package app.capture

import io.sentry.Hint
import io.sentry.ISerializer
import io.sentry.SentryEnvelope
import io.sentry.SentryEnvelopeItem
import io.sentry.SentryItemType
import io.sentry.transport.ITransport

/** Last boundary before Sentry's disk cache/network, including cached-envelope retries. */
internal class CrashTransport(
    private val delegate: ITransport,
    private val serializer: ISerializer,
) : ITransport by delegate {
    override fun send(envelope: SentryEnvelope) = send(envelope, Hint())

    override fun send(envelope: SentryEnvelope, hint: Hint) {
        // Discard all non-error items: attachments, replay, profiles, feedback,
        // logs, sessions, transactions and metrics. Rebuild the header too so
        // scope baggage, transaction names and user identifiers cannot escape.
        val clean = try {
            val item = envelope.items.singleOrNull { it.header.type == SentryItemType.Event } ?: return
            val event = item.getEvent(serializer) ?: return
            val safe = CrashReporting.sanitize(event, hint) ?: return
            SentryEnvelope(safe.eventId, null, SentryEnvelopeItem.fromEvent(serializer, safe))
        } catch (_: Exception) {
            return // Fail closed; never log the original envelope or error text.
        }
        delegate.send(clean, hint)
    }
}
