package app.capture

import io.sentry.Attachment
import io.sentry.Hint
import io.sentry.JsonSerializer
import io.sentry.SentryEnvelope
import io.sentry.SentryEnvelopeHeader
import io.sentry.SentryEnvelopeItem
import io.sentry.SentryEvent
import io.sentry.SentryItemType
import io.sentry.android.core.SentryAndroidOptions
import io.sentry.transport.ITransport
import io.sentry.transport.RateLimiter
import java.io.ByteArrayOutputStream
import java.io.StringReader
import org.junit.Assert.*
import org.junit.Test

class CrashReportingTest {
    private val secret = "confidential wallet Bearer token https://media.invalid/frame?signature=secret"
    private val options = SentryAndroidOptions().also(CrashReporting::configure)
    private val serializer = JsonSerializer(options)

    private fun event(): SentryEvent = serializer.deserialize(StringReader("""
        {"message":{"formatted":"$secret"},"user":{"id":"$secret","ip_address":"127.0.0.1"},
         "request":{"url":"$secret","data":"$secret","headers":{"Authorization":"$secret"}},
         "extra":{"payload":"$secret"},"tags":{"wallet":"$secret"},
         "contexts":{"private":{"payload":"$secret"}},"transaction":"$secret",
         "breadcrumbs":[{"message":"$secret"}],"unknown_field":"$secret",
         "threads":{"values":[{"id":1,"name":"$secret"}]},
         "exception":{"values":[{"type":"IllegalStateException","module":"java.lang","value":"$secret",
          "unknown_field":"$secret","mechanism":{"type":"$secret","handled":false,"data":{"x":"$secret"}},
          "stacktrace":{"frames":[{"module":"app.capture.MainActivity","function":"onCreate","lineno":42,
           "filename":"$secret","abs_path":"$secret","context_line":"$secret","pre_context":["$secret"],
           "vars":{"frame":"$secret"},"unknown_field":"$secret"}]}}]}}
    """.trimIndent()), SentryEvent::class.java)!!

    @Test fun serializedEnvelopeContainsOnlyCrashMetadata() {
        val attachment = Attachment(secret.toByteArray(), "frame.jpg")
        val hint = Hint.withAttachment(attachment).apply {
            screenshot = attachment
            viewHierarchy = attachment
            threadDump = attachment
            tombstone = attachment
        }
        val sink = RecordingTransport()
        val header = serializer.deserialize(StringReader("""
            {"event_id":"0123456789abcdef0123456789abcdef", "unknown_field":"$secret",
             "trace":{"trace_id":"0123456789abcdef0123456789abcdef", "public_key":"key",
                      "user_id":"$secret", "transaction":"$secret"}}
        """.trimIndent()), SentryEnvelopeHeader::class.java)!!
        assertNotNull(header.traceContext)
        val envelope = SentryEnvelope(header, listOf(
            SentryEnvelopeItem.fromEvent(serializer, event()),
            SentryEnvelopeItem.fromAttachment(serializer, options.logger, attachment, 10000),
        ))
        CrashTransport(sink, serializer).send(envelope, hint)
        val sent = requireNotNull(sink.envelope)
        assertEquals(listOf(SentryItemType.Event), sent.items.map { it.header.type })
        assertNull(sent.header.traceContext)
        val bytes = ByteArrayOutputStream()
        serializer.serialize(sent, bytes)
        val json = bytes.toString("UTF-8")
        for (forbidden in listOf("confidential", "Bearer", "signature", "wallet", "frame.jpg",
                "unknown_field", "Authorization", "127.0.0.1", "context_line", "abs_path", "vars")) {
            assertFalse("Leaked $forbidden", json.contains(forbidden))
        }
        assertTrue(json.contains("IllegalStateException"))
        assertTrue(json.contains("onCreate"))
        assertTrue(json.contains("42"))
        assertTrue(json.contains("\"handled\":false"))
        assertTrue(hint.attachments.isEmpty())
        assertNull(hint.screenshot)
        assertNull(hint.viewHierarchy)
        assertNull(hint.threadDump)
        assertNull(hint.tombstone)
    }

    @Test fun messageOnlyEventsAndNonCrashEnvelopesAreDropped() {
        assertNull(options.beforeSend!!.execute(SentryEvent(), Hint()))
        val sink = RecordingTransport()
        val attachment = Attachment(secret.toByteArray(), "frame.jpg")
        CrashTransport(sink, serializer).send(SentryEnvelope(null, null,
            SentryEnvelopeItem.fromAttachment(serializer, options.logger, attachment, 10000)))
        assertNull(sink.envelope)
    }

    @Test fun collectionFeaturesStayDisabled() {
        assertEquals(false, options.dataCollection.userInfo)
        assertFalse(options.isAttachScreenshot)
        assertFalse(options.isAttachViewHierarchy)
        assertFalse(options.isAttachAnrThreadDump)
        assertFalse(options.isAttachRawTombstone)
        assertFalse(options.isEnableNdk)
        assertFalse(options.isEnableScopePersistence)
        assertFalse(options.isEnableAutoSessionTracking)
        assertFalse(options.logs.isEnabled)
        assertFalse(options.metrics.isEnabled)
        assertEquals(0.0, options.tracesSampleRate!!, 0.0)
        assertEquals(0.0, options.sessionReplay.onErrorSampleRate!!, 0.0)
        assertEquals(0.0, options.sessionReplay.sessionSampleRate!!, 0.0)
        assertEquals(0, options.maxBreadcrumbs)
        assertEquals(0L, options.maxAttachmentSize)
    }

    private class RecordingTransport : ITransport {
        var envelope: SentryEnvelope? = null
        override fun send(envelope: SentryEnvelope, hint: Hint) { this.envelope = envelope }
        override fun flush(timeoutMillis: Long) = Unit
        override fun getRateLimiter(): RateLimiter? = null
        override fun close() = Unit
        override fun close(isRestarting: Boolean) = Unit
    }
}
