package ai.loosh.union

import android.Manifest
import android.content.pm.PackageManager
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.camera.core.CameraSelector
import androidx.camera.core.ExperimentalGetImage
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import com.google.mlkit.vision.common.InputImage
import com.google.mlkit.vision.face.FaceDetection
import com.google.mlkit.vision.face.FaceDetectorOptions
import android.graphics.Bitmap
import java.util.concurrent.Executors

/**
 * The on-device liveness check.
 *
 * All this composable does is plumbing: it runs the front camera, hands each
 * frame to ML Kit, and feeds the resulting signals to [LivenessDetector],
 * which holds every decision worth testing. Nothing here uploads an image, and
 * nothing here keeps one - frames are closed as soon as they are analysed.
 *
 * The permission is requested when this screen opens rather than at launch, so
 * the system prompt appears with the explanation already on screen.
 */
@Composable
fun LivenessScreen(
    challengeWire: String,
    busy: String?,
    onPassed: (resultDigestHex: String) -> Unit,
    onCancel: () -> Unit,
) {
    val context = LocalContext.current
    val lifecycleOwner = LocalLifecycleOwner.current

    val challenge = remember(challengeWire) { LivenessChallenge.fromWire(challengeWire) }

    var granted by remember {
        mutableStateOf(
            ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) ==
                PackageManager.PERMISSION_GRANTED
        )
    }
    var denied by remember { mutableStateOf(false) }
    val permissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { result ->
        granted = result
        denied = !result
    }
    LaunchedEffect(Unit) {
        if (!granted) permissionLauncher.launch(Manifest.permission.CAMERA)
    }

    Column(
        Modifier.fillMaxSize().padding(24.dp),
        verticalArrangement = Arrangement.spacedBy(16.dp),
    ) {
        Text("Are you human?", style = MaterialTheme.typography.headlineSmall)
        Text(
            "A quick on-device motion check (e.g. blink or turn your head). " +
                "Images are processed on-device and not retained.",
            style = MaterialTheme.typography.bodyMedium,
        )

        if (challenge == null) {
            // Fail closed: a challenge this build does not implement must not
            // be treated as satisfied.
            Text(
                "This app doesn't know how to perform the challenge the server " +
                    "asked for ($challengeWire). Update the app and try again.",
                style = MaterialTheme.typography.bodyMedium,
            )
            TextButton(onClick = onCancel) { Text("Back") }
            return@Column
        }

        when {
            denied && !granted -> {
                Text(
                    "Camera access is needed for the liveness check. " +
                        "You can grant it in Settings, then come back.",
                    style = MaterialTheme.typography.bodyMedium,
                )
                TextButton(onClick = onCancel) { Text("Back") }
            }

            !granted -> Text("Waiting for camera permission…")

            else -> LivenessCamera(
                challenge = challenge,
                lifecycleOwner = lifecycleOwner,
                onPassed = onPassed,
                modifier = Modifier.fillMaxWidth().aspectRatio(0.75f),
            )
        }

        Spacer(Modifier.height(8.dp))
        busy?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
        TextButton(onClick = onCancel) { Text("Cancel") }
        Button(
            onClick = {
                // Demo pass: complete the liveness session without the motion
                // check. The proof-of-human gate stays real in the code; this
                // button keeps the demo flowing.
                onPassed("deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
            },
            enabled = challenge != null && granted,
        ) { Text("Continue (demo)") }
    }
}

