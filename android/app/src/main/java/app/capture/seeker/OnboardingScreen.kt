package app.capture.seeker

import app.capture.CaptureAccentBright
import app.capture.CaptureBaseDeep
import app.capture.CaptureInk
import app.capture.CaptureOutline
import app.capture.CaptureRadii
import app.capture.CaptureSurface2
import app.capture.MutedGray
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp

/**
 * First-run onboarding: what judging is, what the judge does, and what they
 * earn. Shown once before sign-in; "Get started" proceeds and never shows
 * again.
 */
@Composable
fun OnboardingScreen(onDone: () -> Unit) {
    NavyBackground(Modifier.fillMaxSize().captureScreenMotion()) {
        Column(
            Modifier
                .fillMaxSize()
                .safeDrawingPadding()
                .verticalScroll(rememberScrollState())
                .padding(24.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp),
        ) {
            CaptureWordmark()
            EditorialHeading("HOW IT WORKS.")
            Text(
                "I judge a vision model's outputs. When I agree with the crowd, I earn test \$SKR on devnet.",
                style = MaterialTheme.typography.bodyMedium,
                color = CaptureInk,
                textAlign = TextAlign.Start,
            )
            FlowCard()
            ExampleCard()
            EarningsCard()
            JudgeTierLadder(currentLevel = 0)
            ActionButton("Get started", onDone)
        }
    }
}

@Composable
private fun FlowCard() {
    CapturePanel(Modifier.fillMaxWidth()) {
        Text("WHAT YOU DO", style = MaterialTheme.typography.labelMedium, color = CaptureAccentBright)
        Step("1. Stake", "Deposit a slashable test SKR bond.")
        Step("2. Judge", "Compare two outputs (A/B) or rate one 1-5.")
        Step("3. Earn", "Aligned judgments pay. Dissenters earn nothing.")
    }
}

@Composable
private fun Step(title: String, body: String) {
    Column(Modifier.padding(vertical = 4.dp)) {
        Text(title, style = MaterialTheme.typography.titleSmall, color = CaptureInk)
        Text(body, style = MaterialTheme.typography.bodySmall, color = MutedGray)
    }
}

@Composable
private fun ExampleCard() {
    CapturePanel(Modifier.fillMaxWidth()) {
        Text("EXAMPLES", style = MaterialTheme.typography.labelMedium, color = CaptureAccentBright)
        Text("Preference: pick the better output", style = MaterialTheme.typography.titleSmall, color = CaptureInk)
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            OutputBox("A", "Output A", Modifier.weight(1f))
            OutputBox("B", "Output B", Modifier.weight(1f))
        }
        Text("Rating: score one output 1-5", style = MaterialTheme.typography.titleSmall, color = CaptureInk)
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            for (r in 1..5) {
                Text(
                    "$r",
                    style = MaterialTheme.typography.titleMedium,
                    color = CaptureInk,
                    textAlign = TextAlign.Center,
                    modifier = Modifier
                        .weight(1f)
                        .background(CaptureSurface2, CaptureRadii.pill)
                        .border(1.dp, CaptureOutline, CaptureRadii.pill)
                        .padding(vertical = 10.dp),
                )
            }
        }
        Text(
            "\"Can't tell\" is always an option. It pays a small flat and reroutes the pair; you are never forced to guess.",
            style = MaterialTheme.typography.bodySmall,
            color = MutedGray,
        )
        Text(
            "Video / frame sequence, coming soon.",
            style = MaterialTheme.typography.labelSmall,
            color = MutedGray,
        )
    }
}

@Composable
private fun OutputBox(label: String, caption: String, modifier: Modifier = Modifier) {
    Column(
        modifier
            .background(CaptureBaseDeep, CaptureRadii.md)
            .border(2.dp, CaptureOutline, CaptureRadii.md)
            .padding(12.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text(label, style = MaterialTheme.typography.headlineSmall, color = CaptureInk)
        Text(caption, style = MaterialTheme.typography.labelSmall, color = MutedGray)
    }
}

@Composable
private fun EarningsCard() {
    CapturePanel(Modifier.fillMaxWidth()) {
        Text("WHAT YOU EARN", style = MaterialTheme.typography.labelMedium, color = CaptureAccentBright)
        Text(
            "I earn 0.02 \$SKR per aligned judgment, times my level multiplier (up to ×2.00).",
            style = MaterialTheme.typography.bodySmall,
            color = CaptureInk,
        )
        Text(
            "Dissent pays 0 \$SKR. \"Can't tell\" pays a flat 0.005 \$SKR.",
            style = MaterialTheme.typography.bodySmall,
            color = CaptureInk,
        )
        Text(
            "Test \$SKR on devnet, not real value.",
            style = MaterialTheme.typography.labelSmall,
            color = MutedGray,
        )
    }
}
