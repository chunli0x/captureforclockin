package ai.loosh.union

import java.security.MessageDigest

/**
 * The liveness state machine, kept free of CameraX and ML Kit on purpose.
 *
 * Everything that decides whether a challenge was met lives here, as a pure
 * function of a stream of face samples. That is what lets it be tested in CI:
 * the camera half cannot run on a build server, but the half that can be wrong
 * in an interesting way can.
 *
 * ## What it checks, and why each challenge needs two phases
 *
 * A photo held up to the camera produces a perfectly good face with stable
 * landmarks. So no challenge is satisfied by a *state* - only by a
 * **transition** the holder of a still image cannot produce:
 *
 * - blink: eyes open, then shut, then open again.
 * - turn: facing forward, then turned past a threshold, then back.
 * - smile: neutral, then smiling.
 *
 * Requiring the return leg matters as much as the departure. Without it,
 * "turned" could be satisfied by simply holding the phone at an angle, and
 * "eyes shut" by a photo of someone mid-blink.
 *
 * Losing the face resets progress, so a real face cannot be swapped for a
 * printed one between phases.
 *
 * ## What this does not do
 *
 * It is not anti-spoofing. A video of the right person performing the right
 * motion passes, and so does a modified build that never opens the camera -
 * the server cannot see the sensor. The honest claim, which the backend
 * repeats in its own response, is "a live person performed a motion
 * challenge". See docs/LIVENESS.md.
 */

/** One frame's worth of face signals, as ML Kit reports them. */
data class FaceSample(
    /** Null when the classifier has no opinion, which is not the same as 0. */
    val leftEyeOpen: Float?,
    val rightEyeOpen: Float?,
    /** Degrees. Positive is the user turning to their own left in ML Kit. */
    val headYawDeg: Float,
    val smiling: Float?,
)

enum class LivenessChallenge(val wireName: String, val prompt: String) {
    BLINK("blink", "Blink"),
    TURN_LEFT("turn_left", "Turn your head left, then back"),
    TURN_RIGHT("turn_right", "Turn your head right, then back"),
    SMILE("smile", "Smile");

    companion object {
        /** Unknown challenges fail closed rather than silently passing. */
        fun fromWire(name: String): LivenessChallenge? =
            entries.firstOrNull { it.wireName == name }
    }
}

enum class LivenessPhase {
    /** No usable face in frame. */
    NO_FACE,

    /** Face present, waiting for the starting condition (eyes open, centred). */
    READY,

    /** The departure happened; waiting for the return leg. */
    HALFWAY,

    PASSED,
}

object LivenessThresholds {
    /** Eye-open probability above which an eye counts as open. */
    const val EYE_OPEN = 0.60f

    /** And below which it counts as shut. Deliberately not the same number:
     *  a single threshold makes the classifier's noise look like blinking. */
    const val EYE_SHUT = 0.30f

    /** Degrees of yaw that count as "turned". */
    const val YAW_TURNED = 22f

    /** And back inside this to count as returned to centre. */
    const val YAW_CENTRED = 10f

    const val SMILE_NEUTRAL = 0.30f
    const val SMILE_SMILING = 0.70f

    /** Frames a condition must hold before it counts.
     *
     *  One frame is noise; ML Kit's probabilities jitter frame to frame and a
     *  single sample below EYE_SHUT is more likely a bad estimate than a
     *  blink.
     */
    const val CONFIRM_FRAMES = 2
}

/**
 * Tracks one challenge attempt.
 *
 * Not thread-safe: feed it from the analyzer thread only, which is how
 * CameraX delivers frames anyway.
 */
class LivenessDetector(val challenge: LivenessChallenge) {

    var phase: LivenessPhase = LivenessPhase.NO_FACE
        private set

    /** Evidence summarised into the result digest. */
    private var framesSeen = 0
    private var facesSeen = 0
    private var extremeYaw = 0f
    private var minEyeOpen = 1f
    private var maxSmile = 0f

    private var holdCount = 0

    /**
     * What the passive classifier concluded, folded into the digest.
     *
     * "unavailable" is a real, distinguishable value rather than an absence.
     * A session where LiteRT would not load produces a different proof hash
     * from one where the texture check ran and passed, so an attestation
     * records which checks actually happened instead of implying both did.
     */
    var passiveCheck: String = "unavailable"
        private set

    fun recordPassiveCheck(outcome: LivenessOutcome, available: Boolean) {
        passiveCheck = if (!available) "unavailable" else when (outcome) {
            LivenessOutcome.PASSED -> "live"
            LivenessOutcome.SPOOF_SUSPECTED -> "spoof_suspected"
            LivenessOutcome.PENDING -> "inconclusive"
        }
    }

    fun reset() {
        phase = LivenessPhase.NO_FACE
        holdCount = 0
        framesSeen = 0
        facesSeen = 0
        extremeYaw = 0f
        minEyeOpen = 1f
        maxSmile = 0f
        passiveCheck = "unavailable"
    }

