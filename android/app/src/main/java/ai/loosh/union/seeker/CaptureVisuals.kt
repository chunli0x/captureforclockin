package ai.loosh.union.seeker

import ai.loosh.union.*
import androidx.compose.animation.core.*
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.draw.blur
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.*
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.drawscope.rotate
import androidx.compose.ui.graphics.drawscope.translate
import androidx.compose.ui.hapticfeedback.HapticFeedbackType
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalHapticFeedback
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.clearAndSetSemantics
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import ai.loosh.union.R
import kotlin.math.*

/** Applied once at each screen root; cards never repeat the canvas gradient. */
@Composable
fun NavyBackground(modifier: Modifier = Modifier, content: @Composable BoxScope.() -> Unit) {
    Box(modifier.background(CaptureGradients.navy), content = content)
}

@Composable
fun ConstellationBackground(modifier: Modifier = Modifier) {
    val drift = rememberInfiniteTransition(label = "constellation")
    val phase by drift.animateFloat(0f, (2 * PI).toFloat(), infiniteRepeatable(tween(6000, easing = LinearEasing)), label = "orb drift")
    Canvas(modifier) {
        val positions = listOf(.12f to .16f, .43f to .08f, .78f to .18f, .92f to .38f, .68f to .43f,
            .24f to .40f, .07f to .65f, .36f to .76f, .74f to .72f, .88f to .90f)
        val points = positions.mapIndexed { i, (x, y) -> Offset(size.width * x, size.height * y + sin(phase + i) * 7.dp.toPx()) }
        listOf(0 to 1, 1 to 2, 2 to 3, 3 to 4, 4 to 5, 0 to 5, 5 to 6, 6 to 7, 7 to 8, 8 to 9, 4 to 8).forEach { (a, b) ->
            drawLine(CaptureOutline.copy(alpha = .6f), points[a], points[b], 1.dp.toPx())
        }
        points.forEachIndexed { i, point ->
            val radius = (9 + i % 4 * 5).dp.toPx()
            drawCircle(Brush.radialGradient(listOf(CaptureAccentBright.copy(alpha = .15f), Color.Transparent), point, radius * 2.5f), radius * 2.5f, point)
            drawCircle(Brush.radialGradient(listOf(CaptureAccent.copy(alpha = if (i % 3 == 0) .9f else .35f), CaptureAccentDeep.copy(alpha = .15f)), point, radius), radius, point)
            if (i % 3 == 0) drawCircle(CaptureInk.copy(alpha = .4f), radius, point, style = Stroke(1.dp.toPx()))
        }
    }
}

/** Crisp offset ink, with no elevation blur. Apply before clipping the fill. */
fun Modifier.captureFlatShadow(shape: Shape = CaptureRadii.md): Modifier = drawBehind {
    val outline = shape.createOutline(size, layoutDirection, this)
    translate(CaptureDimensions.shadowX.toPx(), CaptureDimensions.shadowY.toPx()) {
        drawOutline(outline, CaptureInk)
    }
}

@Composable
fun CapturePanel(modifier: Modifier = Modifier, elevated: Boolean = false, content: @Composable ColumnScope.() -> Unit) {
    Column(modifier.captureFlatShadow().clip(CaptureRadii.md).background(CaptureSurface)
        .border(CaptureDimensions.inkBorder, CaptureOutline, CaptureRadii.md)) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(12.dp), content = content)
    }
}

/** Spec fallback without Haze: translucent navy + white tint, with sharp content.
 * This deliberately does not pretend foreground blur is a backdrop blur. */
fun Modifier.captureGlass(
    shape: androidx.compose.ui.graphics.Shape = CaptureRadii.xl,
    fill: Color = CaptureSurface.copy(alpha = .7f),
): Modifier =
    clip(shape).background(fill).background(CaptureGradients.glass)
        .border(1.dp, CaptureInk.copy(alpha = .10f), shape)

@Composable
fun GlassCard(modifier: Modifier = Modifier, tint: Color = CaptureAccent, content: @Composable ColumnScope.() -> Unit) {
    Column(modifier.captureGlass().border(1.dp, tint.copy(alpha = .35f), CaptureRadii.xl)
        .padding(20.dp), verticalArrangement = Arrangement.spacedBy(12.dp), content = content)
}

