package ai.loosh.union.seeker

import ai.loosh.union.CaptureBase
import ai.loosh.union.R
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.res.painterResource

@Composable
fun TitleScreen() {
    Box(
        modifier = Modifier.fillMaxSize().background(CaptureBase),
        contentAlignment = Alignment.Center,
    ) {
        Image(
            painter = painterResource(R.drawable.capture_logo),
            contentDescription = "Capture",
            modifier = Modifier.fillMaxWidth(0.65f),
            contentScale = ContentScale.Fit,
        )
    }
}
