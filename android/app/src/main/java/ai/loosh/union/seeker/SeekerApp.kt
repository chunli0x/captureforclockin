package ai.loosh.union.seeker

import ai.loosh.union.*
import android.content.Context
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Checkbox
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.SnackbarDuration
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.text.style.TextAlign
import androidx.lifecycle.viewmodel.compose.viewModel
import com.solana.mobilewalletadapter.clientlib.ActivityResultSender

/**
 * The judgment product app: sign in, stake, judge, and earn test $SKR for
 * consensus-aligned preferences (slashable bond in test $SKR).
 */
@Composable
fun SeekerApp(sender: ActivityResultSender, vm: SeekerViewModel = viewModel()) {
    val s by vm.state.collectAsState()
    var showBond by remember { mutableStateOf(false) }
    var showBalances by remember { mutableStateOf(false) }
    var showCalibration by remember { mutableStateOf(false) }
    var showLiveness by remember { mutableStateOf(false) }
    val context = LocalContext.current
    val prefs = remember { context.getSharedPreferences("seeker.api", Context.MODE_PRIVATE) }
    var onboarded by remember { mutableStateOf(prefs.getBoolean("onboarded", false)) }

    val snackbar = remember { SnackbarHostState() }
    LaunchedEffect(s.transient, s.status) {
        if (s.status == AsyncStatus.Success && s.transient != null) {
            snackbar.showSnackbar(s.transient!!, duration = SnackbarDuration.Short)
            vm.dismissTransient()
        }
    }

    val showTopMenu =
        onboarded && s.wallet.isNotEmpty() && !showBond && !showBalances && !showCalibration &&
            !showLiveness

    Scaffold(
        modifier = Modifier.fillMaxSize(),
        containerColor = Color.Transparent,
        topBar = {
            if (showTopMenu) {
                TopMenu(
                    onBalances = { showBalances = true },
                    onCalibration = { showCalibration = true },
                    onSignOut = { vm.logout() },
                    enabled = !s.busy,
                )
            }
        },
        snackbarHost = { SnackbarHost(snackbar) },
    ) { padding ->
        Box(Modifier.fillMaxSize().padding(padding)) {
            when {
                !onboarded -> OnboardingScreen(onDone = {
                    prefs.edit().putBoolean("onboarded", true).apply()
                    onboarded = true
                })
                s.wallet.isEmpty() -> SignInScreen(s, vm, sender)
                showLiveness -> {
                    LaunchedEffect(Unit) { vm.startLiveness() }
                    val challenge = s.livenessChallenge
                    if (challenge == null) {
                        Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                            Text("Starting the human check…", color = CaptureInk)
                        }
                    } else {
                        LivenessScreen(
                            challengeWire = challenge,
                            busy = s.error ?: s.message,
                            onPassed = {
                                vm.completeLiveness(it)
                                showLiveness = false
                            },
                            onCancel = { showLiveness = false },
                        )
                    }
                }
                showBond -> BondScreen(s, vm, sender, onBack = { showBond = false })
                showBalances -> BalanceScreen(s, vm, onBack = { showBalances = false })
                showCalibration -> CalibrationScreen(s, vm, onBack = { showCalibration = false })
                else -> JudgmentScreen(
                    s,
                    vm,
                    onBond = { showBond = true },
                    onProveHuman = { showLiveness = true },
                )
            }
        }
    }
}