@Composable
fun CaptureMark(modifier: Modifier = Modifier) {
    // The retro pop-art camera mark (the identity image). Rendered from the
    // bundled art rather than drawn vectors, so it matches the brand exactly.
    Image(
        painter = painterResource(R.drawable.capture_camera_art),
        contentDescription = "Capture",
        modifier = modifier,
        contentScale = ContentScale.Fit,
    )
}

@Composable
fun CaptureWordmark() {
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        CaptureMark(Modifier.size(36.dp))
        Text("CAPTURE", style = MaterialTheme.typography.displaySmall.copy(fontSize = 20.sp, lineHeight = 24.sp), color = CaptureInk)
    }
}

/** Small, consistent line icons drawn as vectors, with accessible labels at call sites. */
enum class CaptureSymbol { CAMERA_OFF, DOCUMENT_CHECK, CAMERA, GALLERY, PERSON, RING, BOLT, STREAK, EXPLORE, ROUTE, VERIFIED, TARGET, BRANCH, LOCK, SURFACE, LIGHT, WEATHER, MOTION, OBJECT, PLACE, VIDEO, EYE, STARS, QUESTION_BUBBLE, FILMSTRIP, COINS, SCALES, INFO, ARROW_LEFT, ARROW_RIGHT, ARROW_DOWN, CLOCK, WALLET }

