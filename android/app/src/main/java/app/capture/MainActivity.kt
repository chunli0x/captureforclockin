package app.capture

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.SystemBarStyle
import androidx.activity.enableEdgeToEdge
import androidx.compose.ui.graphics.toArgb
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.ui.Modifier
import com.solana.mobilewalletadapter.clientlib.ActivityResultSender

class MainActivity : ComponentActivity() {

    // Must be constructed during onCreate: ActivityResultSender registers an
    // activity result launcher, which throws if done after the activity starts.
    private lateinit var sender: ActivityResultSender

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge(
            statusBarStyle = SystemBarStyle.light(CaptureBaseDeep.toArgb(), CaptureBaseDeep.toArgb()),
            navigationBarStyle = SystemBarStyle.light(CaptureBase.toArgb(), CaptureBase.toArgb()),
        )
        sender = ActivityResultSender(this)

        setContent {
            CaptureTheme {
                Surface(
                    modifier = Modifier.fillMaxSize(),
                    color = MaterialTheme.colorScheme.background,
                ) {
                    app.capture.seeker.SeekerApp(sender)
                }
            }
        }
    }
}
