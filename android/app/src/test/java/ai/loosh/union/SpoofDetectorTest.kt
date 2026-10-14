package ai.loosh.union

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The classifier's preprocessing and the fusion policy.
 *
 * The model itself cannot be tested here - that needs a device and real spoof
 * attempts. What can be tested is everything that would make the model score
 * garbage while looking like it works: the wrong channel order, the wrong
 * tensor layout, unnormalised values, or a crop that throws away the context
 * the model was trained to read. Each of those produces a correctly-shaped
 * tensor the model will happily consume, and the only symptom is a classifier
 * that appears to guess.
 */
class SpoofDetectorTest {

    // -- preprocessing ---------------------------------------------------------

    private fun argb(r: Int, g: Int, b: Int) = (0xFF shl 24) or (r shl 16) or (g shl 8) or b

    @Test
    fun `channels are planar BGR, not RGB`() {
        // One pure-red pixel: in BGR planar order red belongs in the LAST plane.
        val pixels = IntArray(4) { argb(255, 0, 0) }
        val out = preprocessFaceCrop(pixels, side = 2)
        val plane = 4
        assertEquals(0f, out[0], 1e-6f)              // blue plane
        assertEquals(0f, out[plane], 1e-6f)          // green plane
        assertEquals(1f, out[2 * plane], 1e-6f)      // red plane
    }

    @Test
    fun `a blue pixel lands in the first plane`() {
        val out = preprocessFaceCrop(IntArray(4) { argb(0, 0, 255) }, side = 2)
        assertEquals(1f, out[0], 1e-6f)
        assertEquals(0f, out[2 * 4], 1e-6f)
    }

    @Test
    fun `values are divided by 255`() {
        val out = preprocessFaceCrop(IntArray(1) { argb(255, 128, 0) }, side = 1)
        assertEquals(0f, out[0], 1e-6f)              // B
        assertEquals(128f / 255f, out[1], 1e-6f)     // G
        assertEquals(1f, out[2], 1e-6f)              // R
    }

    @Test
    fun `layout is NCHW, so a plane is contiguous`() {
        // Two pixels differing only in green: in NCHW they are adjacent within
        // the green plane. In NHWC they would be three apart.
        val pixels = intArrayOf(argb(0, 10, 0), argb(0, 20, 0), argb(0, 30, 0), argb(0, 40, 0))
        val out = preprocessFaceCrop(pixels, side = 2)
        val greenPlane = out.slice(4 until 8)
        assertEquals(listOf(10f, 20f, 30f, 40f).map { it / 255f }, greenPlane)
    }

    @Test
    fun `the tensor has three planes of side squared`() {
        val out = preprocessFaceCrop(IntArray(80 * 80), side = 80)
        assertEquals(3 * 80 * 80, out.size)
    }

    @Test
    fun `a wrongly sized buffer is refused rather than silently reshaped`() {
        val error = runCatching { preprocessFaceCrop(IntArray(10), side = 80) }.exceptionOrNull()
        assertTrue(error is IllegalArgumentException)
    }

    @Test
    fun `alpha is ignored`() {
        val opaque = preprocessFaceCrop(intArrayOf(argb(1, 2, 3)), side = 1)
        val transparent = preprocessFaceCrop(intArrayOf(0x00010203), side = 1)
        assertEquals(opaque.toList(), transparent.toList())
    }

    // -- crop geometry ---------------------------------------------------------

    @Test
    fun `the crop is about 2point7 times the face box`() {
        // The model reads the context around a face - a phone bezel, the edge
        // of a print. A tight crop throws away the evidence.
        val face = FaceBox(400, 400, 500, 500)
        val crop = spoofCropRect(face, imageWidth = 2000, imageHeight = 2000)
        assertEquals(270, crop.width)
        assertEquals(270, crop.height)
    }

    @Test
    fun `the crop is centred on the face`() {
        val face = FaceBox(400, 400, 500, 500)
        val crop = spoofCropRect(face, 2000, 2000)
        assertEquals(450, (crop.left + crop.right) / 2)
        assertEquals(450, (crop.top + crop.bottom) / 2)
    }

    @Test
    fun `the crop stays square for a non-square face box`() {
        // A stretched crop changes the texture statistics being classified.
        val crop = spoofCropRect(FaceBox(100, 100, 200, 300), 2000, 2000)
        assertEquals(crop.width, crop.height)
    }

    @Test
    fun `a face at the edge slides the crop rather than leaving the frame`() {
        val crop = spoofCropRect(FaceBox(0, 0, 100, 100), 2000, 2000)
        assertTrue(crop.left >= 0)
        assertTrue(crop.top >= 0)
        assertEquals(270, crop.width)
    }

    @Test
    fun `the crop never runs past the far edge`() {
        val crop = spoofCropRect(FaceBox(1900, 1900, 2000, 2000), 2000, 2000)
        assertTrue(crop.right <= 2000)
        assertTrue(crop.bottom <= 2000)
    }

    @Test
    fun `a face larger than the frame yields a crop bounded by the frame`() {
        val crop = spoofCropRect(FaceBox(0, 0, 400, 400), 200, 200)
        assertTrue(crop.width <= 200)
        assertTrue(crop.height <= 200)
        assertEquals(crop.width, crop.height)
    }

    // -- reading the output ----------------------------------------------------

    @Test
    fun `class 1 is live`() {
        val verdict = readSpoofOutput(floatArrayOf(0.05f, 0.90f, 0.05f))
        assertEquals(1, verdict.argmaxClass)
        assertEquals(0.90f, verdict.liveScore, 1e-6f)
        assertTrue(verdict.live)
    }

