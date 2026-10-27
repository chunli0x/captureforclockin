package app.capture.seeker

import app.capture.Base58
import app.capture.BuildConfig
import android.app.Application
import android.net.Uri
import android.util.Base64
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.solana.mobilewalletadapter.clientlib.*
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject

private class ApiFailure(val status: Int, message: String) : java.io.IOException(message)

/**
 * What [SeekerViewModel.recover] should do for a prepared transaction id. Kept
 * as a pure decision-only type (no Android, no network) so the branch between
 * replaying an exact confirm and falling back to a chain scan is unit testable.
 */
sealed class RecoveryAction {
    data class Confirm(val id: String, val signature: String) : RecoveryAction()
    data class Reconcile(val id: String) : RecoveryAction()

    companion object {
        fun forTransaction(id: String, storedSignature: String?): RecoveryAction =
            if (storedSignature == null) Reconcile(id) else Confirm(id, storedSignature)
    }
}

/** The phase of the most recent async call. */
sealed class AsyncStatus {
    object Idle : AsyncStatus()
    object Working : AsyncStatus()
    object Success : AsyncStatus()
    object Failure : AsyncStatus()
}

data class PilotState(
    val server: String = "",
    val wallet: String = "",
    val busy: Boolean = false,
    val message: String = "",
    val error: String? = null,
    val status: AsyncStatus = AsyncStatus.Idle,
    val transient: String? = null,
    // --- Bond (test $SKR stake) ---
    val bondTestSkrUnits: Long = 0,
    val bonded: Boolean = false,
    val bondFunded: Boolean = false,
    val livenessCompleted: Boolean = false,
    val livenessChallenge: String? = null,
    val faucetMints: Int = 0,
    val faucetMaxMints: Int = 5,
    val judgeStakedSkr: Long = 0,
    val judgeWarnings: Int = 0,
    // --- Judgment product (RLHF preference judging) ---
    val judgeCalibrated: Boolean = false,
    val judgeAgreementRate: Double = 0.0,
    val judgeJudgedCount: Int = 0,
    val judgeLevel: Int = 0,
    val judgeLevelName: String = "Scout",
    val judgeRateMultiplier: Double = 1.0,
    val judgeBaseSkr: Long = 0,
    val judgeFlatSkr: Long = 0,
    val judgeEarnedSkr: Long = 0,
    val judgeSettledSkr: Long = 0,
    val calibrationCompleted: Int = 0,
    val calibrationRequired: Int = 10,
    val calibrationCorrect: Int = 0,
    val judgePair: JSONObject? = null,
) {
    // The stats API exposes earned and settled; pending is their unpaid difference.
    val judgePendingSkr: Long get() = (judgeEarnedSkr - judgeSettledSkr).coerceAtLeast(0)
}

class SeekerViewModel(application: Application) : AndroidViewModel(application) {
    private val prefs = application.getSharedPreferences("seeker.api", 0)

    private fun signatureKey(id: String) = "sig.$id"
    private fun storeSignature(id: String, signature: String) =
        prefs.edit().putString(signatureKey(id), signature).apply()
    private fun storedSignature(id: String): String? = prefs.getString(signatureKey(id), null)
    private fun clearSignature(id: String) = prefs.edit().remove(signatureKey(id)).apply()

    private val mutable =
        MutableStateFlow(
            PilotState(server = prefs.getString("server", null) ?: BuildConfig.SEEKER_API_BASE)
        )
    val state = mutable.asStateFlow()
    private var token: String? = null
    private var livenessSessionId: String? = null
    private val http =
        OkHttpClient.Builder()
            .followRedirects(false)
            .followSslRedirects(false)
            .callTimeout(75, TimeUnit.SECONDS)
            .build()
    private val adapter =
        MobileWalletAdapter(
                connectionIdentity =
                    ConnectionIdentity(
                        identityUri = Uri.parse("https://capture.magnumopus.ai"),
                        iconUri = Uri.parse("favicon.ico"),
                        identityName = "Capture",
                    )
            )
            .apply { blockchain = Solana.Devnet }