@Composable
fun CaptureIcon(symbol: CaptureSymbol, modifier: Modifier = Modifier, tint: Color = CaptureAccent, description: String? = null) {
    Canvas(modifier.semantics { if (description != null) contentDescription = description }) {
        val u = size.minDimension / 24f
        fun line(x: Float, y: Float, x2: Float, y2: Float) = drawLine(tint, Offset(x*u,y*u), Offset(x2*u,y2*u), 1.6f*u, StrokeCap.Round)
        fun circle(x: Float, y: Float, r: Float) = drawCircle(tint, r*u, Offset(x*u,y*u), style = Stroke(1.6f*u))
        fun path(vararg points: Float) {
            val p = Path().apply { moveTo(points[0]*u,points[1]*u); for (i in 2 until points.size step 2) lineTo(points[i]*u,points[i+1]*u) }
            drawPath(p,tint,style=Stroke(1.6f*u,cap=StrokeCap.Round,join=StrokeJoin.Round))
        }
        when(symbol) {
            CaptureSymbol.CAMERA -> { path(3f,7f,7f,7f,9f,4f,15f,4f,17f,7f,21f,7f,21f,20f,3f,20f,3f,7f); circle(12f,13f,4f) }
            CaptureSymbol.CAMERA_OFF -> {
                path(3f,7f,7f,7f,9f,4f,15f,4f,17f,7f,21f,7f,21f,20f,3f,20f,3f,7f)
                circle(12f,13f,4f)
                line(2f,2f,22f,22f)
            }
            CaptureSymbol.DOCUMENT_CHECK -> {
                path(5f,2f,14f,2f,20f,8f,20f,22f,5f,22f,5f,2f)
                path(14f,2f,14f,8f,20f,8f)
                path(8f,15f,11f,18f,17f,12f)
            }
            CaptureSymbol.GALLERY -> { path(5f,3f,21f,3f,21f,19f,5f,19f,5f,3f); path(2f,7f,2f,22f,17f,22f); path(5f,16f,10f,11f,14f,15f,17f,12f,21f,16f); circle(16f,7f,1f) }
            CaptureSymbol.PERSON -> { circle(12f,7f,4f); path(4f,21f,4f,18f,7f,14f,17f,14f,20f,18f,20f,21f) }
            CaptureSymbol.BOLT -> path(14f,2f,5f,14f,11f,14f,10f,22f,20f,9f,13f,9f,14f,2f)
            CaptureSymbol.STREAK -> { path(12f,2f,10f,9f,6f,7f,4f,14f,6f,20f,12f,22f,18f,20f,20f,14f,16f,8f,15f,13f,12f,2f) }
            CaptureSymbol.EXPLORE -> { circle(12f,12f,9f); path(16f,8f,14f,14f,8f,16f,10f,10f,16f,8f) }
            CaptureSymbol.ROUTE, CaptureSymbol.MOTION -> { circle(5f,19f,2f); circle(19f,5f,2f); path(5f,17f,5f,6f,9f,3f,13f,6f,13f,18f,17f,21f,19f,18f,19f,7f) }
            CaptureSymbol.VERIFIED -> { circle(12f,12f,9f); path(7f,12f,11f,16f,17f,8f) }
            CaptureSymbol.TARGET -> { circle(12f,12f,9f); circle(12f,12f,5f); circle(12f,12f,1f) }
            CaptureSymbol.BRANCH -> { path(12f,22f,12f,13f,5f,6f,5f,2f); path(12f,13f,19f,6f,19f,2f); path(2f,5f,5f,2f,8f,5f); path(16f,5f,19f,2f,22f,5f) }
            CaptureSymbol.LOCK -> { path(5f,11f,19f,11f,19f,22f,5f,22f,5f,11f); path(8f,11f,8f,5f,10f,2f,14f,2f,16f,5f,16f,11f); line(12f,15f,12f,18f) }
            CaptureSymbol.SURFACE -> { path(2f,8f,12f,3f,22f,8f,12f,13f,2f,8f); path(2f,13f,12f,18f,22f,13f); path(2f,18f,12f,23f,22f,18f) }
            CaptureSymbol.LIGHT -> { circle(12f,12f,4f); repeat(8) { val a=it*PI/4; line(12f+cos(a).toFloat()*7,12f+sin(a).toFloat()*7,12f+cos(a).toFloat()*10,12f+sin(a).toFloat()*10) } }
            CaptureSymbol.WEATHER -> { path(4f,16f,2f,13f,4f,10f,7f,10f,9f,5f,15f,4f,19f,9f,22f,11f,22f,16f,4f,16f); line(8f,19f,7f,22f); line(15f,19f,14f,22f) }
            CaptureSymbol.OBJECT -> { path(3f,7f,12f,2f,21f,7f,21f,17f,12f,22f,3f,17f,3f,7f,12f,12f,21f,7f); line(12f,12f,12f,22f) }
            CaptureSymbol.PLACE -> { path(3f,11f,12f,3f,21f,11f); path(5f,10f,5f,21f,19f,21f,19f,10f); path(10f,21f,10f,14f,14f,14f,14f,21f) }
            CaptureSymbol.VIDEO -> { path(3f,6f,16f,6f,16f,18f,3f,18f,3f,6f); path(16f,10f,22f,6f,22f,18f,16f,14f) }
            CaptureSymbol.EYE -> {
                val eye = Path().apply {
                    moveTo(2f*u,12f*u); quadraticTo(12f*u,-2f*u,22f*u,12f*u)
                    quadraticTo(12f*u,26f*u,2f*u,12f*u); close()
                }
                drawPath(eye, tint, style = Stroke(1.6f*u))
                circle(12f,12f,3f)
            }
            CaptureSymbol.STARS -> repeat(5) { i ->
                val star = Path()
                repeat(10) { point ->
                    val angle = -PI/2 + point*PI/5
                    val radius = if (point % 2 == 0) 2.5f else 1.1f
                    val x = (2.5f + i*4.75f + cos(angle).toFloat()*radius)*u
                    val y = (12f + sin(angle).toFloat()*radius)*u
                    if (point == 0) star.moveTo(x,y) else star.lineTo(x,y)
                }
                star.close()
                drawPath(star, tint)
            }
            CaptureSymbol.QUESTION_BUBBLE -> {
                path(3f,3f,21f,3f,21f,18f,10f,18f,5f,22f,5f,18f,3f,18f,3f,3f)
                path(9f,8f,10f,6f,14f,6f,15f,8f,12f,11f,12f,12f)
                line(12f,15f,12f,15.2f)
            }
            CaptureSymbol.FILMSTRIP -> {
                path(2f,3f,22f,3f,22f,21f,2f,21f,2f,3f)
                line(6f,3f,6f,21f); line(18f,3f,18f,21f)
                for (y in listOf(7f,12f,17f)) { line(2f,y,6f,y); line(18f,y,22f,y) }
                line(6f,12f,18f,12f)
            }
            CaptureSymbol.COINS -> {
                for (y in listOf(10f,15f,20f)) {
                    drawOval(tint, Offset(3f*u,(y-3f)*u), Size(13f*u,5f*u), style = Stroke(1.6f*u))
                }
                circle(17f,6f,4f)
            }
            CaptureSymbol.SCALES -> {
                line(12f,3f,12f,21f); line(7f,21f,17f,21f); line(3f,7f,21f,7f)
                path(6f,7f,2f,15f,10f,15f,6f,7f)
                path(18f,7f,14f,15f,22f,15f,18f,7f)
            }
            CaptureSymbol.INFO -> { circle(12f,12f,9f); line(12f,11f,12f,17f); line(12f,7f,12f,7.2f) }
            CaptureSymbol.ARROW_LEFT -> { path(10f,5f,3f,12f,10f,19f); line(3f,12f,21f,12f) }
            CaptureSymbol.ARROW_RIGHT -> { path(14f,5f,21f,12f,14f,19f); line(3f,12f,21f,12f) }
            CaptureSymbol.ARROW_DOWN -> { path(5f,14f,12f,21f,19f,14f); line(12f,3f,12f,21f) }
            CaptureSymbol.CLOCK -> { circle(12f,12f,9f); path(12f,6f,12f,12f,16f,14f) }
            CaptureSymbol.WALLET -> {
                path(3f,7f,19f,7f,19f,21f,3f,21f,3f,7f,17f,3f,17f,7f)
                path(19f,11f,14f,11f,14f,17f,21f,17f,21f,11f,19f,11f)
                line(17f,14f,17.2f,14f)
            }
            CaptureSymbol.RING -> circle(12f,12f,9f)
        }
    }
}

