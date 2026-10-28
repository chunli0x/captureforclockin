package app.capture.seeker

import androidx.compose.foundation.layout.*
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp

@Composable
fun OperationError(s: PilotState, onRetry: () -> Unit) {
    s.error?.let { error ->
        CapturePanel(Modifier.fillMaxWidth()) {
            Text(error, color = MaterialTheme.colorScheme.error)
            Text("Refresh to check the latest status, then retry the action or recover a pending transaction.",
                style = MaterialTheme.typography.bodySmall)
            SecondaryButton("Retry refresh", onClick = onRetry, enabled = !s.busy)
        }
    }
}

@Composable
fun PendingTransactions(s: PilotState, vm: SeekerViewModel) {
    if (s.pendingTransactions.isNotEmpty()) {
        CapturePanel(Modifier.fillMaxWidth()) {
            Text("Pending bond transactions", style = MaterialTheme.typography.titleMedium)
            Text("Check whether your wallet transaction finalized before trying again.",
                style = MaterialTheme.typography.bodySmall)
            s.pendingTransactions.forEach { pending ->
                Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    Text("${pending.action} · ${pending.id.take(12)}", style = MaterialTheme.typography.bodySmall)
                    SecondaryButton("Recover ${pending.action}", onClick = { vm.recover(pending.id) }, enabled = !s.busy)
                }
            }
        }
    }
}