@Composable
private fun TopMenu(
    onBalances: () -> Unit,
    onCalibration: () -> Unit,
    onSignOut: () -> Unit,
    enabled: Boolean,
) {
    Column(Modifier.background(CaptureBase).statusBarsPadding()) {
        Row(
            Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 4.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                CaptureMark(Modifier.size(28.dp))
                Text("CAPTURE", style = MaterialTheme.typography.displaySmall.copy(fontSize = 18.sp, lineHeight = 22.sp), color = CaptureInk)
            }
            Text(
                "DEVNET · TEST \$SKR",
                style = MaterialTheme.typography.labelSmall.copy(letterSpacing = 0.sp),
                color = CaptureInk,
                textAlign = TextAlign.Center,
                modifier = Modifier.weight(1f).background(CaptureYellow, CaptureRadii.pill)
                    .border(CaptureDimensions.inkBorder, CaptureInk, CaptureRadii.pill)
                    .padding(horizontal = 8.dp, vertical = 8.dp),
            )
            TextButton(onClick = onSignOut, enabled = enabled,
                modifier = Modifier.heightIn(min = CaptureDimensions.touchTarget),
                contentPadding = PaddingValues(horizontal = 4.dp)) {
                Text("Sign out", color = MutedGray, style = MaterialTheme.typography.bodySmall)
            }
        }
        Row(Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 4.dp),
            horizontalArrangement = Arrangement.spacedBy(12.dp)) {
            TextButton(onClick = onCalibration, enabled = enabled,
                modifier = Modifier.weight(1f).heightIn(min = CaptureDimensions.touchTarget)) {
                CaptureIcon(CaptureSymbol.EXPLORE, Modifier.size(22.dp), CaptureInk)
                Spacer(Modifier.width(8.dp))
                Text("Calibrate", color = CaptureInk, style = MaterialTheme.typography.titleMedium)
            }
            TextButton(onClick = onBalances, enabled = enabled,
                modifier = Modifier.weight(1f).heightIn(min = CaptureDimensions.touchTarget)) {
                CaptureIcon(CaptureSymbol.WALLET, Modifier.size(22.dp), CaptureInk)
                Spacer(Modifier.width(8.dp))
                Text("Balances", color = CaptureInk, style = MaterialTheme.typography.titleMedium)
            }
        }
        Box(Modifier.fillMaxWidth().height(CaptureDimensions.inkBorder).background(CaptureOutline))
    }
}

@Composable
private fun SignInScreen(s: PilotState, vm: SeekerViewModel, sender: ActivityResultSender) {
    var over18 by remember { mutableStateOf(false) }
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
            EditorialHeading("SIGN IN.")
            Text(
                "Compare two model outputs and pick the better one. Consensus-aligned judges " +
                    "earn test \$SKR on devnet.",
                style = MaterialTheme.typography.bodyMedium,
                color = CaptureInk,
            )
            if (s.error != null) {
                Row(
                    Modifier
                        .fillMaxWidth()
                        .background(ErrorRedBg, CaptureRadii.md)
                        .border(1.dp, ErrorRed, CaptureRadii.md)
                        .padding(horizontal = 14.dp, vertical = 10.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text(
                        "!",
                        style = MaterialTheme.typography.titleMedium,
                        color = ErrorRed,
                        modifier = Modifier.padding(end = 10.dp),
                    )
                    Text(
                        s.error,
                        style = MaterialTheme.typography.bodyMedium,
                        color = ErrorRed,
                        modifier = Modifier.weight(1f),
                    )
                    TextButton(onClick = vm::dismissError) { Text("Dismiss", color = ErrorRed) }
                }
            }
            Row(
                Modifier
                    .fillMaxWidth()
                    .background(CaptureSurface2, CaptureRadii.md)
                    .border(1.dp, CaptureOutline, CaptureRadii.md)
                    .padding(horizontal = 14.dp, vertical = 10.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Checkbox(checked = over18, onCheckedChange = { over18 = it }, enabled = !s.busy)
                Text(
                    "I confirm I am 18 or older",
                    style = MaterialTheme.typography.bodyMedium,
                    color = CaptureInk,
                )
            }
            Text(
                "Designed for the Solana Seeker mobile",
                style = MaterialTheme.typography.bodyMedium,
                color = CaptureInk,
            )
            Button(
                onClick = { vm.connect(sender, over18) },
                enabled = !s.busy && over18,
                modifier = Modifier.fillMaxWidth().height(56.dp),
                colors = ButtonDefaults.buttonColors(
                    containerColor = CaptureAction,
                    contentColor = Color.White,
                    disabledContainerColor = ButtonGrey,
                    disabledContentColor = ButtonGreyInk,
                ),
                shape = CaptureRadii.pill,
            ) {
                Text("Sign in with Seeker Wallet", style = MaterialTheme.typography.titleMedium)
            }
        }
    }
}