    private fun run(label: String, success: String? = null, block: suspend () -> Unit) {
        if (mutable.value.busy) return
        mutable.update {
            it.copy(busy = true, error = null, message = label, status = AsyncStatus.Working, transient = null)
        }
        viewModelScope.launch {
            try {
                block()
                mutable.update {
                    it.copy(message = "", status = AsyncStatus.Success, transient = success)
                }
            } catch (e: Exception) {
                if (e is kotlinx.coroutines.CancellationException) throw e
                if (e is ApiFailure && e.status == 401) clearSession()
                mutable.update {
                    it.copy(error = e.message ?: "Request failed", status = AsyncStatus.Failure)
                }
            } finally {
                mutable.update { it.copy(busy = false) }
            }
        }
    }

    fun dismissTransient() {
        if (!mutable.value.busy)
            mutable.update { it.copy(status = AsyncStatus.Idle, transient = null) }
    }

    fun dismissError() {
        if (!mutable.value.busy)
            mutable.update {
                it.copy(
                    error = null,
                    status = if (it.status == AsyncStatus.Failure) AsyncStatus.Idle else it.status,
                )
            }
    }

    private fun clearSession() {
        token = null
        livenessSessionId = null
        adapter.authToken = null
        mutable.value = PilotState(server = mutable.value.server, busy = mutable.value.busy)
    }

    fun server(value: String) {
        if (mutable.value.busy) return
        val url = value.trim().trimEnd('/')
        val uri = Uri.parse(url)
        if (
            uri.host.isNullOrBlank() ||
                !(uri.scheme == "https" || BuildConfig.DEBUG && uri.scheme == "http") ||
                uri.userInfo != null
        ) {
            mutable.update {
                it.copy(error = "Enter an HTTPS server address (HTTP is allowed only in debug).")
            }
            return
        }
        token = null
        adapter.authToken = null
        prefs.edit().putString("server", url).apply()
        mutable.value = PilotState(server = url)
    }

    private suspend fun bytes(
        path: String,
        json: JSONObject? = null,
        requestKey: String? = null,
    ): ByteArray =
        withContext(Dispatchers.IO) {
            val url = mutable.value.server
            require(BuildConfig.DEBUG || url.startsWith("https://")) {
                "Release builds require HTTPS."
            }
            val builder = Request.Builder().url(url + path)
            token?.let { builder.header("Authorization", "Bearer $it") }
            requestKey?.let { builder.header("Idempotency-Key", it) }
            if (json != null)
                builder.post(json.toString().toRequestBody("application/json".toMediaType()))
            http.newCall(builder.build()).execute().use { response ->
                val data = response.body?.bytes() ?: error("Empty server response")
                if (!response.isSuccessful) {
                    val detail = runCatching {
                        JSONObject(String(data)).optString("detail")
                    }
                        .getOrDefault("Request failed")
                    throw ApiFailure(response.code, "${response.code}: $detail")
                }
                data
            }
        }

    private suspend fun get(path: String) = JSONObject(String(bytes(path)))

    private suspend fun post(
        path: String,
        data: JSONObject = JSONObject(),
        requestKey: String? = null,
    ) = JSONObject(String(bytes(path, data, requestKey = requestKey)))

