package app.capture.seeker

import app.capture.*
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.background
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.ui.draw.clip
import androidx.compose.ui.layout.ContentScale
import coil3.compose.AsyncImage

/** Formats \$SKR base units with six-decimal precision for pay rates (0.005 \$SKR). */
internal fun formatPaySkr(units: Long): String =
    "%.6f".format(units / 1_000_000.0).trimEnd('0').trimEnd('.')

/**
 * The judgment product screen: a judge compares two model outputs (A/B) or rates
 * one 1-5, with a first-class "can't tell". Earns test $SKR; bond is test $SKR.
 */
@Composable
fun JudgmentScreen(s: PilotState, vm: SeekerViewModel, onBond: () -> Unit, onProveHuman: () -> Unit) {
    NavyBackground(Modifier.fillMaxSize().captureScreenMotion()) {
        Column(
            Modifier
                .fillMaxSize()
                .safeDrawingPadding()
                .verticalScroll(rememberScrollState())
                .padding(24.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.spacedBy(16.dp),
        ) {
            EditorialHeading("JUDGE.")
            OperationError(s, onRetry = vm::refresh)
            // Liveness is an account prerequisite, independent of pair availability.
            // Put it before the guidance/stats so it is visible on a phone at sign-in.
            if (!s.livenessCompleted) {
                CapturePanel(Modifier.fillMaxWidth(), elevated = true) {
                    Text("Prove you're human", style = MaterialTheme.typography.titleMedium, color = CaptureInk)
                    Text(
                        "A quick on-device motion check. Images are processed on-device and not retained.",
                        style = MaterialTheme.typography.bodySmall,
                        color = MutedGray,
                    )
                    ActionButton("Prove you're human", onClick = onProveHuman, enabled = !s.busy)
                }
                return@Column
            }
            JudgeGuidanceCard()
            Text(
                "Compare two model outputs and pick the better one. Consensus-aligned judges earn " +
                    "test \$SKR on devnet.",
                style = MaterialTheme.typography.bodyMedium,
                color = MutedGray,
                textAlign = TextAlign.Center,
            )
            JudgeStatsCard(s)
            if (s.judgePair == null) {
                EmptyCard(s, vm, onBond)
            } else {
                PairCard(s, vm)
            }
        }
    }
}

/** Guidance only: reading these beats never gates an answer. */
@Composable
private fun JudgeGuidanceCard() {
    CapturePanel(Modifier.fillMaxWidth()) {
        val beats = listOf(
            "Read" to "Review the task and both outputs.",
            "Compare" to "Look closely at both outputs.",
            "Choose" to "Pick the more natural output.",
        )
        beats.forEachIndexed { index, (title, detail) ->
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                Box(Modifier.size(28.dp).background(CaptureYellow, CaptureRadii.pill), contentAlignment = Alignment.Center) {
                    Text("${index + 1}", style = MaterialTheme.typography.titleMedium, color = CaptureInk)
                }
                Column(Modifier.weight(1f)) {
                    Text(title, style = MaterialTheme.typography.titleMedium, color = CaptureInk)
                    Text(detail, style = MaterialTheme.typography.bodySmall, color = MutedGray)
                }
            }
        }
    }
}

@Composable
private fun JudgeStatsCard(s: PilotState) {
    CapturePanel(Modifier.fillMaxWidth()) {
        Text("YOUR JUDGE STATS", style = MaterialTheme.typography.labelMedium, color = CaptureAccentBright)
        Text(
            "Level ${s.judgeLevelName} · ${agreementPercent(s.judgeAgreementRate)}% agreement · ×${"%.2f".format(s.judgeRateMultiplier)} rate",
            style = MaterialTheme.typography.titleMedium,
            color = CaptureInk,
        )
        Text(
            "${s.judgeJudgedCount} judged · ${formatToken(s.judgeStakedSkr)} \$SKR staked · ${s.judgeWarnings} warnings",
            style = MaterialTheme.typography.bodySmall,
            color = MutedGray,
        )
        Text(
            "Earn ${formatPaySkr((s.judgeBaseSkr * s.judgeRateMultiplier).toLong())} \$SKR per aligned answer " +
                "(${formatPaySkr(s.judgeBaseSkr)} base × ${"%.2f".format(s.judgeRateMultiplier)}) · " +
                "${formatPaySkr(s.judgeFlatSkr)} \$SKR for can't tell",
            style = MaterialTheme.typography.bodySmall,
            color = CaptureInk,
        )
    }
}

