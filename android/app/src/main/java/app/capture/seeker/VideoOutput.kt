package app.capture.seeker

import android.content.Context
import androidx.compose.foundation.layout.*
import androidx.compose.material3.Text
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.compose.ui.viewinterop.AndroidView
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.database.StandaloneDatabaseProvider
import androidx.media3.datasource.DataSource
import androidx.media3.datasource.DataSpec
import androidx.media3.datasource.DefaultHttpDataSource
import androidx.media3.datasource.TransferListener
import androidx.media3.datasource.cache.CacheDataSource
import androidx.media3.datasource.cache.LeastRecentlyUsedCacheEvictor
import androidx.media3.datasource.cache.SimpleCache
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.ui.AspectRatioFrameLayout
import androidx.media3.ui.PlayerView
import app.capture.*
import java.io.File
import java.io.IOException
import java.security.MessageDigest
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request

/** Only elapsed foreground PLAYING time counts; seeking to the end grants no time. */
internal data class VideoViewingState(val loaded: Boolean = false, val viewedMs: Long = 0, val failed: Boolean = false) {
    fun canSubmit(durationMs: Long): Boolean = loaded && !failed && durationMs > 0 && viewedMs >= durationMs
}

internal fun videoCacheKey(url: String, authorization: String): String {
    val account = MessageDigest.getInstance("SHA-256").digest(authorization.toByteArray())
        .joinToString("") { "%02x".format(it) }
    // Retain the COMPLETE signed URL, including its token, in the cache identity.
    return "$account:$url"
}

@androidx.annotation.OptIn(androidx.media3.common.util.UnstableApi::class)
private object VideoCache {
    private var cache: SimpleCache? = null
    @Synchronized fun get(context: Context): SimpleCache = cache ?: SimpleCache(
        File(context.cacheDir, "video"), LeastRecentlyUsedCacheEvictor(64L * 1024 * 1024),
        StandaloneDatabaseProvider(context.applicationContext),
    ).also { cache = it }
}

/** Validate even a fully cached read with the server before making it playable. */
@androidx.annotation.OptIn(androidx.media3.common.util.UnstableApi::class)
private class AuthorizedCacheSource(private val delegate: DataSource, private val authorization: String) : DataSource {
    companion object {
        private val client = OkHttpClient.Builder().followRedirects(false).followSslRedirects(false)
            .callTimeout(15, java.util.concurrent.TimeUnit.SECONDS).build()
    }
    override fun open(dataSpec: DataSpec): Long {
        val request = Request.Builder().url(dataSpec.uri.toString()).head()
            .header("Authorization", authorization).build()
        client.newCall(request).execute().use {
            if (!it.isSuccessful) throw IOException("Video authorization expired; reload pair")
        }
        return delegate.open(dataSpec)
    }
    override fun read(buffer: ByteArray, offset: Int, length: Int) = delegate.read(buffer, offset, length)
    override fun getUri() = delegate.uri
    override fun getResponseHeaders() = delegate.responseHeaders
    override fun addTransferListener(listener: TransferListener) = delegate.addTransferListener(listener)
    override fun close() = delegate.close()
}

@androidx.annotation.OptIn(androidx.media3.common.util.UnstableApi::class)
@Composable
internal fun VideoOutput(
    url: String, authorization: String, label: String, durationMs: Long, expiresAt: Long,
    modifier: Modifier = Modifier, onState: (VideoViewingState) -> Unit,
) {
    val context = LocalContext.current
    val lifecycle = LocalLifecycleOwner.current.lifecycle
    val report by rememberUpdatedState(onState)
    var active by remember(lifecycle) { mutableStateOf(lifecycle.currentState.isAtLeast(Lifecycle.State.RESUMED)) }
    var cache by remember { mutableStateOf<SimpleCache?>(null) }
    var cacheFailed by remember { mutableStateOf(false) }
    LaunchedEffect(Unit) {
        runCatching { withContext(Dispatchers.IO) { VideoCache.get(context.applicationContext) } }
            .onSuccess { cache = it }.onFailure { cacheFailed = true; report(VideoViewingState(failed = true)) }
    }
    DisposableEffect(lifecycle) {
        val observer = LifecycleEventObserver { _, _ -> active = lifecycle.currentState.isAtLeast(Lifecycle.State.RESUMED) }
        lifecycle.addObserver(observer)
        onDispose { lifecycle.removeObserver(observer) }
    }
    Column(modifier) {
        Text(label, color = CaptureInk)
        if (active && cache != null) {
            // Removing this subtree on background or navigation releases the player.
            key(url, authorization) {
                ActiveVideo(url, authorization, durationMs, expiresAt, cache!!, onState = { report(it) })
            }
        } else {
            Box(Modifier.fillMaxWidth().aspectRatio(16f / 9f)) {
                Text(if (cacheFailed) "Video unavailable" else if (active) "Loading video…" else "Video paused", color = MutedGray)
            }
            LaunchedEffect(active) { if (!active) report(VideoViewingState()) }
        }
    }
}