    fun connect(sender: ActivityResultSender, over18: Boolean) =
        run("Connecting to my Seeker Wallet…", "Seeker Wallet connected") {
            val connection = adapter.connect(sender)
            check(connection is TransactionResult.Success) { "Seeker Wallet connection was not approved." }
            val wallet = Base58.encode(connection.authResult.accounts.first().publicKey)
            val challenge = post("/auth/challenge", JSONObject().put("wallet", wallet))
            val signed =
                adapter.transact(sender) { auth ->
                    check(Base58.encode(auth.accounts.first().publicKey) == wallet) {
                        "Seeker Wallet changed; sign in again."
                    }
                    signMessagesDetached(
                        arrayOf(challenge.getString("message").toByteArray(Charsets.UTF_8)),
                        arrayOf(auth.accounts.first().publicKey),
                    )
                }
            check(signed is TransactionResult.Success) { "Sign-in was not approved." }
            val signature =
                signed.successPayload?.messages?.first()?.signatures?.first()
                    ?: error("Seeker Wallet returned no signature")
            val verify =
                buildVerifyRequest(
                    challengeId = challenge.getString("challenge_id"),
                    signature = Base58.encode(signature),
                    over18 = over18,
                )
            val session =
                post(
                    "/auth/verify",
                    JSONObject()
                        .put("challenge_id", verify.challengeId)
                        .put("signature", verify.signature)
                        .put("over_18", verify.over18),
                )
            token = session.getString("token")
            mutable.update { it.copy(wallet = wallet) }
            refreshJudgment()
        }

    fun logout() =
        run("Signing out", "Signed out") {
            try {
                post("/auth/logout")
            } finally {
                clearSession()
            }
        }

    // --- Bond (test $SKR stake) ---

    fun faucet() =
        run("Requesting test \$SKR", "Test \$SKR funded") {
            val r = post("/bond/faucet")
            mutable.update {
                it.copy(bondFunded = true, faucetMints = r.optInt("mints", it.faucetMints + 1))
            }
        }

    fun depositBond(sender: ActivityResultSender) =
        run("Depositing bond", "Bond deposited") {
            val prepared = post("/bond/deposit")
            val tx = Base64.decode(prepared.getString("transaction_base64"), Base64.DEFAULT)
            val result =
                adapter.transact(sender) { auth ->
                    check(Base58.encode(auth.accounts.first().publicKey) == mutable.value.wallet) {
                        "Seeker Wallet changed; sign in again."
                    }
                    signAndSendTransactions(arrayOf(tx))
                }
            check(result is TransactionResult.Success) {
                "Seeker Wallet did not send the transaction. Refresh to recover if it was sent."
            }
            val sig =
                result.successPayload?.signatures?.first()
                    ?: error("Seeker Wallet returned no signature; use Recover.")
            val id = prepared.getString("id")
            val signature = Base58.encode(sig)
            storeSignature(id, signature)
            try {
                confirm(id, signature)
                clearSignature(id)
            } catch (e: Exception) {
                throw e
            }
            refreshJudgment()
        }

    // --- Judgment product (RLHF preference judging) ---

    fun startLiveness() =
        run("Starting the human check", null) {
            livenessSessionId = null
            mutable.update { it.copy(livenessChallenge = null) }
            val s = post(
                "/liveness/session",
                JSONObject()
                    .put("contributor", mutable.value.wallet)
                    .put("task_id", "capture-session"),
            )
            livenessSessionId = s.getString("session_id")
            mutable.update { it.copy(livenessChallenge = s.getString("challenge")) }
        }

    fun completeLiveness(resultDigest: String) =
        run("Verifying", "You're verified as human") {
            val sid = livenessSessionId ?: error("No liveness session in progress")
            post(
                "/liveness/complete",
                JSONObject()
                    .put("contributor", mutable.value.wallet)
                    .put("session_id", sid)
                    .put("result_digest", resultDigest),
            )
            livenessSessionId = null
            // Completion is already persisted. A later stats/next failure must
            // not leave the client treating this single-use session as pending.
            mutable.update { it.copy(livenessCompleted = true, livenessChallenge = null) }
            refreshJudgment()
        }

    fun judgeNext() = run("Loading next pair", "Pair loaded") { refreshJudgment() }

    fun submitJudgment(choice: String? = null, rating: Int? = null) =
        run("Submitting judgment", "Judgment recorded") {
            val pair = mutable.value.judgePair ?: error("No pair to judge")
            val id = pair.getString("pair_id")
            val body = JSONObject()
            if (choice != null) body.put("choice", choice)
            else body.put("rating", rating ?: error("A rating is required"))
            post("/judge/$id", body)
            refreshJudgment()
        }

