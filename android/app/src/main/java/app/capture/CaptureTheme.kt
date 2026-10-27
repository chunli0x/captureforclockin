package app.capture

import androidx.compose.animation.core.CubicBezierEasing
import androidx.compose.animation.core.FastOutSlowInEasing
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.Font
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.em
import androidx.compose.ui.unit.sp

// Capture "POP EDITORIAL": cream canvas, ink outlines, blue actions,
// coral highlights and yellow feature-availability badges.

val CaptureBaseDeep = Color(0xFFF6E9C7)
val CaptureBase = Color(0xFFFFF3D6)
val CaptureSurface = Color(0xFFFFFFFF)
val CaptureSurface2 = Color(0xFFFFF8E8)
val CaptureSurface3 = CaptureBaseDeep
val CaptureInk = Color(0xFF181626)
val CaptureOnBase = CaptureInk
val CaptureMutedText = Color(0xFF52515F)
val MutedGray = CaptureMutedText
val CaptureInkFaint = Color(0xFF9CA3AF)
val CaptureOutline = CaptureInk
val CaptureOutlineVariant = CaptureInk.copy(alpha = .25f)
val CaptureAccent = Color(0xFF3B82F6)
val CaptureAccentBright = CaptureAccent
val CaptureAction = Color(0xFF005FEA)
val CaptureAccentDeep = Color(0xFF1D4ED8)
val CaptureAccentTint = CaptureAccent.copy(alpha = .12f)
val CaptureAccentBorder = CaptureAccent.copy(alpha = .35f)
val CaptureCoral = Color(0xFFF26762)
val CaptureYellow = Color(0xFFFFD75E)
val CaptureYellowInk = CaptureInk
val SuccessGreen = Color(0xFF147D42)
val SuccessGreenBg = Color(0xFFE8F5EC)
val ErrorRed = Color(0xFFB42318)
val ErrorRedBg = Color(0xFFFEE4E2)
val CaptureRed = ErrorRed
val CaptureRedBg = ErrorRedBg
// Warnings have an amber treatment separate from feature availability.
val WarnAmber = Color(0xFF854D0E)
val WarnAmberBg = Color(0xFFFEF3C7)
// Button grey (secondary / disabled CTA).
val ButtonGrey = Color(0xFFE5E7EB)
val ButtonGreyInk = Color(0xFF374151)

object CaptureGradients {
    val navy = Brush.verticalGradient(listOf(CaptureBaseDeep, CaptureBase))
    val action = Brush.verticalGradient(listOf(CaptureAction, CaptureAccentDeep))
    val actionGlow = Brush.radialGradient(listOf(CaptureAccentBright.copy(alpha = .18f), CaptureAccent.copy(alpha = .02f)))
    val ring = Brush.sweepGradient(listOf(CaptureAccentBright, CaptureAccent, CaptureAccentDeep, CaptureAccentBright))
    val glass = Brush.verticalGradient(listOf(CaptureInk.copy(alpha = .05f), CaptureInk.copy(alpha = .02f)))
}
object CaptureMotion {
    const val fast = 120
    const val base = 220
    const val slow = 360
    const val celebrate = 700
    val easing = FastOutSlowInEasing
    val progressEasing = CubicBezierEasing(0.2f, 0f, 1f, 1f)
}
object CaptureRadii {
    val sm = RoundedCornerShape(12.dp)
    val md = RoundedCornerShape(20.dp)
    val lg = RoundedCornerShape(20.dp)
    val xl = RoundedCornerShape(24.dp)
    val button = RoundedCornerShape(28.dp)
    val pill = RoundedCornerShape(50)
}
object CaptureDimensions {
    val inkBorder = 2.dp
    val shadowX = 3.dp
    val shadowY = 4.dp
    val primaryButtonMinHeight = 52.dp
    val touchTarget = 48.dp
}
val CaptureColors = lightColorScheme(
    primary = CaptureAction, onPrimary = Color.White,
    primaryContainer = CaptureAccentTint, onPrimaryContainer = CaptureInk,
    secondary = CaptureAccentDeep, onSecondary = Color.White,
    secondaryContainer = CaptureSurface2, onSecondaryContainer = CaptureInk,
    tertiary = CaptureAccent, onTertiary = Color.White,
    tertiaryContainer = CaptureSurface3, onTertiaryContainer = CaptureInk,
    background = CaptureBase, onBackground = CaptureInk,
    surface = CaptureSurface, onSurface = CaptureInk,
    surfaceVariant = CaptureSurface2, onSurfaceVariant = MutedGray,
    surfaceDim = CaptureBaseDeep, surfaceBright = CaptureSurface,
    surfaceContainerLowest = CaptureBaseDeep, surfaceContainerLow = CaptureSurface,
    surfaceContainer = CaptureSurface, surfaceContainerHigh = CaptureSurface2,
    surfaceContainerHighest = CaptureSurface3,
    outline = CaptureOutline, outlineVariant = CaptureOutlineVariant,
    error = ErrorRed, onError = Color.White,
    errorContainer = ErrorRedBg, onErrorContainer = ErrorRed,
)
val Anton = FontFamily(Font(R.font.anton_regular, FontWeight.Normal))
val Inter = FontFamily(
    Font(R.font.inter_400, FontWeight.Normal),
    Font(R.font.inter_500, FontWeight.Medium),
    Font(R.font.inter_600, FontWeight.SemiBold),
    Font(R.font.inter_700, FontWeight.Bold),
    Font(R.font.inter_800, FontWeight.ExtraBold),
)
private fun inter(size: Int, line: Float, weight: FontWeight = FontWeight.Normal, tracking: Float = 0f) = TextStyle(
    fontFamily = Inter, fontWeight = weight, fontSize = size.sp,
    lineHeight = (size * line).sp, letterSpacing = tracking.em,
)
val CaptureNumeric = inter(28, 1f, FontWeight.Bold).copy(fontFeatureSettings = "tnum")
val CaptureTypography = Typography(
    displayLarge = inter(40, 1f, tracking = -.01f).copy(fontFamily = Anton),
    displayMedium = inter(34, 1.05f, tracking = -.01f).copy(fontFamily = Anton),
    displaySmall = inter(28, 1.1f, tracking = -.005f).copy(fontFamily = Anton),
    headlineLarge = inter(28, 1.1f, tracking = -.005f).copy(fontFamily = Anton),
    headlineMedium = inter(22, 1.15f, FontWeight.Bold),
    headlineSmall = inter(17, 1.25f, FontWeight.Bold),
    titleLarge = inter(16, 1.3f, FontWeight.SemiBold),
    titleMedium = inter(15, 1.35f, FontWeight.SemiBold),
    titleSmall = inter(15, 1.35f, FontWeight.SemiBold),
    bodyLarge = inter(15, 1.5f), bodyMedium = inter(13, 1.5f), bodySmall = inter(13, 1.5f),
    labelLarge = inter(11, 1.2f, FontWeight.SemiBold, .08f),
    labelMedium = inter(11, 1.2f, FontWeight.SemiBold, .08f),
    labelSmall = inter(11, 1.3f, FontWeight.Medium, .02f),
)
@Composable
fun CaptureTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = CaptureColors, typography = CaptureTypography,
        shapes = Shapes(CaptureRadii.sm, CaptureRadii.md, CaptureRadii.lg, CaptureRadii.xl, CaptureRadii.xl), content = content)
}
