package app.capture.seeker

import app.capture.CaptureAccentBright
import app.capture.CaptureInk
import app.capture.MutedGray
import app.capture.SuccessGreen
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import com.solana.mobilewalletadapter.clientlib.ActivityResultSender

/**
 * The bond gate for the judgment product: deposit the minimum test $SKR stake
 * before judging. The bond is a real on-chain escrow, slashed only for
 * systematic disagreement with the consensus ladder.
 */
@Composable
fun BondScreen(
    s: PilotState,
    vm: SeekerViewModel,
    sender: ActivityResultSender,
    onBack: () -> Unit,
) {
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
            CaptureWordmark()
            Text("Stake to judge", style = MaterialTheme.typography.displaySmall, color = CaptureInk)
            Text(
                "I deposit a test \$SKR bond with Seeker Wallet, a real on-chain escrow. It is " +
                    "slashed only for systematic disagreement; withdraw what remains later.",
                style = MaterialTheme.typography.bodyMedium,
                color = MutedGray,
                textAlign = TextAlign.Center,
            )
            BondCard(s, vm, sender)
            if (s.bonded)
                Button(onClick = onBack, enabled = !s.busy, modifier = Modifier.fillMaxWidth()) {
                    Text("Back to judging")
                }
        }
    }
}

@Composable
private fun BondCard(s: PilotState, vm: SeekerViewModel, sender: ActivityResultSender) {
    CapturePanel(Modifier.fillMaxWidth(), elevated = true) {
        Text(
            "DEPOSIT THE MINIMUM TEST \$SKR BOND",
            style = MaterialTheme.typography.labelMedium,
            color = CaptureAccentBright,
        )
        Text(
            "Your tokens are transferred into the contract. A warned bond is frozen from " +
                "withdrawal; a second strike slashes the full bond to a penalty vault.",
            style = MaterialTheme.typography.bodySmall,
            color = MutedGray,
        )
        when {
            s.bonded -> DoneRow("Bond deposited, you can judge now.")
            else -> {
                if (s.bondFunded) {
                    DoneRow("Test \$SKR funded.")
                    Button(
                        onClick = { vm.depositBond(sender) },
                        enabled = !s.busy,
                        modifier = Modifier.fillMaxWidth(),
                    ) { Text("Deposit bond") }
                }
                if (s.faucetMints < s.faucetMaxMints) {
                    Button(
                        onClick = { vm.faucet() },
                        enabled = !s.busy,
                        modifier = Modifier.fillMaxWidth(),
                    ) { Text("Get test \$SKR (${s.faucetMints}/${s.faucetMaxMints})") }
                }
                if (!s.bondFunded)
                    Text(
                        "Fund test \$SKR first, then deposit the bond.",
                        style = MaterialTheme.typography.labelSmall,
                        color = MutedGray,
                    )
            }
        }
    }
}

@Composable
private fun DoneRow(text: String) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        CaptureIcon(CaptureSymbol.VERIFIED, Modifier.size(18.dp), SuccessGreen)
        Text(text, style = MaterialTheme.typography.bodySmall, color = SuccessGreen)
    }
}
