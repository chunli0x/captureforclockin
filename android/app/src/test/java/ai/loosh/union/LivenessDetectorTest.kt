package ai.loosh.union

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The liveness state machine, checked without a camera.
 *
 * The property each of these defends is the same one: a challenge must not be
 * satisfiable by a still image. A detector that passed on a *state* rather
 * than a transition would look completely fine on a device - you would hold
 * your face up, it would say "done", and nobody would notice it says the same
 * thing to a photograph.
 */
class LivenessDetectorTest {

    private fun face(
        eyes: Float = 0.9f,
        yaw: Float = 0f,
        smile: Float = 0.0f,
    ) = FaceSample(leftEyeOpen = eyes, rightEyeOpen = eyes, headYawDeg = yaw, smiling = smile)

    /** Feed the same sample enough times to clear the confirm window. */
    private fun LivenessDetector.hold(sample: FaceSample?, frames: Int = 3): LivenessPhase {
        var phase = this.phase
        repeat(frames) { phase = accept(sample) }
        return phase
    }

    // -- blink -----------------------------------------------------------------

    @Test
    fun `a still open-eyed face never passes blink`() {
        val detector = LivenessDetector(LivenessChallenge.BLINK)
        detector.hold(face(eyes = 0.95f), frames = 60)
        assertEquals(LivenessPhase.READY, detector.phase)
    }

    @Test
    fun `a photo of shut eyes does not pass on its own`() {
        // Reaches HALFWAY, but the return leg never comes.
        val detector = LivenessDetector(LivenessChallenge.BLINK)
        detector.hold(face(eyes = 0.05f), frames = 60)
        assertEquals(LivenessPhase.HALFWAY, detector.phase)
    }

    @Test
    fun `open then shut then open passes`() {
        val detector = LivenessDetector(LivenessChallenge.BLINK)
        detector.hold(face(eyes = 0.95f))
        detector.hold(face(eyes = 0.05f))
        assertEquals(LivenessPhase.PASSED, detector.hold(face(eyes = 0.95f)))
    }

    @Test
    fun `a single noisy frame is not a blink`() {
        val detector = LivenessDetector(LivenessChallenge.BLINK)
        detector.hold(face(eyes = 0.95f))
        detector.accept(face(eyes = 0.05f)) // one frame only
        detector.hold(face(eyes = 0.95f))
        assertEquals(LivenessPhase.READY, detector.phase)
    }

    @Test
    fun `one occluded eye does not read as a blink`() {
        val detector = LivenessDetector(LivenessChallenge.BLINK)
        val winking = FaceSample(
            leftEyeOpen = 0.02f, rightEyeOpen = 0.95f, headYawDeg = 0f, smiling = 0f
        )
        detector.hold(winking, frames = 30)
        assertEquals(LivenessPhase.READY, detector.phase)
    }

    @Test
    fun `losing the face resets progress`() {
        val detector = LivenessDetector(LivenessChallenge.BLINK)
        detector.hold(face(eyes = 0.95f))
        detector.hold(face(eyes = 0.05f))
        assertEquals(LivenessPhase.HALFWAY, detector.phase)

        detector.hold(null) // phone lowered, or a photo swapped in
        assertEquals(LivenessPhase.NO_FACE, detector.phase)

        // The earlier half does not carry over: opening eyes now is only the
        // starting state again, not a completed blink.
        detector.hold(face(eyes = 0.95f))
        assertEquals(LivenessPhase.READY, detector.phase)
    }

    @Test
    fun `provisional motion pass is reset when face leaves`() {
        val detector = LivenessDetector(LivenessChallenge.BLINK)
        detector.hold(face(eyes = 0.95f))
        detector.hold(face(eyes = 0.05f))
        detector.hold(face(eyes = 0.95f))
        assertEquals(LivenessPhase.NO_FACE, detector.hold(null, frames = 10))
    }

    @Test
    fun `missing eye classification cannot pass blink`() {
        val detector = LivenessDetector(LivenessChallenge.BLINK)
        val noOpinion = FaceSample(null, null, headYawDeg = 0f, smiling = 0f)
        detector.hold(noOpinion, frames = 30)
        assertEquals(LivenessPhase.READY, detector.phase)
    }

    // -- turns -----------------------------------------------------------------

    @Test
    fun `turning left and back passes turn left`() {
        val detector = LivenessDetector(LivenessChallenge.TURN_LEFT)
        detector.hold(face(yaw = 0f))
        detector.hold(face(yaw = 30f))
        assertEquals(LivenessPhase.PASSED, detector.hold(face(yaw = 2f)))
    }

    @Test
    fun `a head held at an angle does not pass`() {
        // Without the return leg, tilting the phone would be enough.
        val detector = LivenessDetector(LivenessChallenge.TURN_LEFT)
        detector.hold(face(yaw = 35f), frames = 60)
        assertEquals(LivenessPhase.HALFWAY, detector.phase)
    }

    @Test
    fun `turning the wrong way does not pass`() {
        val detector = LivenessDetector(LivenessChallenge.TURN_LEFT)
        detector.hold(face(yaw = -35f), frames = 30)
        assertEquals(LivenessPhase.READY, detector.phase)
    }

    @Test
    fun `turn right is the mirror of turn left`() {
        val detector = LivenessDetector(LivenessChallenge.TURN_RIGHT)
        detector.hold(face(yaw = 0f))
        detector.hold(face(yaw = -30f))
        assertEquals(LivenessPhase.PASSED, detector.hold(face(yaw = 0f)))
    }