@Composable
fun StatPill(symbol: CaptureSymbol, value: String, description: String, onClick: () -> Unit) {
    Row(Modifier.heightIn(min = 48.dp).clip(CaptureRadii.pill).clickable(onClick = onClick)
        .semantics { contentDescription = description }.padding(vertical = 6.dp)
        .background(CaptureSurface2, CaptureRadii.pill).border(1.dp, CaptureOutline, CaptureRadii.pill)
        .padding(horizontal = 12.dp, vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        CaptureIcon(symbol, Modifier.size(16.dp))
        Text(value, style = MaterialTheme.typography.titleMedium, color = CaptureInk)
    }
}

@Composable
fun FloatingBottomBar(profile: Boolean, enabled: Boolean, onGallery: () -> Unit, onCapture: () -> Unit, onProfile: () -> Unit, modifier: Modifier = Modifier) {
    Box(modifier.widthIn(max = 360.dp).fillMaxWidth().padding(horizontal = 20.dp, vertical = 16.dp)) {
        Box(Modifier.matchParentSize().padding(top = 12.dp).captureGlass(CaptureRadii.pill, CaptureSurface2.copy(alpha = .85f)))
        Row(Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 8.dp),
            horizontalArrangement = Arrangement.SpaceAround, verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.clip(CaptureRadii.pill).clickable(onClick = onGallery).padding(8.dp), horizontalAlignment = Alignment.CenterHorizontally) {
                CaptureIcon(CaptureSymbol.GALLERY, Modifier.size(22.dp), MutedGray)
                Text("GALLERY", style = MaterialTheme.typography.labelSmall, color = MutedGray)
            }
            Box(Modifier.offset(y = (-8).dp).size(64.dp), contentAlignment = Alignment.Center) {
                Box(Modifier.matchParentSize().blur(12.dp).background(CaptureGradients.actionGlow, CaptureRadii.pill))
                Box(Modifier.size(56.dp).clip(CaptureRadii.pill).background(CaptureGradients.action)
                    .clickable(enabled = enabled, onClick = onCapture).semantics { contentDescription = "Capture" }, contentAlignment = Alignment.Center) {
                    CaptureIcon(CaptureSymbol.CAMERA, Modifier.size(26.dp), Color.White)
                }
            }
            Column(Modifier.clip(CaptureRadii.pill).background(if (profile) CaptureAccentTint else Color.Transparent)
                .clickable(onClick = onProfile).padding(8.dp), horizontalAlignment = Alignment.CenterHorizontally) {
                CaptureIcon(CaptureSymbol.PERSON, Modifier.size(22.dp), if (profile) CaptureAccent else MutedGray)
                Text("PROFILE", style = MaterialTheme.typography.labelSmall, color = MutedGray)
            }
        }
    }
}

@Composable
fun ActionButton(
    label: String, onClick: () -> Unit, modifier: Modifier = Modifier, enabled: Boolean = true,
    containerColor: Color = CaptureAction, contentColor: Color = Color.White,
    labelStyle: androidx.compose.ui.text.TextStyle = MaterialTheme.typography.titleMedium,
) {
    val haptic = LocalHapticFeedback.current
    Button(onClick = { haptic.performHapticFeedback(HapticFeedbackType.TextHandleMove); onClick() },
        modifier = modifier.fillMaxWidth().heightIn(min = CaptureDimensions.primaryButtonMinHeight)
            .widthIn(min = CaptureDimensions.touchTarget).captureFlatShadow(CaptureRadii.button),
        enabled = enabled, shape = CaptureRadii.button,
        border = BorderStroke(CaptureDimensions.inkBorder, CaptureInk),
        contentPadding = PaddingValues(horizontal = 12.dp, vertical = 12.dp),
        colors = ButtonDefaults.buttonColors(containerColor = containerColor, contentColor = contentColor,
            disabledContainerColor = ButtonGrey, disabledContentColor = ButtonGreyInk)) {
        Text(label, style = labelStyle)
    }
}

