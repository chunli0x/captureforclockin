package app.capture.seeker

import org.junit.Assert.*
import org.junit.Test

class VideoViewingTest {
    @Test fun `submission requires loaded full viewing opportunity and no error`() {
        assertFalse(VideoViewingState().canSubmit(4000))
        assertFalse(VideoViewingState(loaded = true, viewedMs = 3999).canSubmit(4000))
        assertTrue(VideoViewingState(loaded = true, viewedMs = 4000).canSubmit(4000))
        assertFalse(VideoViewingState(loaded = true, viewedMs = 4000, failed = true).canSubmit(4000))
        assertFalse(VideoViewingState(loaded = true, viewedMs = 4000).canSubmit(0))
    }
    @Test fun `cache key keeps signed token and isolates sessions`() {
        val url = "https://local.test/media/deliver/abc?token=one"
        assertTrue(videoCacheKey(url, "wallet-a").endsWith(url))
        assertNotEquals(videoCacheKey(url, "wallet-a"), videoCacheKey(url, "wallet-b"))
        assertNotEquals(videoCacheKey(url, "wallet-a"), videoCacheKey(url.replace("one", "two"), "wallet-a"))
    }
}
