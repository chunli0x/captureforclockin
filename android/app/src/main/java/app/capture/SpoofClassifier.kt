package app.capture

import android.content.Context
import android.graphics.Bitmap
import android.util.Log
import com.google.ai.edge.litert.CompiledModel

/**
 * Runs the silent-face model. Everything decidable lives in `SpoofDetector.kt`.
 *
 * This class is the part that cannot be unit tested on a build server: it
 * loads a 1.8 MB flatbuffer, asks LiteRT to compile it for whatever
 * accelerator the phone has, and runs it. The preprocessing it calls, the
 * output it reads and the policy that consumes the result are all pure
 * functions with tests.
 *
 * ## Failing visibly rather than either way
 *
 * LiteRT may not load - an accelerator that will not compile the graph, a
 * stripped ABI, an OEM driver. Two bad answers are available. Failing closed
 * bricks liveness on that device, so nobody can use the app. Failing open
 * silently drops the texture check while the UI still says "human confirmed",
 * which is the dishonest option and the easier one to ship by accident.
 *
 * So it fails *visibly*: [available] goes false, the screen says the passive
 * check is unavailable, and - the part that matters - the fact is folded into
 * the result digest, so the attestation minted from that session records that
 * only the motion challenge ran. A verifier reading it back can tell the
 * difference.
 */
class SpoofClassifier private constructor(private val model: CompiledModel) {

    companion object {
        private const val TAG = "SpoofClassifier"

        /** Load the model, or null when this device cannot run it. */
        fun createOrNull(context: Context): SpoofClassifier? =
            try {
                SpoofClassifier(CompiledModel.create(context.assets, SpoofModel.ASSET))
            } catch (e: Throwable) {
                // Throwable, not Exception: a missing native library surfaces
                // as UnsatisfiedLinkError, which is exactly the case this is
                // here to survive.
                Log.w(TAG, "anti-spoofing model unavailable; motion challenge only", e)
                null
            }
    }

    /**
     * Score one face crop.
     *
     * The bitmap must already be the model's square input size; cropping is
     * [spoofCropRect]'s job and scaling is the caller's, so this method has no
     * opinion about geometry it cannot verify.
     */
    fun classify(faceCrop: Bitmap): SpoofVerdict? {
        require(faceCrop.width == SpoofModel.INPUT_SIDE &&
            faceCrop.height == SpoofModel.INPUT_SIDE) {
            "expected a ${SpoofModel.INPUT_SIDE}px square crop, got " +
                "${faceCrop.width}x${faceCrop.height}"
        }

        val pixels = IntArray(SpoofModel.INPUT_SIDE * SpoofModel.INPUT_SIDE)
        faceCrop.getPixels(
            pixels, 0, SpoofModel.INPUT_SIDE, 0, 0,
            SpoofModel.INPUT_SIDE, SpoofModel.INPUT_SIDE,
        )

        return try {
            val inputs = model.createInputBuffers()
            val outputs = model.createOutputBuffers()
            inputs[0].writeFloat(preprocessFaceCrop(pixels))
            model.run(inputs, outputs)
            readSpoofOutput(outputs[0].readFloat())
        } catch (e: Throwable) {
            // A frame that fails to score is a dropped frame, not a spoof.
            // Counting it as one would fail honest users on a transient error.
            Log.w(TAG, "spoof inference failed for a frame", e)
            null
        }
    }

    fun close() {
        runCatching { model.close() }
    }
}
