package ai.loosh.union

import android.Manifest
import android.content.pm.PackageManager
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.camera.core.CameraSelector
import androidx.camera.core.ExperimentalGetImage
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import ai.loosh.union.seeker.ActionButton
import ai.loosh.union.seeker.CaptureIcon
import ai.loosh.union.seeker.CapturePanel
import ai.loosh.union.seeker.CaptureSymbol
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
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
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.text.font.FontWeight
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
 * The intro appears before any camera work. Only Begin check requests camera
 * permission and enters the existing on-device motion check.
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

    var started by rememberSaveable(challengeWire) { mutableStateOf(false) }
    var granted by remember {
        mutableStateOf(
            ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) ==
                PackageManager.PERMISSION_GRANTED
        )
    }
    var denied by rememberSaveable(challengeWire) { mutableStateOf(false) }
    val permissionLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { result ->
        granted = result
        denied = !result
        started = result
    }
    BackHandler(onBack = onCancel)
    val scrollState = rememberScrollState()
    LaunchedEffect(started) { scrollState.scrollTo(0) }

    // SeekerApp's Scaffold supplies the system insets. Scroll so large text and
    // small screens keep every action reachable without shrinking the artwork.
    Column(
        Modifier.fillMaxSize().background(CaptureBase)
            .verticalScroll(scrollState).padding(20.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        TextButton(
            onClick = onCancel,
            modifier = Modifier.align(Alignment.Start)
                .heightIn(min = CaptureDimensions.primaryButtonMinHeight),
        ) {
            CaptureIcon(CaptureSymbol.ARROW_LEFT, Modifier.size(20.dp), CaptureInk)
            Text("Cancel", Modifier.padding(start = 8.dp), color = CaptureMutedText)
        }
        Text(
            "ARE YOU HUMAN?",
            style = MaterialTheme.typography.displayLarge,
            color = CaptureInk,
            textAlign = TextAlign.Center,
        )
        Spacer(Modifier.height(8.dp))
        Text(
            "A quick motion check before you capture.",
            style = MaterialTheme.typography.bodyLarge,
            color = CaptureMutedText,
            textAlign = TextAlign.Center,
        )
        Spacer(Modifier.height(20.dp))

        if (challenge == null) {
            // Unknown challenges must never be treated as satisfied, even in demo.
            Text(
                "This app doesn't know how to perform the challenge the server " +
                    "asked for ($challengeWire). Update the app and try again.",
                style = MaterialTheme.typography.bodyMedium,
                color = CaptureInk,
            )
            return@Column
        }

        if (started && granted) {
            LivenessCamera(
                challenge = challenge,
                lifecycleOwner = lifecycleOwner,
                onPassed = onPassed,
                modifier = Modifier.fillMaxWidth().aspectRatio(0.75f),
            )
        } else {
            LivenessPreCheck()
            if (denied) {
                Spacer(Modifier.height(16.dp))
                Text(
                    "Camera access is needed for the motion check. Try Begin check again, " +
                        "or allow camera access in Settings.",
                    style = MaterialTheme.typography.bodyMedium,
                    color = CaptureInk,
                    textAlign = TextAlign.Center,
                )
            }
            Spacer(Modifier.height(24.dp))
            ActionButton(
                label = "Begin check",
                labelStyle = MaterialTheme.typography.titleLarge.copy(fontFamily = Anton),
                onClick = {
                    // Recheck after returning from Settings; never open the camera
                    // simply because permission was granted on a previous visit.
                    granted = ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) ==
                        PackageManager.PERMISSION_GRANTED
                    if (granted) {
                        denied = false
                        started = true
                    } else {
                        permissionLauncher.launch(Manifest.permission.CAMERA)
                    }
                },
            )
        }

        Spacer(Modifier.height(8.dp))
        TextButton(
            modifier = Modifier.heightIn(min = CaptureDimensions.primaryButtonMinHeight),
            onClick = {
                // Nonzero 32-byte demo digest accepted by the session API.
                // This escape hatch deliberately works without camera permission.
                onPassed("deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
            },
        ) {
            Text("Continue (demo)", style = MaterialTheme.typography.bodyMedium, color = CaptureMutedText)
        }
        busy?.let {
            Text(it, style = MaterialTheme.typography.bodySmall, color = CaptureMutedText)
            Spacer(Modifier.height(8.dp))
        }
        Text(
            "Images are processed on-device and not retained.",
            style = MaterialTheme.typography.bodySmall,
            color = CaptureMutedText,
            textAlign = TextAlign.Center,
        )
    }
}

@Composable
private fun LivenessPreCheck() {
    val hero = painterResource(R.drawable.liveness_hero)
    Image(
        painter = hero,
        contentDescription = null,
        modifier = Modifier.fillMaxWidth()
            .aspectRatio(hero.intrinsicSize.width / hero.intrinsicSize.height),
        contentScale = ContentScale.Fit,
    )
    Spacer(Modifier.height(20.dp))
    Text(
        "Look at the camera and blink to confirm human",
        style = MaterialTheme.typography.headlineSmall.copy(fontWeight = FontWeight.Bold),
        color = CaptureInk,
        textAlign = TextAlign.Center,
    )
    Spacer(Modifier.height(8.dp))
    Text(
        "Follow the on-screen motion prompt.",
        style = MaterialTheme.typography.bodyLarge,
        color = CaptureMutedText,
        textAlign = TextAlign.Center,
    )
    Spacer(Modifier.height(20.dp))
    Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
        LivenessInfoRow(CaptureSymbol.VERIFIED, "Motion is checked on your device.")
        LivenessInfoRow(CaptureSymbol.CAMERA_OFF, "Camera images aren't saved or uploaded.")
        LivenessInfoRow(CaptureSymbol.DOCUMENT_CHECK, "Your completion status is recorded.")
    }
}

@Composable
private fun LivenessInfoRow(symbol: CaptureSymbol, text: String) {
    CapturePanel(Modifier.fillMaxWidth()) {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            CaptureIcon(symbol, Modifier.size(24.dp), tint = CaptureInk)
            Text(text, style = MaterialTheme.typography.bodyMedium, color = CaptureInk)
        }
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
