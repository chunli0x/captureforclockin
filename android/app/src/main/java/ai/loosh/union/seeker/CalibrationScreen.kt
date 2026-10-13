package ai.loosh.union.seeker

import ai.loosh.union.CaptureAccent
import ai.loosh.union.CaptureBase
import ai.loosh.union.CaptureCoral
import ai.loosh.union.CaptureInk
import ai.loosh.union.CaptureRadii
import ai.loosh.union.CaptureYellow
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import java.util.Locale

/** One informational route: judging examples, live pay rates, and personal standing. */
@Composable
fun CalibrationScreen(s: PilotState, vm: SeekerViewModel, onBack: () -> Unit) {
    Column(
        Modifier.fillMaxSize().background(CaptureBase).captureScreenMotion()
            .safeDrawingPadding().verticalScroll(rememberScrollState()),
    ) {
        CalibrationAppBar(onBack)
        Column(
            Modifier.widthIn(max = 640.dp).fillMaxWidth().align(Alignment.CenterHorizontally)
                .padding(20.dp),
            verticalArrangement = Arrangement.spacedBy(24.dp),
        ) {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                EditorialHeading("CALIBRATE.", Modifier.semantics { heading() })
                Text(
                    "How to judge, what it pays, and where you stand.",
                    style = MaterialTheme.typography.bodyLarge,
                    color = CaptureInk,
                )
            }
            ExampleCards(s)
            VideoTier()
            PayCard(s)
            Row(
                Modifier.align(Alignment.CenterHorizontally),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text("Scroll for your standing", style = MaterialTheme.typography.bodyMedium, color = CaptureInk)
                CaptureIcon(CaptureSymbol.ARROW_DOWN, Modifier.size(24.dp), CaptureInk)
            }
            StandingCard(s)
            Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                OutlinedButton(
                    onClick = { vm.refresh() },
                    enabled = !s.busy,
                    shape = CaptureRadii.pill,
                    border = BorderStroke(2.dp, CaptureInk),
                    colors = ButtonDefaults.outlinedButtonColors(contentColor = CaptureInk),
                    modifier = Modifier.fillMaxWidth().heightIn(min = 56.dp),
                ) { Text("Refresh", style = MaterialTheme.typography.titleMedium) }
                ActionButton(
                    "Back to judging", onBack,
                    containerColor = CaptureAccent, contentColor = CaptureInk,
                )
            }
        }
    }
}

@Composable
private fun CalibrationAppBar(onBack: () -> Unit) {
    Row(
        Modifier.fillMaxWidth().padding(horizontal = 8.dp, vertical = 12.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(4.dp),
    ) {
        IconButton(onClick = onBack) {
            CaptureIcon(CaptureSymbol.ARROW_LEFT, Modifier.size(24.dp), CaptureInk, "Back to judging")
        }
        Row(Modifier.weight(1f), verticalAlignment = Alignment.CenterVertically) {
            CaptureMark(Modifier.size(28.dp))
            Text(
                "CAPTURE", color = CaptureInk,
                style = MaterialTheme.typography.displaySmall.copy(fontSize = 20.sp),
            )
        }
        Text(
            "DEVNET · TEST \$SKR",
            style = MaterialTheme.typography.labelSmall,
            color = CaptureInk,
            textAlign = TextAlign.Center,
            modifier = Modifier.widthIn(max = 144.dp)
                .background(CaptureYellow, CaptureRadii.pill)
                .border(1.dp, CaptureInk, CaptureRadii.pill)
                .padding(horizontal = 10.dp, vertical = 8.dp),
        )
    }
    HorizontalDivider(color = CaptureInk)
}

@Composable
private fun SectionBanner(title: String) {
    Text(
        title,
        style = MaterialTheme.typography.titleLarge,
        color = Color.White,
        modifier = Modifier.fillMaxWidth().background(CaptureAccent)
            .padding(horizontal = 16.dp, vertical = 12.dp).semantics { heading() },
    )
}

@Composable
private fun CalibrationSection(title: String, content: @Composable ColumnScope.() -> Unit) {
    Column(
        Modifier.fillMaxWidth().clip(CaptureRadii.md)
            .border(2.dp, CaptureInk, CaptureRadii.md),
    ) {
        SectionBanner(title)
        Column(
            Modifier.padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp),
            content = content,
        )
    }
}

