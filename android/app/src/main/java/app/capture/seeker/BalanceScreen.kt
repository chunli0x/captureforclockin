package app.capture.seeker

import app.capture.*
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import com.solana.mobilewalletadapter.clientlib.ActivityResultSender
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import kotlin.math.floor

/** 1 token = 1_000_000 base units (6 decimals), matching the SPL mints. */
private const val TOKEN_UNITS = 1_000_000.0

/** Formats token base units as a human amount, e.g. 1_000_000 -> "1", 1 -> "0.000001". */
fun formatToken(units: Long): String {
    val tokens = units / TOKEN_UNITS
    return if (tokens == floor(tokens)) "%.0f".format(tokens) else "%.6f".format(tokens).trimEnd('0')
}

/** The balances page: what a judge has earned (\$SKR) and staked (\$SKR), plus the
 *  level ladder. Test tokens on devnet, not real value. */
@Composable
fun BalanceScreen(s: PilotState, vm: SeekerViewModel, sender: ActivityResultSender, onBack: () -> Unit) {
    LaunchedEffect(Unit) { vm.refresh() }
    NavyBackground(Modifier.fillMaxSize().captureScreenMotion()) {
        Column(
            Modifier
                .fillMaxSize()
                .safeDrawingPadding()
                .verticalScroll(rememberScrollState())
                .padding(24.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp),
        ) {
            CaptureWordmark()
            EditorialHeading("BALANCES.")
            Text(
                "Earned in test \$SKR, staked in test \$SKR, devnet only, not real value.",
                style = MaterialTheme.typography.bodyMedium,
                color = MutedGray,
            )
            OperationError(s, onRetry = vm::refresh)
            BalanceCard(s)
            PendingTransactions(s, vm)
            SecondaryButton("Withdraw bond", onClick = { vm.transact("/bond/withdraw", sender) },
                enabled = !s.busy && s.bonded && s.judgeWarnings == 0)
            Text("Withdrawal requires a clean bond, no pending judgments, and an elapsed cooldown.",
                style = MaterialTheme.typography.bodySmall, color = MutedGray)
            JudgeTierLadder(s.judgeLevel)
            SecondaryButton("Refresh", onClick = { vm.refresh() }, enabled = !s.busy)
            ActionButton("Back to judging", onClick = onBack)
        }
    }
}

@Composable
private fun BalanceCard(s: PilotState) {
    Column(verticalArrangement = Arrangement.spacedBy(16.dp)) {
        Row(Modifier.fillMaxWidth().height(IntrinsicSize.Min), horizontalArrangement = Arrangement.spacedBy(16.dp)) {
            BalanceModule("Earned", s.judgeEarnedSkr, CaptureSymbol.COINS, Modifier.weight(1f).fillMaxHeight())
            BalanceModule("Pending", s.judgePendingSkr, CaptureSymbol.CLOCK, Modifier.weight(1f).fillMaxHeight())
        }
        Row(Modifier.fillMaxWidth().height(IntrinsicSize.Min), horizontalArrangement = Arrangement.spacedBy(16.dp)) {
            BalanceModule("Settled", s.judgeSettledSkr, CaptureSymbol.VERIFIED, Modifier.weight(1f).fillMaxHeight())
            BalanceModule("Staked", s.judgeStakedSkr, CaptureSymbol.LOCK, Modifier.weight(1f).fillMaxHeight())
        }
        Text(
            "${s.judgeLevelName} · ×${"%.2f".format(s.judgeRateMultiplier)} rate · ${s.judgeWarnings} warning${if (s.judgeWarnings == 1) "" else "s"}",
            style = MaterialTheme.typography.bodySmall, color = MutedGray,
        )
    }
}

@Composable
private fun BalanceModule(label: String, amount: Long, symbol: CaptureSymbol, modifier: Modifier = Modifier) {
    CapturePanel(modifier) {
        CaptureIcon(symbol, Modifier.size(28.dp), CaptureAccent)
        Text(label, style = MaterialTheme.typography.titleMedium, color = MutedGray)
        Text(formatToken(amount), style = MaterialTheme.typography.headlineMedium, color = CaptureInk)
        Text("\$SKR", style = MaterialTheme.typography.labelSmall, color = MutedGray)
    }
}