@Composable
private fun EmptyCard(s: PilotState, vm: SeekerViewModel, onBond: () -> Unit) {
    CapturePanel(Modifier.fillMaxWidth(), elevated = true) {
        when {
            !s.bonded -> {
                Text("No pairs to judge right now", style = MaterialTheme.typography.titleMedium, color = CaptureInk)
                ActionButton("Deposit the SKR bond", onClick = onBond)
                SecondaryButton("Refresh", onClick = { vm.judgeNext() }, enabled = !s.busy)
            }
            else -> {
                Text("No pairs to judge right now", style = MaterialTheme.typography.titleMedium, color = CaptureInk)
                SecondaryButton("Refresh", onClick = { vm.judgeNext() }, enabled = !s.busy)
            }
        }
    }
}

@Composable
private fun PairCard(s: PilotState, vm: SeekerViewModel) {
    CapturePanel(Modifier.fillMaxWidth(), elevated = true) {
        val pair = s.judgePair
        if (pair == null) {
            Text("No pair loaded", style = MaterialTheme.typography.bodyMedium, color = MutedGray)
        } else {
            Text("TASK", style = MaterialTheme.typography.labelMedium, color = CaptureAccentBright)
            pair.optString("rubric", "").takeIf { it.isNotBlank() }?.let {
                Text(it, style = MaterialTheme.typography.bodyMedium, color = CaptureInk)
            }
            if (pair.optString("media_type", "image") == "video") {
                VideoJudgmentScreen(s, vm)
            } else {
                PairChoice(s, vm)
            }
        }
    }
}

@Composable
private fun PairChoice(s: PilotState, vm: SeekerViewModel) {
    val pair = s.judgePair ?: return
    val taskType = pair.optString("task_type", "preference")
    val required = if (taskType == "rating") 1 else 2
    var okCount by remember(pair) { mutableStateOf(0) }
    var failCount by remember(pair) { mutableStateOf(0) }
    val report: (Boolean) -> Unit = { if (it) okCount++ else failCount++ }
    val imagesReady = failCount == 0 && okCount >= required

    if (taskType == "rating") {
        Text("Is this photo realistic?", style = MaterialTheme.typography.titleMedium, color = CaptureInk)
        Text("Rate it 1–5. 1 = bad, 5 = good.", style = MaterialTheme.typography.bodySmall, color = MutedGray)
        OutputImage(pair.optString("output_a_url", ""), "Output", Modifier.fillMaxWidth(), report)
    } else {
        Text("Which photo is more realistic?", style = MaterialTheme.typography.titleMedium, color = CaptureInk)
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            OutputImage(pair.optString("output_a_url", ""), "A", Modifier.weight(1f), report)
            OutputImage(pair.optString("output_b_url", ""), "B", Modifier.weight(1f), report)
        }
    }
    if (failCount > 0) {
        SecondaryButton("Couldn't load the image, try another", onClick = { vm.skipUnavailablePair() }, enabled = !s.busy)
    }
    if (taskType == "rating") {
        FlowRow(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp)) {
            for (r in 1..5) {
                ActionButton("$r", onClick = { vm.submitJudgment(rating = r) },
                    enabled = !s.busy && imagesReady, modifier = Modifier.width(48.dp))
            }
        }
    } else {
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            ActionButton("A", onClick = { vm.submitJudgment(choice = "a") },
                enabled = !s.busy && imagesReady, modifier = Modifier.weight(1f))
            ActionButton("B", onClick = { vm.submitJudgment(choice = "b") },
                enabled = !s.busy && imagesReady, modifier = Modifier.weight(1f))
        }
    }
    Column(Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text("Unsure? Choose this.", style = MaterialTheme.typography.bodySmall, color = MutedGray,
            modifier = Modifier.fillMaxWidth(), textAlign = TextAlign.Center)
        SecondaryButton("Can't tell", onClick = { vm.submitJudgment(choice = "cant_tell") },
            enabled = !s.busy && imagesReady)
    }
}

@Composable
private fun OutputImage(url: String, label: String, modifier: Modifier = Modifier, onState: (Boolean) -> Unit = {}) {
    Column(modifier, horizontalAlignment = Alignment.CenterHorizontally) {
        Text(label, style = MaterialTheme.typography.labelMedium, color = CaptureAccentBright)
        if (url.isBlank()) {
            LaunchedEffect(url) { onState(false) }
            Text("No output", style = MaterialTheme.typography.bodySmall, color = MutedGray)
        } else {
            AsyncImage(
                model = url,
                contentDescription = "Output $label",
                contentScale = ContentScale.Crop,
                onSuccess = { onState(true) },
                onError = { onState(false) },
                modifier = Modifier
                    .fillMaxWidth()
                    .aspectRatio(1f)
                    .clip(RoundedCornerShape(12.dp)),
            )
        }
    }
}
