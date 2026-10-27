package app.capture

import io.sentry.AsyncHttpTransportFactory
import io.sentry.KeyValueCollectionBehavior
import io.sentry.Hint
import io.sentry.NoOpLogger
import io.sentry.SentryEvent
import io.sentry.android.core.SentryAndroidOptions
import io.sentry.protocol.Mechanism
import io.sentry.protocol.SentryException
import io.sentry.protocol.SentryStackFrame
import io.sentry.protocol.SentryStackTrace

/** An allowlist, not regex redaction of arbitrary application content. */
internal object CrashReporting {
    fun configure(options: SentryAndroidOptions) {
        options.isDebug = false
        options.setLogger(NoOpLogger.getInstance())
        options.dataCollection.apply {
            userInfo = false
            cookies = KeyValueCollectionBehavior.off()
            urlQueryParams = KeyValueCollectionBehavior.off()
            httpBodies = emptySet()
            httpHeaders.request = KeyValueCollectionBehavior.off()
            httpHeaders.response = KeyValueCollectionBehavior.off()
            databaseQueryData = false
            filePaths = false
            graphql.document = false
            graphql.variables = false
        }
        options.isEnableExternalConfiguration = false
        options.isEnableAutoSessionTracking = false
        options.isEnableScopePersistence = false
        options.isSendClientReports = false
        options.isAttachServerName = false
        options.isAttachThreads = false
        options.isAttachScreenshot = false
        options.isAttachViewHierarchy = false
        options.isAttachAnrThreadDump = false
        options.isAttachRawTombstone = false
        options.isEnableNdk = false
        options.isEnableScopeSync = false
        options.isEnableRootCheck = false
        options.isEnableActivityLifecycleBreadcrumbs = false
        options.isEnableAppLifecycleBreadcrumbs = false
        options.isEnableSystemEventBreadcrumbs = false
        options.isEnableAppComponentBreadcrumbs = false
        options.isEnableNetworkEventBreadcrumbs = false
        options.isEnableUserInteractionBreadcrumbs = false
        options.isEnableAutoActivityLifecycleTracing = false
        options.isEnableUserInteractionTracing = false
        options.isEnableScreenTracking = false
        options.isEnableAutoTraceIdGeneration = false
        options.isEnableFramesTracking = false
        options.isEnableAppStartProfiling = false
        options.isEnableStandaloneAppStartTracing = false
        options.tracesSampleRate = 0.0
        options.profilesSampleRate = 0.0
        options.profileSessionSampleRate = 0.0
        options.anrProfilingSampleRate = 0.0
        options.sessionReplay.sessionSampleRate = 0.0
        options.sessionReplay.onErrorSampleRate = 0.0
        options.logs.isEnabled = false
        options.metrics.isEnabled = false
        options.maxBreadcrumbs = 0
        options.maxAttachmentSize = 0
        options.setBeforeBreadcrumb { _, _ -> null }
        options.setBeforeSendTransaction { _, _ -> null }
        options.setBeforeSend { event, hint -> sanitize(event, hint) }
        options.setTransportFactory { config, request ->
            CrashTransport(AsyncHttpTransportFactory().create(config, request), config.serializer)
        }
    }

    fun sanitize(event: SentryEvent, hint: Hint): SentryEvent? {
        // Scope attachments are copied to the hint before beforeSend runs.
        hint.clearAttachments()
        hint.screenshot = null
        hint.viewHierarchy = null
        hint.threadDump = null
        hint.tombstone = null
        hint.replayRecording = null
        val exceptions = event.exceptions?.map { source ->
            SentryException().apply {
                type = symbol(source.type) ?: "Error"
                module = symbol(source.module)
                mechanism = Mechanism().apply {
                    type = "capture"
                    isHandled = source.mechanism?.isHandled ?: true
                }
                stacktrace = source.stacktrace?.let { stack ->
                    SentryStackTrace(stack.frames.orEmpty().map { frame ->
                        SentryStackFrame().apply {
                            module = symbol(frame.module)
                            function = symbol(frame.function)
                            lineno = frame.lineno
                            isInApp = frame.isInApp
                        }
                    })
                }
            }
        }?.takeIf { it.isNotEmpty() } ?: return null // Never send log/message-only events.

        // Construct a fresh event: no throwable/message, user, request, breadcrumbs,
        // tags, extras, contexts, thread names, source text/paths, locals or unknown fields.
        return SentryEvent(event.timestamp).apply {
            eventId = event.eventId
            level = event.level
            platform = "java"
            release = "${BuildConfig.APPLICATION_ID}@${BuildConfig.VERSION_NAME}+${BuildConfig.VERSION_CODE}"
            environment = "release"
            setExceptions(exceptions)
        }
    }

    // Only compiled JVM symbol identifiers belong in stack locations; no arbitrary text.
    private fun symbol(value: String?): String? = value?.takeIf {
        it.length <= 240 && it.matches(Regex("[A-Za-z_$][A-Za-z0-9_.$<>-]*"))
    }
}