    @Test
    fun `a turn just short of the threshold does not count`() {
        val detector = LivenessDetector(LivenessChallenge.TURN_LEFT)
        detector.hold(face(yaw = LivenessThresholds.YAW_TURNED - 1f), frames = 30)
        assertEquals(LivenessPhase.READY, detector.phase)
    }

    @Test
    fun `returning only part way does not complete the turn`() {
        val detector = LivenessDetector(LivenessChallenge.TURN_LEFT)
        detector.hold(face(yaw = 0f))
        detector.hold(face(yaw = 30f))
        detector.hold(face(yaw = 15f), frames = 30) // outside YAW_CENTRED
        assertEquals(LivenessPhase.HALFWAY, detector.phase)
    }

    // -- smile -----------------------------------------------------------------

    @Test
    fun `neutral then smiling passes`() {
        val detector = LivenessDetector(LivenessChallenge.SMILE)
        detector.hold(face(smile = 0.05f))
        assertEquals(LivenessPhase.PASSED, detector.hold(face(smile = 0.95f)))
    }

    @Test
    fun `a permanently smiling photo does not pass`() {
        val detector = LivenessDetector(LivenessChallenge.SMILE)
        detector.hold(face(smile = 0.95f), frames = 60)
        assertEquals(LivenessPhase.READY, detector.phase)
    }

    // -- wire names ------------------------------------------------------------

    @Test
    fun `wire names match the backend challenge values`() {
        assertEquals(LivenessChallenge.BLINK, LivenessChallenge.fromWire("blink"))
        assertEquals(LivenessChallenge.TURN_LEFT, LivenessChallenge.fromWire("turn_left"))
        assertEquals(LivenessChallenge.TURN_RIGHT, LivenessChallenge.fromWire("turn_right"))
        assertEquals(LivenessChallenge.SMILE, LivenessChallenge.fromWire("smile"))
    }

    @Test
    fun `an unknown challenge fails closed`() {
        // A server that adds a challenge must not silently auto-pass here.
        assertNull(LivenessChallenge.fromWire("hop_on_one_leg"))
    }

    // -- digest ----------------------------------------------------------------

    @Test
    fun `the digest is 32 bytes of hex`() {
        val detector = LivenessDetector(LivenessChallenge.BLINK)
        detector.hold(face())
        assertEquals(64, detector.resultDigestHex().length)
        assertEquals(32, detector.resultDigest().size)
    }

    @Test
    fun `two different runs produce different digests`() {
        // The proof hash must differ between attempts at the same challenge.
        val first = LivenessDetector(LivenessChallenge.BLINK)
        first.hold(face(eyes = 0.95f))
        first.hold(face(eyes = 0.05f))
        first.hold(face(eyes = 0.95f))

        val second = LivenessDetector(LivenessChallenge.BLINK)
        second.hold(face(eyes = 0.95f), frames = 9)
        second.hold(face(eyes = 0.02f))
        second.hold(face(eyes = 0.99f))

        assertNotEquals(first.resultDigestHex(), second.resultDigestHex())
    }

    @Test
    fun `the digest carries no landmark or image data`() {
        // It is a summary of counts and extremes, never anything biometric.
        val detector = LivenessDetector(LivenessChallenge.BLINK)
        detector.hold(face())
        // Only the hash escapes; assert its shape is a fixed-width digest and
        // therefore cannot be carrying a variable-length payload.
        assertTrue(detector.resultDigestHex().matches(Regex("[0-9a-f]{64}")))
    }

    @Test
    fun `prompts exist for every challenge and phase`() {
        for (challenge in LivenessChallenge.entries) {
            val detector = LivenessDetector(challenge)
            assertTrue(detector.hint().isNotBlank())
            detector.hold(face())
            assertTrue(detector.hint().isNotBlank())
        }
    }

    // -- what the digest records about the passive check ------------------------

    @Test
    fun `an unavailable classifier is recorded, not silently omitted`() {
        // The whole point of failing visibly: an attestation minted from this
        // session must be distinguishable from one where both checks ran.
        val withModel = LivenessDetector(LivenessChallenge.BLINK)
        withModel.hold(face())
        withModel.recordPassiveCheck(LivenessOutcome.PASSED, available = true)

        val withoutModel = LivenessDetector(LivenessChallenge.BLINK)
        withoutModel.hold(face())
        withoutModel.recordPassiveCheck(LivenessOutcome.PASSED, available = false)

        assertEquals("live", withModel.passiveCheck)
        assertEquals("unavailable", withoutModel.passiveCheck)
        assertNotEquals(withModel.resultDigestHex(), withoutModel.resultDigestHex())
    }

    @Test
    fun `a suspected spoof is recorded distinctly`() {
        val detector = LivenessDetector(LivenessChallenge.BLINK)
        detector.recordPassiveCheck(LivenessOutcome.SPOOF_SUSPECTED, available = true)
        assertEquals("spoof_suspected", detector.passiveCheck)
    }

    @Test
    fun `the passive check defaults to unavailable before anything runs`() {
        // Fail-safe default: a detector that was never told about the
        // classifier must not claim the texture check happened.
        assertEquals("unavailable", LivenessDetector(LivenessChallenge.BLINK).passiveCheck)
    }
}