    /** Feed one frame. `null` means no face was detected. */
    fun accept(sample: FaceSample?): LivenessPhase {
        framesSeen++

        if (sample == null) {
            // Until the fused check passes, a motion pass alone is provisional.
            reset()
            return phase
        }

        facesSeen++
        recordEvidence(sample)

        if (phase == LivenessPhase.PASSED) return phase
        if (phase == LivenessPhase.NO_FACE) phase = LivenessPhase.READY

        when (challenge) {
            LivenessChallenge.BLINK -> stepBlink(sample)
            LivenessChallenge.TURN_LEFT -> stepTurn(sample, toUsersLeft = true)
            LivenessChallenge.TURN_RIGHT -> stepTurn(sample, toUsersLeft = false)
            LivenessChallenge.SMILE -> stepSmile(sample)
        }
        return phase
    }

    private fun recordEvidence(sample: FaceSample) {
        if (kotlin.math.abs(sample.headYawDeg) > kotlin.math.abs(extremeYaw)) {
            extremeYaw = sample.headYawDeg
        }
        eyeOpenness(sample)?.let { if (it < minEyeOpen) minEyeOpen = it }
        sample.smiling?.let { if (it > maxSmile) maxSmile = it }
    }

    /** The more-open eye: one eye occluded by a hand should not read as a blink. */
    private fun eyeOpenness(sample: FaceSample): Float? {
        val left = sample.leftEyeOpen
        val right = sample.rightEyeOpen
        return when {
            left != null && right != null -> maxOf(left, right)
            else -> left ?: right
        }
    }

    /** Advance on a condition only once it has held for CONFIRM_FRAMES. */
    private fun confirm(condition: Boolean, onConfirmed: () -> Unit) {
        if (!condition) {
            holdCount = 0
            return
        }
        holdCount++
        if (holdCount >= LivenessThresholds.CONFIRM_FRAMES) {
            holdCount = 0
            onConfirmed()
        }
    }

    private fun stepBlink(sample: FaceSample) {
        val openness = eyeOpenness(sample) ?: return
        when (phase) {
            LivenessPhase.READY ->
                confirm(openness < LivenessThresholds.EYE_SHUT) {
                    phase = LivenessPhase.HALFWAY
                }
            LivenessPhase.HALFWAY ->
                confirm(openness > LivenessThresholds.EYE_OPEN) {
                    phase = LivenessPhase.PASSED
                }
            else -> Unit
        }
    }

    private fun stepTurn(sample: FaceSample, toUsersLeft: Boolean) {
        val yaw = sample.headYawDeg
        val turned =
            if (toUsersLeft) yaw > LivenessThresholds.YAW_TURNED
            else yaw < -LivenessThresholds.YAW_TURNED
        when (phase) {
            LivenessPhase.READY -> confirm(turned) { phase = LivenessPhase.HALFWAY }
            LivenessPhase.HALFWAY ->
                confirm(kotlin.math.abs(yaw) < LivenessThresholds.YAW_CENTRED) {
                    phase = LivenessPhase.PASSED
                }
            else -> Unit
        }
    }

    private fun stepSmile(sample: FaceSample) {
        val smile = sample.smiling ?: return
        when (phase) {
            LivenessPhase.READY ->
                confirm(smile < LivenessThresholds.SMILE_NEUTRAL) {
                    phase = LivenessPhase.HALFWAY
                }
            LivenessPhase.HALFWAY ->
                confirm(smile > LivenessThresholds.SMILE_SMILING) {
                    phase = LivenessPhase.PASSED
                }
            else -> Unit
        }
    }

    /** What to show above the camera preview. */
    fun hint(): String = when (phase) {
        LivenessPhase.NO_FACE -> "Hold your face in the frame"
        LivenessPhase.READY -> challenge.prompt
        LivenessPhase.HALFWAY -> when (challenge) {
            LivenessChallenge.BLINK -> "Open your eyes"
            LivenessChallenge.TURN_LEFT, LivenessChallenge.TURN_RIGHT ->
                "Now back to centre"
            LivenessChallenge.SMILE -> "Hold that smile"
        }
        LivenessPhase.PASSED -> "Done"
    }

    /**
     * A digest of what was observed, for the server to bind into the proof.
     *
     * Deliberately not biometric: counts and extremes, no landmarks, no image
     * data, nothing that could identify a face. The server treats it as an
     * opaque blob and cannot verify it - which is the known gap documented in
     * docs/LIVENESS.md, not something this digest closes. Its real job is to
     * make the proof hash differ between two runs of the same challenge.
     */
    fun resultDigest(): ByteArray {
        val summary = buildString {
            append("v2|")
            append(challenge.wireName).append('|')
            append(phase.name).append('|')
            append(framesSeen).append('|')
            append(facesSeen).append('|')
            append(Math.round(extremeYaw)).append('|')
            append(Math.round(minEyeOpen * 100)).append('|')
            append(Math.round(maxSmile * 100)).append('|')
            append(passiveCheck)
        }
        return MessageDigest.getInstance("SHA-256").digest(summary.toByteArray())
    }

    fun resultDigestHex(): String =
        resultDigest().joinToString("") { "%02x".format(it) }
}