@androidx.annotation.OptIn(androidx.media3.common.util.UnstableApi::class)
@Composable
private fun ActiveVideo(url: String, authorization: String, durationMs: Long, expiresAt: Long,
                        cache: SimpleCache, onState: (VideoViewingState) -> Unit) {
    val context = LocalContext.current
    val lifecycle = LocalLifecycleOwner.current.lifecycle
    val report by rememberUpdatedState(onState)
    var state by remember { mutableStateOf(VideoViewingState()) }
    var buffering by remember { mutableStateOf(true) }
    var expired by remember { mutableStateOf(false) }
    val player = remember(url, authorization, cache) {
        val upstream = DefaultHttpDataSource.Factory().setDefaultRequestProperties(mapOf("Authorization" to authorization))
            .setConnectTimeoutMs(15_000).setReadTimeoutMs(15_000).setAllowCrossProtocolRedirects(false)
        val cached = CacheDataSource.Factory().setCache(cache).setUpstreamDataSourceFactory(upstream)
        val sources = DataSource.Factory { AuthorizedCacheSource(cached.createDataSource(), authorization) }
        ExoPlayer.Builder(context).setMediaSourceFactory(DefaultMediaSourceFactory(sources)).build().apply {
            volume = 0f
            setPlaybackSpeed(1f)
            repeatMode = Player.REPEAT_MODE_OFF
            setMediaItem(MediaItem.Builder().setUri(url).setCustomCacheKey(videoCacheKey(url, authorization)).build())
        }
    }
    DisposableEffect(player, lifecycle) {
        // Pause synchronously with lifecycle events, even if composition is suspended.
        val lifecycleObserver = LifecycleEventObserver { _, _ ->
            if (!lifecycle.currentState.isAtLeast(Lifecycle.State.RESUMED)) player.pause()
        }
        lifecycle.addObserver(lifecycleObserver)
        val listener = object : Player.Listener {
            override fun onPlaybackStateChanged(playbackState: Int) {
                buffering = playbackState == Player.STATE_BUFFERING || playbackState == Player.STATE_IDLE
                if (playbackState == Player.STATE_READY || playbackState == Player.STATE_ENDED) {
                    state = state.copy(loaded = true)
                }
            }
            override fun onPlayerError(error: PlaybackException) {
                state = state.copy(loaded = false, failed = true)
                buffering = false
            }
        }
        player.addListener(listener)
        player.prepare()
        onDispose {
            player.pause()
            lifecycle.removeObserver(lifecycleObserver)
            player.removeListener(listener)
            player.release()
            report(VideoViewingState())
        }
    }
    LaunchedEffect(state) { report(state) }
    LaunchedEffect(player) {
        var previous = android.os.SystemClock.elapsedRealtime()
        var wasPlaying = false
        while (true) {
            delay(100)
            val current = android.os.SystemClock.elapsedRealtime()
            if (System.currentTimeMillis() / 1000 >= expiresAt) {
                expired = true
                player.pause()
                state = state.copy(loaded = false, failed = true)
            } else if (wasPlaying && (player.isPlaying || player.playbackState == Player.STATE_ENDED)) {
                state = state.copy(viewedMs = (state.viewedMs + (current - previous).coerceAtMost(150)).coerceAtMost(durationMs))
                // At the final sample allow the 100 ms sampling granularity.
                if (player.playbackState == Player.STATE_ENDED && state.viewedMs >= durationMs - 150)
                    state = state.copy(viewedMs = durationMs)
            }
            wasPlaying = player.isPlaying
            previous = current
        }
    }
    AndroidView(
        factory = { PlayerView(it).apply {
            this.player = player
            resizeMode = AspectRatioFrameLayout.RESIZE_MODE_FIT
            setShowNextButton(false)
            setShowPreviousButton(false)
            setShowFastForwardButton(false)
            setShowRewindButton(false)
            controllerShowTimeoutMs = 0
            findViewById<android.view.View>(androidx.media3.ui.R.id.exo_settings)?.visibility = android.view.View.GONE
        } },
        update = { it.player = player },
        modifier = Modifier.fillMaxWidth().aspectRatio(16f / 9f),
    )
    when {
        expired -> Text("Video access expired. Reload this pair.", color = MutedGray)
        state.failed -> {
            Text("Video unavailable", color = MutedGray)
            SecondaryButton("Retry video", onClick = {
                state = VideoViewingState()
                buffering = true
                player.seekTo(0)
                player.prepare()
            })
        }
        buffering -> Text("Loading video…", color = MutedGray)
        !state.canSubmit(durationMs) -> Text("Play the full clip to compare", color = MutedGray)
    }
}