    private suspend fun refreshJudgment() {
        val me = get("/me")
        val next = get("/judge/next")
        val stats = get("/judge/stats")
        val level = stats.optInt("level", 0).coerceIn(0, MAX_JUDGE_LEVEL)
        val progress = stats.optJSONObject("calibration_progress")
        val pair = if (next.optString("kind", "none") == "none") null else next
        mutable.update {
            it.copy(
                bondTestSkrUnits = me.optLong("bond_test_skr_units", 0),
                bonded = me.optBoolean("bonded", false),
                bondFunded = me.optBoolean("skr_funded", false),
                livenessCompleted = me.optBoolean("liveness_completed", false),
                faucetMints = me.optInt("faucet_mints", 0),
                faucetMaxMints = me.optInt("faucet_max", 5),
                judgeStakedSkr = stats.optLong("staked_skr", 0),
                judgeWarnings = stats.optInt("warnings", 0),
                judgePair = pair,
                judgeCalibrated = stats.optBoolean("calibrated", false),
                judgeAgreementRate = stats.optDouble("agreement_rate", 0.0),
                judgeJudgedCount = stats.optInt("judged_count", 0),
                judgeLevel = level,
                judgeLevelName = stats.optString("level_name", judgeLevelName(level)),
                judgeRateMultiplier = stats.optDouble("rate_multiplier", judgeRateMultiplier(level)),
                judgeBaseSkr = stats.optLong("base_skr", 0),
                judgeFlatSkr = stats.optLong("flat_skr", 0),
                judgeEarnedSkr = stats.optLong("earned_skr", 0),
                judgeSettledSkr = stats.optLong("settled_skr", 0),
                calibrationCompleted = progress?.optInt("completed", 0) ?: 0,
                calibrationRequired = progress?.optInt("required", 10) ?: 10,
                calibrationCorrect = progress?.optInt("correct", 0) ?: 0,
            )
        }
    }

    fun refresh() = run("Refreshing", "Refreshed") { refreshJudgment() }

    // --- Prepared transactions (bond deposit/withdraw) ---

    fun transact(path: String, sender: ActivityResultSender) =
        run("Review this transaction in Seeker Wallet", "Transaction confirmed") {
            val prepared = post("$path/prepare")
            val tx = Base64.decode(prepared.getString("transaction_base64"), Base64.DEFAULT)
            val result =
                adapter.transact(sender) { auth ->
                    check(Base58.encode(auth.accounts.first().publicKey) == mutable.value.wallet) {
                        "Seeker Wallet changed; sign in again."
                    }
                    signAndSendTransactions(arrayOf(tx))
                }
            check(result is TransactionResult.Success) {
                "Seeker Wallet did not send the transaction. Refresh to recover if it was sent."
            }
            val sig =
                result.successPayload?.signatures?.first()
                    ?: error("Seeker Wallet returned no signature; use Recover.")
            val id = prepared.getString("id")
            val signature = Base58.encode(sig)
            storeSignature(id, signature)
            try {
                confirm(id, signature)
                clearSignature(id)
            } catch (e: Exception) {
                throw e
            }
            refreshJudgment()
        }

    private suspend fun confirm(id: String, signature: String) {
        var confirmed = false
        repeat(10) {
            if (!confirmed) {
                try {
                    post(
                        "/transactions/$id/confirm",
                        JSONObject().put("signature", signature),
                    )
                    confirmed = true
                } catch (e: Exception) {
                    if (e !is ApiFailure || e.status != 503 || it == 9) throw e
                    delay(1500)
                }
            }
        }
    }

    fun recover(id: String) =
        run("Checking finalized chain history", "Transaction recovered") {
            when (val action = RecoveryAction.forTransaction(id, storedSignature(id))) {
                is RecoveryAction.Confirm -> {
                    confirm(action.id, action.signature)
                    clearSignature(action.id)
                }
                is RecoveryAction.Reconcile -> post("/transactions/${action.id}/reconcile")
            }
            refreshJudgment()
        }
}
