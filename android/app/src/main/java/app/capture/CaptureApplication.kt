package app.capture

import android.app.Application
import io.sentry.android.core.SentryAndroid

class CaptureApplication : Application() {
    override fun onCreate() {
        super.onCreate()
        // Debug/demo builds and unconfigured release builds never start telemetry.
        if (BuildConfig.DEBUG || BuildConfig.SENTRY_DSN.isBlank()) return
        SentryAndroid.init(this) { options ->
            options.dsn = BuildConfig.SENTRY_DSN
            CrashReporting.configure(options)
        }
    }
}
