package ai.loosh.union

/**
 * Passive anti-spoofing, layered under the motion challenge.
 *
 * [LivenessDetector] asks the person to do something a photograph cannot. That
 * closes the still-image attack and nothing else: a **video replay** of the
 * right person blinking satisfies every motion challenge we can pose, because
 * the motion really is there. What a replay cannot easily fake is the *texture*
 * of a live face - a screen re-photographed has moire, a flat specular
 * response and paper-print artefacts that a classifier can see.
 *
 * So the two run together and both must pass:
 *
 * ```text
 *   motion challenge (behavioural) ─┐
 *                                   ├─► liveness accepted
 *   silent-face classifier (texture)┘
 * ```
 *
 * ## The model
 *
 * `litert-community/Silent-Face-Anti-Spoofing-LiteRT`, MiniFASNetV2 at the 2.7
 * crop scale, Apache-2.0, shipped in `assets/silentface.tflite` (1,850,744
 * bytes, sha256 4ff758f4…08e1b2). Everything runs on the device; no image and
 * no crop leaves the phone.
 *
 * Its contract, from the model card and reproduced exactly in
 * [preprocessFaceCrop]: input `[1, 3, 80, 80]` NCHW, **BGR**, `x / 255`, taken
 * from a crop about 2.7x the face box and centred on it. Output is a 3-way
 * softmax where **class 1 is live** and classes 0 and 2 are print and replay
 * spoofs; the live score is `output[1]` and a live verdict is `argmax == 1`.
 *
 * ## What this still does not buy
 *
 * Two things, and neither is closed by adding a model:
 *
 * 1. The upstream project ensembles two MiniFASNets, at crop scales 2.7 and
 *    4.0, plus a face-quality gate. This ships one of them. Expect it to be
 *    weaker than the paper against a determined attacker.
 * 2. **The server still cannot see the sensor.** A modified build can report
 *    "live" without ever opening the camera. Device attestation is the only
 *    answer to that and it remains out of scope - see docs/LIVENESS.md.
 *
 * The honest claim does not change: a live person performed a motion challenge
 * and the device's own classifier judged the face live. Not bank-grade
 * anti-spoofing.
 */

/** Where the classifier's assets live, so one edit moves both the model and the tests. */
object SpoofModel {
    const val ASSET = "silentface.tflite"

    /** Model card: crop ~2.7x the face box, centred. */
    const val CROP_SCALE = 2.7f

    const val INPUT_SIDE = 80

    /** Softmax index that means live. 0 and 2 are print and replay spoofs. */
    const val LIVE_CLASS = 1

    /**
     * Minimum live score to accept.
     *
     * Deliberately above a bare `argmax == 1`: on a three-way softmax an
     * argmax can win with barely a third of the mass, and a liveness check
     * that accepts a 0.34 confidence is not making much of a claim. This is a
     * starting point and needs tuning against real spoof attempts on a real
     * device - it is the number most likely to be wrong in this file.
     */
    const val LIVE_THRESHOLD = 0.70f
}

/** A face box in image pixels, as ML Kit reports it. */
data class FaceBox(val left: Int, val top: Int, val right: Int, val bottom: Int) {
    val width: Int get() = right - left
    val height: Int get() = bottom - top
}

/** The classifier's reading of one frame. */
data class SpoofVerdict(val liveScore: Float, val argmaxClass: Int) {
    /**
     * Both conditions, not either.
     *
     * `argmax` alone accepts a barely-won three-way split; the threshold alone
     * would accept a frame the model thought was *more* likely a replay if the
     * live score happened to clear the bar.
     */
    val live: Boolean
        get() = argmaxClass == SpoofModel.LIVE_CLASS && liveScore >= SpoofModel.LIVE_THRESHOLD
}

/**
 * The square crop to feed the classifier.
 *
 * Centred on the face box and scaled by [SpoofModel.CROP_SCALE], because the
 * model was trained on context around the face - the edge of a phone being held
 * up, or the border of a printed photo, is exactly the evidence it uses, and a
 * tight crop throws that away.
 *
 * Square, because the model takes a square input and a non-square crop would
 * be stretched, changing the very texture statistics being classified.
 *
 * Clamped to the frame. A face near the edge yields a smaller crop rather than
 * one with invented pixels; padding with black would hand the classifier a
 * hard edge that looks like the border of a print.
 */