@Composable
fun SecondaryButton(label: String, onClick: () -> Unit, modifier: Modifier = Modifier, enabled: Boolean = true) {
    OutlinedButton(onClick = onClick, enabled = enabled,
        modifier = modifier.fillMaxWidth().heightIn(min = CaptureDimensions.primaryButtonMinHeight),
        shape = CaptureRadii.button, border = BorderStroke(CaptureDimensions.inkBorder, CaptureInk),
        colors = ButtonDefaults.outlinedButtonColors(contentColor = CaptureInk),
        contentPadding = PaddingValues(horizontal = 12.dp, vertical = 12.dp)) {
        Text(label, style = MaterialTheme.typography.titleMedium)
    }
}

@Composable
fun ComingSoonBadge(text: String = "Coming Soon") {
    Text(
        text,
        color = CaptureInk,
        style = MaterialTheme.typography.labelMedium,
        modifier = Modifier.clearAndSetSemantics { contentDescription = text }
            .clip(CaptureRadii.pill)
            .background(CaptureYellow)
            .border(2.dp, CaptureInk, CaptureRadii.pill)
            .padding(horizontal = 12.dp, vertical = 6.dp),
    )
}

@Composable
fun VerdictBadge(good: Boolean) {
    val tint = if (good) SuccessGreen else ErrorRed
    Text(if (good) "✓ GOOD" else "× BAD", color = tint, style = MaterialTheme.typography.labelMedium,
        modifier = Modifier.clip(CaptureRadii.pill).background(CaptureBase.copy(alpha = .9f))
            .border(1.dp, tint.copy(alpha = .6f), CaptureRadii.pill).padding(horizontal = 12.dp, vertical = 8.dp))
}

@Composable
fun ConfettiCanvas(modifier: Modifier = Modifier, startDelay: Int = 0) {
    var played by rememberSaveable { mutableStateOf(false) }
    var started by remember { mutableStateOf(false) }
    val progress = remember { Animatable(if (played) 1f else 0f) }
    LaunchedEffect(Unit) {
        if (!played) {
            kotlinx.coroutines.delay(startDelay.toLong())
            started = true
            progress.animateTo(1f, tween(CaptureMotion.celebrate - startDelay, easing = LinearEasing))
            played = true
        }
    }
    Canvas(modifier) {
        val t = progress.value
        if (!started || t >= 1f) return@Canvas
        repeat(40) { i ->
            val angle = i * 2.39996f
            val speed = size.minDimension * (.3f + (i % 7) * .07f)
            val p = center + Offset(cos(angle) * speed * t, sin(angle) * speed * t + size.height * .4f * t * t)
            val color = listOf(CaptureAccent, CaptureCoral, CaptureAccentDeep, SuccessGreen, CaptureInk)[i % 5].copy(alpha = 1f - t)
            rotate(i * 31f + t * 180f, p) { drawRect(color, p, Size(5.dp.toPx(), 9.dp.toPx())) }
        }
    }
}

/** Presentation-only route reveal; never recreates the camera or its state. */
@Composable
fun Modifier.captureScreenMotion(route: Any? = Unit): Modifier {
    val reveal = remember { Animatable(0f) }
    LaunchedEffect(route) {
        reveal.snapTo(0f)
        reveal.animateTo(1f, tween(CaptureMotion.base, easing = CaptureMotion.easing))
    }
    return this.graphicsLayer {
        alpha = reveal.value
        translationY = size.height * .04f * (1f - reveal.value)
    }
}

@Composable
fun Modifier.captureCardMotion(index: Int): Modifier {
    val reveal = remember { Animatable(0f) }
    LaunchedEffect(Unit) {
        kotlinx.coroutines.delay(index * 40L)
        reveal.animateTo(1f, tween(CaptureMotion.base, easing = CaptureMotion.easing))
    }
    return graphicsLayer { alpha = reveal.value; translationY = 12.dp.toPx() * (1f - reveal.value) }
}