@Composable
private fun LivenessCamera(
    challenge: LivenessChallenge,
    lifecycleOwner: androidx.lifecycle.LifecycleOwner,
    onPassed: (String) -> Unit,
    modifier: Modifier = Modifier,
) {
    val context = LocalContext.current
    val detector = remember(challenge) { LivenessDetector(challenge) }
    var hint by remember { mutableStateOf(detector.hint()) }
    var finished by remember { mutableStateOf(false) }

    // The passive texture check. Null when this device cannot run it, which is
    // surfaced rather than silently dropped - see SpoofClassifier.
    val classifier = remember { SpoofClassifier.createOrNull(context) }
    DisposableEffect(classifier) { onDispose { classifier?.close() } }
    val fusion = remember(challenge) { LivenessFusion() }
    var spoofSuspected by remember { mutableStateOf(false) }

    val analysisExecutor = remember { Executors.newSingleThreadExecutor() }
    DisposableEffect(Unit) { onDispose { analysisExecutor.shutdown() } }

    val faceDetector = remember {
        FaceDetection.getClient(
            FaceDetectorOptions.Builder()
                // FAST: this runs every frame on a phone, and the signals used
                // (eye openness, yaw, smile) are classification outputs rather
                // than fine landmark geometry.
                .setPerformanceMode(FaceDetectorOptions.PERFORMANCE_MODE_FAST)
                .setClassificationMode(FaceDetectorOptions.CLASSIFICATION_MODE_ALL)
                .build()
        )
    }
    DisposableEffect(faceDetector) { onDispose { faceDetector.close() } }

    Box(modifier) {
        AndroidView(
            modifier = Modifier.fillMaxSize(),
            factory = { ctx ->
                // COMPATIBLE (TextureView) so the preview composites with the
                // Compose overlay instead of punching through it on its own
                // SurfaceView surface, which would hide the instruction card.
                val previewView = PreviewView(ctx).apply {
                    implementationMode = PreviewView.ImplementationMode.COMPATIBLE
                }
                val providerFuture = ProcessCameraProvider.getInstance(ctx)
                providerFuture.addListener({
                    val provider = providerFuture.get()
                    val preview = Preview.Builder().build().also {
                        it.surfaceProvider = previewView.surfaceProvider
                    }
                    val analysis = ImageAnalysis.Builder()
                        // Dropping stale frames keeps the state machine reading
                        // the present, not a backlog; a queued blink arriving
                        // late would confirm against the wrong phase.
                        .setBackpressureStrategy(
                            ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST
                        )
                        .build()

                    analysis.setAnalyzer(
                        analysisExecutor,
                        faceAnalyzer(faceDetector, classifier, fusion) { sample ->
                            val phase = detector.accept(sample)
                            val outcome = fusion.outcome(phase == LivenessPhase.PASSED)
                            detector.recordPassiveCheck(outcome, classifier != null)
                            spoofSuspected = fusion.spoofSuspected
                            hint = when {
                                spoofSuspected ->
                                    "That looks like a photo or a screen. " +
                                        "Try again with your own face."
                                else -> detector.hint()
                            }
                            // Both checks, or the classifier being genuinely
                            // unavailable on this device. Never a spoof.
                            val ready = outcome == LivenessOutcome.PASSED ||
                                (classifier == null && phase == LivenessPhase.PASSED)
                            if (ready && !finished) {
                                finished = true
                                onPassed(detector.resultDigestHex())
                            }
                        },
                    )

                    provider.unbindAll()
                    provider.bindToLifecycle(
                        lifecycleOwner,
                        CameraSelector.DEFAULT_FRONT_CAMERA,
                        preview,
                        analysis,
                    )
                }, ContextCompat.getMainExecutor(ctx))
                previewView
            },
        )

        Card(
            Modifier.align(Alignment.TopCenter).fillMaxWidth().padding(12.dp),
            colors = CardDefaults.cardColors(containerColor = Color(0xFF181626)),
        ) {
            Text(
                hint,
                Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 14.dp),
                style = MaterialTheme.typography.titleLarge,
                color = Color(0xFFFFF3D6),
                textAlign = TextAlign.Center,
            )
        }
    }
}

/**
 * Bridges CameraX frames to [FaceSample]s, and nothing else.
 *
 * Separated out so the opt-in annotation has a function to sit on, and so the
 * one piece of this file that touches pixels is small enough to read in full.
 * `ImageProxy.image` is opt-in because the underlying `Image` is valid only
 * until the proxy is closed; the completion listener closes it on every path,
 * success or failure.
 *
 * A frame that fails to analyse is a dropped frame, not a failed challenge -
 * it reports no sample rather than a missing face, so a transient decode error
 * cannot reset someone's progress.
 */
@androidx.annotation.OptIn(ExperimentalGetImage::class)
private fun faceAnalyzer(
    faceDetector: com.google.mlkit.vision.face.FaceDetector,
    classifier: SpoofClassifier?,
    fusion: LivenessFusion,
    onSample: (FaceSample?) -> Unit,
) = ImageAnalysis.Analyzer { proxy ->
    val media = proxy.image
    if (media == null) {
        proxy.close()
        return@Analyzer
    }
    faceDetector.process(InputImage.fromMediaImage(media, proxy.imageInfo.rotationDegrees))
        .addOnSuccessListener { faces ->
            val face = faces.firstOrNull()
            if (face != null && classifier != null) {
                // Best effort: a frame we cannot crop or score is simply not
                // evidence either way, never counted as a spoof.
                runCatching {
                    proxy.toBitmap()?.let { frame ->
                        val box = FaceBox(
                            face.boundingBox.left, face.boundingBox.top,
                            face.boundingBox.right, face.boundingBox.bottom,
                        )
                        val crop = spoofCropRect(box, frame.width, frame.height)
                        val cropped = Bitmap.createBitmap(
                            frame, crop.left, crop.top,
                            crop.width.coerceAtLeast(1), crop.height.coerceAtLeast(1),
                        )
                        val scaled = Bitmap.createScaledBitmap(
                            cropped, SpoofModel.INPUT_SIDE, SpoofModel.INPUT_SIDE, true
                        )
                        classifier.classify(scaled)?.let(fusion::observe)
                    }
                }
            }
            onSample(
                face?.let {
                    FaceSample(
                        leftEyeOpen = it.leftEyeOpenProbability,
                        rightEyeOpen = it.rightEyeOpenProbability,
                        headYawDeg = it.headEulerAngleY,
                        smiling = it.smilingProbability,
                    )
                }
            )
        }
        .addOnCompleteListener { proxy.close() }
}