fun spoofCropRect(face: FaceBox, imageWidth: Int, imageHeight: Int): FaceBox {
    require(imageWidth > 0 && imageHeight > 0) { "image must have a positive size" }

    val centreX = (face.left + face.right) / 2f
    val centreY = (face.top + face.bottom) / 2f
    val side = maxOf(face.width, face.height) * SpoofModel.CROP_SCALE

    var half = side / 2f
    // Never larger than the frame, or the clamping below would produce a
    // rectangle that is not square.
    half = minOf(half, imageWidth / 2f, imageHeight / 2f)

    var left = centreX - half
    var top = centreY - half
    // Slide rather than shrink, so a face near an edge keeps the full crop
    // size and only loses its centring.
    left = left.coerceIn(0f, imageWidth - 2 * half)
    top = top.coerceIn(0f, imageHeight - 2 * half)

    return FaceBox(
        left = left.toInt(),
        top = top.toInt(),
        right = (left + 2 * half).toInt(),
        bottom = (top + 2 * half).toInt(),
    )
}

/**
 * ARGB pixels to the tensor the model expects.
 *
 * `[1, 3, 80, 80]` NCHW, BGR, divided by 255. Each of those three is a way to
 * be silently wrong: NHWC instead of NCHW, RGB instead of BGR, or raw 0..255
 * all produce a tensor of the right size that the model will happily score,
 * and the only symptom is a classifier that seems to guess. Hence the tests.
 *
 * @param pixels ARGB_8888, row-major, [SpoofModel.INPUT_SIDE] squared.
 */
fun preprocessFaceCrop(pixels: IntArray, side: Int = SpoofModel.INPUT_SIDE): FloatArray {
    require(pixels.size == side * side) {
        "expected ${side * side} pixels for a ${side}x$side crop, got ${pixels.size}"
    }

    val plane = side * side
    val out = FloatArray(3 * plane)
    for (i in 0 until plane) {
        val argb = pixels[i]
        val r = (argb shr 16) and 0xFF
        val g = (argb shr 8) and 0xFF
        val b = argb and 0xFF
        // Planar BGR: all blue, then all green, then all red.
        out[i] = b / 255f
        out[plane + i] = g / 255f
        out[2 * plane + i] = r / 255f
    }
    return out
}

/** Read a `[1, 3]` softmax into a verdict. */
fun readSpoofOutput(softmax: FloatArray): SpoofVerdict {
    require(softmax.size == 3) { "expected a 3-class softmax, got ${softmax.size}" }
    var best = 0
    for (i in softmax.indices) if (softmax[i] > softmax[best]) best = i
    return SpoofVerdict(liveScore = softmax[SpoofModel.LIVE_CLASS], argmaxClass = best)
}

/** What the two checks together decided. */
enum class LivenessOutcome { PENDING, PASSED, SPOOF_SUSPECTED }

/**
 * Fuses the behavioural and texture checks.
 *
 * The motion challenge has to complete *and* the classifier has to have called
 * the face live, over a run of frames rather than one. A single live frame is
 * cheap - hold a real face up for an instant mid-replay - so the classifier's
 * verdict is taken over the frames during which the motion actually happened.
 */
class LivenessFusion(
    private val minLiveFrames: Int = 5,
    private val maxGapMs: Long = 750,
    private val clockMs: () -> Long = { System.nanoTime() / 1_000_000 },
) {
    private var liveFrames = 0
    private var spoofFrames = 0
    private var lastFrameMs: Long? = null
    var attempt: Long = 0
        private set

    init {
        require(minLiveFrames > 0 && maxGapMs > 0)
    }

    fun reset() {
        liveFrames = 0
        spoofFrames = 0
        lastFrameMs = null
        attempt++
    }

    val expired: Boolean
        get() = lastFrameMs?.let { clockMs() - it > maxGapMs } ?: false

    fun observe(verdict: SpoofVerdict?) {
        if (verdict == null) {
            reset()
            return
        }
        if (expired) reset()
        lastFrameMs = clockMs()
        if (verdict.live) {
            liveFrames++
        } else {
            liveFrames = 0
            spoofFrames++
            attempt++ // motion must restart with the next continuous live run
        }
    }

    val sawEnoughLiveFrames: Boolean get() = !expired && liveFrames >= minLiveFrames

    // Rejection is latched for this motion attempt. A new face/attempt resets
    // both checks; a few live frames cannot erase sustained spoof evidence.
    val spoofSuspected: Boolean get() = spoofFrames >= minLiveFrames

    fun outcome(motionPassed: Boolean): LivenessOutcome = when {
        spoofSuspected -> LivenessOutcome.SPOOF_SUSPECTED
        motionPassed && sawEnoughLiveFrames -> LivenessOutcome.PASSED
        else -> LivenessOutcome.PENDING
    }
}
