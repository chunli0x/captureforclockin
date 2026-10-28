package app.capture.seeker

import androidx.compose.foundation.layout.*
import androidx.compose.material3.Text
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import app.capture.*

/** Fixed geometry, duration, 1x speed and the same controls for both outputs. */
@Composable
fun VideoJudgmentScreen(s: PilotState, vm: SeekerViewModel) {
    val pair = s.judgePair ?: return
    val id = pair.optString("pair_id")
    var a by remember(id, pair) { mutableStateOf(VideoViewingState()) }
    var b by remember(id, pair) { mutableStateOf(VideoViewingState()) }
    val durationA = pair.optJSONObject("media_a")?.optLong("duration_ms", 0) ?: 0
    val durationB = pair.optJSONObject("media_b")?.optLong("duration_ms", 0) ?: 0
    val valid = durationA == 4000L && durationB == durationA
    val authorized = vm.videoAuthorization()
    val ready = valid && a.canSubmit(durationA) && b.canSubmit(durationB)
    Text("Compare robot vision video outputs", color = CaptureInk)
    if (valid && authorized != null) {
        for (label in listOf("a", "b")) {
            val path = pair.optString("output_${label}_url")
            val expires = pair.optJSONObject("media_$label")?.optLong("expires_at", 0) ?: 0
            if (path.startsWith("/media/deliver/") && !path.contains("#")) {
                VideoOutput(s.server.trimEnd('/') + path, authorized, label.uppercase(), durationA, expires,
                    Modifier.fillMaxWidth(), onState = { if (label == "a") a = it else b = it })
            } else {
                Text("Video unavailable", color = MutedGray)
            }
        }
    } else {
        Text("Video unavailable", color = MutedGray)
    }
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        ActionButton("A", onClick = { vm.submitJudgment(choice = "a") }, enabled = ready && !s.busy,
            modifier = Modifier.weight(1f))
        ActionButton("B", onClick = { vm.submitJudgment(choice = "b") }, enabled = ready && !s.busy,
            modifier = Modifier.weight(1f))
    }
    SecondaryButton("Can't tell", onClick = { vm.submitJudgment(choice = "cant_tell") }, enabled = ready && !s.busy)
    SecondaryButton("Reload this pair", onClick = { vm.judgeNext() }, enabled = !s.busy)
    SecondaryButton("Report playback problem and skip", onClick = { vm.skipUnavailablePair() }, enabled = !s.busy)
    Text("Technical skips are unpaid and do not affect agreement.", color = MutedGray)
}