@Composable
private fun TopicHeading(number: Int, title: String, symbol: CaptureSymbol, tint: Color = CaptureInk) {
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
        Text(
            "$number", color = CaptureInk, style = MaterialTheme.typography.titleMedium,
            textAlign = TextAlign.Center,
            modifier = Modifier.background(CaptureYellow, CaptureRadii.pill)
                .border(1.dp, CaptureInk, CaptureRadii.pill).padding(horizontal = 12.dp, vertical = 6.dp),
        )
        CaptureIcon(symbol, Modifier.size(32.dp), tint)
        Text(title, style = MaterialTheme.typography.headlineSmall, color = CaptureInk)
    }
}

@Composable
private fun ExampleCards(s: PilotState) {
    Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Box(Modifier.clip(CaptureRadii.sm).border(2.dp, CaptureInk, CaptureRadii.sm)) {
            SectionBanner("HOW TO JUDGE")
        }
        CapturePanel(Modifier.fillMaxWidth(), elevated = true) {
            TopicHeading(1, "Preference", CaptureSymbol.EYE)
            Text("Pick the better output.", style = MaterialTheme.typography.bodyLarge, color = CaptureInk)
            Box(Modifier.fillMaxWidth()) {
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                    OutputBox("A", "Output A", Modifier.weight(1f))
                    OutputBox("B", "Output B", Modifier.weight(1f))
                }
                Row(
                    Modifier.align(Alignment.Center).background(CaptureYellow, CaptureRadii.pill)
                        .border(1.dp, CaptureInk, CaptureRadii.pill).padding(horizontal = 10.dp, vertical = 8.dp),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(6.dp),
                ) {
                    CaptureIcon(CaptureSymbol.ARROW_LEFT, Modifier.size(20.dp), CaptureAccent)
                    Text("Compare both", style = MaterialTheme.typography.labelMedium, color = CaptureInk)
                    CaptureIcon(CaptureSymbol.ARROW_RIGHT, Modifier.size(20.dp), CaptureAccent)
                }
            }
        }
        CapturePanel(Modifier.fillMaxWidth(), elevated = true) {
            TopicHeading(2, "Rating", CaptureSymbol.STARS)
            Text("Score one output 1–5.", style = MaterialTheme.typography.bodyLarge, color = CaptureInk)
            // Illustrations of the rating controls; this page never submits a judgment.
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                for (rating in 1..5) {
                    OutlinedButton(
                        onClick = {}, enabled = false,
                        modifier = Modifier.weight(1f).heightIn(min = 48.dp),
                        shape = CaptureRadii.pill,
                        border = BorderStroke(2.dp, CaptureInk),
                        contentPadding = PaddingValues(vertical = 12.dp),
                        colors = ButtonDefaults.outlinedButtonColors(
                            disabledContainerColor = CaptureBase, disabledContentColor = CaptureInk,
                        ),
                    ) { Text("$rating", style = MaterialTheme.typography.titleMedium) }
                }
            }
        }
        CapturePanel(Modifier.fillMaxWidth(), elevated = true) {
            TopicHeading(3, "Can't tell", CaptureSymbol.QUESTION_BUBBLE, CaptureCoral)
            Text(
                "Always an option. Pays ${formatPaySkr(s.judgeFlatSkr)} \$SKR and reroutes the pair.",
                style = MaterialTheme.typography.bodyLarge, color = CaptureInk,
            )
        }
    }
}