    @Test
    fun `a print spoof is class 0`() {
        assertFalse(readSpoofOutput(floatArrayOf(0.90f, 0.05f, 0.05f)).live)
    }

    @Test
    fun `a replay spoof is class 2`() {
        assertFalse(readSpoofOutput(floatArrayOf(0.05f, 0.05f, 0.90f)).live)
    }

    @Test
    fun `a barely-won argmax does not count as live`() {
        // 0.34 wins a three-way split but is not a claim worth making.
        val verdict = readSpoofOutput(floatArrayOf(0.33f, 0.34f, 0.33f))
        assertEquals(1, verdict.argmaxClass)
        assertFalse(verdict.live)
    }

    @Test
    fun `a high live score still loses to a higher spoof score`() {
        // Threshold alone would accept this; argmax alone would reject it.
        // Requiring both is the point.
        val verdict = readSpoofOutput(floatArrayOf(0.0f, 0.45f, 0.55f))
        assertFalse(verdict.live)
    }

    @Test
    fun `a wrong output width is refused`() {
        val error = runCatching { readSpoofOutput(floatArrayOf(0.5f, 0.5f)) }.exceptionOrNull()
        assertTrue(error is IllegalArgumentException)
    }

    // -- fusion ----------------------------------------------------------------

    private val live = SpoofVerdict(liveScore = 0.95f, argmaxClass = 1)
    private val spoof = SpoofVerdict(liveScore = 0.02f, argmaxClass = 2)

    @Test
    fun `motion alone does not pass`() {
        // The gap this whole file exists to close: a video replay performs a
        // real blink, so the motion challenge cannot see it.
        val fusion = LivenessFusion()
        repeat(30) { fusion.observe(spoof) }
        assertEquals(LivenessOutcome.SPOOF_SUSPECTED, fusion.outcome(motionPassed = true))
    }

    @Test
    fun `the classifier alone does not pass`() {
        val fusion = LivenessFusion()
        repeat(30) { fusion.observe(live) }
        assertEquals(LivenessOutcome.PENDING, fusion.outcome(motionPassed = false))
    }

    @Test
    fun `both together pass`() {
        val fusion = LivenessFusion()
        repeat(10) { fusion.observe(live) }
        assertEquals(LivenessOutcome.PASSED, fusion.outcome(motionPassed = true))
    }

    @Test
    fun `one live frame is not enough`() {
        // Holding a real face up for an instant mid-replay is cheap.
        val fusion = LivenessFusion(minLiveFrames = 5)
        fusion.observe(live)
        assertEquals(LivenessOutcome.PENDING, fusion.outcome(motionPassed = true))
    }

    @Test
    fun `bad frames invalidate historical live evidence`() {
        // A continuous recent run must be earned again after a bad frame.
        val fusion = LivenessFusion(minLiveFrames = 5)
        repeat(20) { fusion.observe(live) }
        repeat(3) { fusion.observe(spoof) }
        assertEquals(LivenessOutcome.PENDING, fusion.outcome(motionPassed = true))
        repeat(5) { fusion.observe(live) }
        assertEquals(LivenessOutcome.PASSED, fusion.outcome(motionPassed = true))
    }

    @Test
    fun `spoof is only suspected once there is enough evidence`() {
        val fusion = LivenessFusion(minLiveFrames = 5)
        repeat(2) { fusion.observe(spoof) }
        assertFalse(fusion.spoofSuspected)
        repeat(5) { fusion.observe(spoof) }
        assertTrue(fusion.spoofSuspected)
    }

    @Test
    fun `the model constants match the model card`() {
        assertEquals("silentface.tflite", SpoofModel.ASSET)
        assertEquals(80, SpoofModel.INPUT_SIDE)
        assertEquals(1, SpoofModel.LIVE_CLASS)
        assertEquals(2.7f, SpoofModel.CROP_SCALE, 1e-6f)
    }
    @Test
    fun `spoof dominates historical and subsequent live frames`() {
        val fusion = LivenessFusion()
        repeat(5) { fusion.observe(live) }
        repeat(10) { fusion.observe(spoof) }
        repeat(5) { fusion.observe(live) }
        assertEquals(LivenessOutcome.SPOOF_SUSPECTED, fusion.outcome(true))
    }

    @Test
    fun `lost face and missing classification reset evidence`() {
        val fusion = LivenessFusion()
        repeat(5) { fusion.observe(live) }
        val attempt = fusion.attempt
        fusion.observe(null)
        assertTrue(fusion.attempt > attempt)
        fusion.observe(live)
        assertEquals(LivenessOutcome.PENDING, fusion.outcome(true))
    }

    @Test
    fun `frame gap expires evidence and starts a new motion attempt`() {
        var now = 0L
        val fusion = LivenessFusion(clockMs = { now })
        repeat(5) { fusion.observe(live) }
        val attempt = fusion.attempt
        now = 751
        assertEquals(LivenessOutcome.PENDING, fusion.outcome(true))
        fusion.observe(live)
        assertTrue(fusion.attempt > attempt)
        assertEquals(LivenessOutcome.PENDING, fusion.outcome(true))
    }

    @Test
    fun `spoof frame restarts motion attempt`() {
        val fusion = LivenessFusion()
        fusion.observe(live)
        val attempt = fusion.attempt
        fusion.observe(spoof)
        assertTrue(fusion.attempt > attempt)
    }

}