@Composable
private fun OutputBox(label: String, caption: String, modifier: Modifier = Modifier) {
    Column(
        modifier.heightIn(min = 156.dp).background(CaptureBase, CaptureRadii.sm)
            .border(2.dp, CaptureInk, CaptureRadii.sm).padding(12.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.SpaceBetween,
    ) {
        Text(label, style = MaterialTheme.typography.displaySmall, color = CaptureInk)
        Text(caption, style = MaterialTheme.typography.bodyMedium, color = CaptureInk)
    }
}

@Composable
private fun VideoTier() {
    Column(Modifier.clip(CaptureRadii.md).border(2.dp, CaptureInk, CaptureRadii.md)) {
        Column(
            Modifier.fillMaxWidth().background(CaptureCoral).padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                CaptureIcon(CaptureSymbol.FILMSTRIP, Modifier.size(28.dp), CaptureInk)
                Text(
                    "VIDEO / FRAME SEQUENCE", style = MaterialTheme.typography.titleLarge,
                    color = CaptureInk, modifier = Modifier.weight(1f).semantics { heading() },
                )
            }
            ComingSoonBadge("COMING SOON")
        }
        Text(
            "Judge a short motion clip instead of a still frame. The next tier.",
            style = MaterialTheme.typography.bodyLarge, color = CaptureInk,
            modifier = Modifier.padding(16.dp),
        )
    }
}

@Composable
private fun PayRow(symbol: CaptureSymbol, text: String, tint: Color = CaptureInk) {
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        CaptureIcon(symbol, Modifier.size(28.dp), tint)
        Text(text, style = MaterialTheme.typography.bodyLarge, color = CaptureInk, modifier = Modifier.weight(1f))
    }
}

@Composable
private fun PayCard(s: PilotState) {
    CalibrationSection("WHAT YOU EARN") {
        PayRow(CaptureSymbol.COINS, "Aligned judgment")
        Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(
                "${formatPaySkr(s.judgeBaseSkr)} × ${String.format(Locale.US, "%.2f", s.judgeRateMultiplier)} = " +
                    "${formatPaySkr((s.judgeBaseSkr * s.judgeRateMultiplier).toLong())}",
                style = MaterialTheme.typography.headlineMedium, color = CaptureInk,
            )
            Text("\$SKR", style = MaterialTheme.typography.labelMedium, color = CaptureInk)
        }
        Text(
            "Multiplier applies to aligned judgments: ×1.00 = base pay; ×2.00 = double.",
            style = MaterialTheme.typography.bodyMedium, color = CaptureInk,
        )
        HorizontalDivider(color = CaptureInk)
        PayRow(CaptureSymbol.SCALES, "Disagreement pays 0 \$SKR")
        PayRow(CaptureSymbol.QUESTION_BUBBLE, "\"Can't tell\": ${formatPaySkr(s.judgeFlatSkr)} \$SKR flat", CaptureCoral)
        HorizontalDivider(color = CaptureInk)
        PayRow(CaptureSymbol.INFO, "Paid in test \$SKR on devnet, not real value.")
    }
}

@Composable
private fun StandingCard(s: PilotState) {
    CalibrationSection("YOUR STANDING") {
        Column(Modifier.fillMaxWidth(), horizontalAlignment = Alignment.CenterHorizontally) {
            Text(
                "${agreementPercent(s.judgeAgreementRate)}%",
                style = MaterialTheme.typography.displayLarge.copy(fontSize = 64.sp, lineHeight = 72.sp),
                color = CaptureInk,
            )
            Text("Agreement with the crowd", style = MaterialTheme.typography.bodyLarge, color = CaptureInk)
        }
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            Metric("Judged", "${s.judgeJudgedCount}", Modifier.weight(1f))
            Metric("Level", s.judgeLevelName, Modifier.weight(1f))
        }
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            Metric("Earned", "${formatToken(s.judgeEarnedSkr)} \$SKR", Modifier.weight(1f))
            Metric("Warnings", "${s.judgeWarnings}", Modifier.weight(1f))
        }
    }
}

@Composable
private fun Metric(label: String, value: String, modifier: Modifier = Modifier) {
    CapturePanel(modifier, elevated = true) {
        Text(label, style = MaterialTheme.typography.labelMedium, color = CaptureInk)
        Text(value, style = MaterialTheme.typography.titleLarge, color = CaptureInk)
    }
}
