import org.jetbrains.kotlin.gradle.dsl.JvmTarget
import java.net.URI
import java.net.InetAddress
import java.util.Properties
import javax.net.ssl.HttpsURLConnection

plugins {
    // AGP 9 has built-in Kotlin support, so org.jetbrains.kotlin.android must
    // NOT be applied - AGP fails the build if it is.
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.compose)
}

val releaseSecrets = Properties().apply {
    val source = rootProject.file("signing.properties")
    if (source.isFile) source.inputStream().use { load(it) }
}
fun releaseValue(name: String, env: String): String? =
    providers.environmentVariable(env).orNull ?: releaseSecrets.getProperty(name)

// Local-only crash configuration; no SDK starts without explicit residency approval.
val crashConfig = Properties().apply {
    val source = rootProject.file("crash-reporting.properties")
    if (source.isFile) source.inputStream().use { load(it) }
}
val crashDsn = crashConfig.getProperty("dsn", "").trim()
val crashResidencyApproved = crashConfig.getProperty("residencyApproved", "false") == "true"
require(crashDsn.none { it == '"' || it == '\\' || it.isISOControl() }) {
    "Invalid crash DSN characters"
}
require(crashDsn.isEmpty() || crashResidencyApproved) {
    "Confirm crash processor terms and data residency before configuring a DSN"
}
if (crashDsn.isNotEmpty()) {
    val uri = URI(crashDsn)
    require(uri.scheme == "https" && !uri.host.isNullOrBlank() &&
        !uri.userInfo.isNullOrBlank() && uri.query == null && uri.fragment == null &&
        uri.path.matches(Regex("/[0-9]+"))) { "Crash reporting requires a valid HTTPS Sentry DSN" }
}

val apiBase = providers.gradleProperty("seekerApiBase")
    .orElse(providers.environmentVariable("SEEKER_API_BASE")).orNull
val releaseStore = releaseValue("storeFile", "SEEKER_KEYSTORE_FILE")?.let { rootProject.file(it) }
val releaseStorePassword = releaseValue("storePassword", "SEEKER_KEYSTORE_PASSWORD")
val releaseKeyAlias = releaseValue("keyAlias", "SEEKER_KEY_ALIAS")
val releaseKeyPassword = releaseValue("keyPassword", "SEEKER_KEY_PASSWORD")

val validateReleaseDistribution = tasks.register("validateReleaseDistribution") {
    group = "verification"
    description = "Require a reachable HTTPS backend and persistent release signing credentials."
    doLast {
        require(!apiBase.isNullOrBlank()) { "Release requires -PseekerApiBase=https://your-backend or SEEKER_API_BASE" }
        val uri = URI(apiBase)
        require(uri.scheme == "https" && !uri.host.isNullOrBlank() && uri.userInfo == null &&
            uri.query == null && uri.fragment == null &&
            uri.host.lowercase() !in setOf("localhost", "10.0.2.2", "127.0.0.1", "[::1]") &&
            !uri.host.endsWith(".localhost")) { "Release backend must be a reachable HTTPS URL, not an emulator/loopback URL" }
        require(releaseStore?.isFile == true && !releaseStorePassword.isNullOrBlank() &&
            !releaseKeyAlias.isNullOrBlank() && !releaseKeyPassword.isNullOrBlank()) {
            "Release requires a persistent keystore: configure android/signing.properties or SEEKER_KEYSTORE_* / SEEKER_KEY_*"
        }
        require(InetAddress.getAllByName(uri.host).none {
            it.isAnyLocalAddress || it.isLoopbackAddress || it.isLinkLocalAddress
        }) { "Release backend must be reachable from a physical device" }
        val connection = URI(apiBase!!.trimEnd('/') + "/health").toURL().openConnection() as HttpsURLConnection
        connection.connectTimeout = 10_000
        connection.readTimeout = 10_000
        connection.instanceFollowRedirects = false
        try {
            require(connection.responseCode in 200..299) { "Release backend /health must return HTTP 2xx over HTTPS" }
        } finally {
            connection.disconnect()
        }
    }
}

// Cover assemble, bundle, install and direct package/sign tasks, including
// invocations such as `assemble` that request both debug and release variants.
tasks.configureEach {
    if (name.contains("Release") && name != "validateReleaseDistribution") {
        dependsOn(validateReleaseDistribution)
    }
}

android {
    namespace = "app.capture"
    compileSdk = 37

    defaultConfig {
        applicationId = "app.capture"
        minSdk = 26
        targetSdk = 37
        versionCode = 2
        versionName = "0.2.0"

        val seekerBase = apiBase ?: "http://10.0.2.2:8001"
        require(seekerBase.none { it == '"' || it == '\\' || it.isISOControl() })
        buildConfigField("String", "SEEKER_API_BASE", "\"$seekerBase\"")
        buildConfigField("String", "SENTRY_DSN", "\"\"")

        // Ship a single-ABI debug APK (arm64) so the installer doesn't choke on
        // a large multi-arch bundle. The Seeker and every modern phone are arm64.
        ndk {
            abiFilters += listOf("arm64-v8a")
        }
    }

    androidResources {
        // LiteRT memory-maps the model straight out of the APK; a compressed
        // asset cannot be mapped and has to be unpacked to disk first.
        noCompress += "tflite"
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    signingConfigs {
        create("release") {
            storeFile = releaseStore
            storePassword = releaseStorePassword
            keyAlias = releaseKeyAlias
            keyPassword = releaseKeyPassword
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            buildConfigField("String", "SENTRY_DSN", "\"$crashDsn\"")
            signingConfig = signingConfigs.getByName("release")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

// AGP 9's built-in Kotlin dropped the `kotlinOptions` block; jvmTarget moves to
// the top-level kotlin extension. Set explicitly rather than via jvmToolchain
// so the build uses the JDK already present instead of provisioning another.
kotlin {
    compilerOptions {
        jvmTarget.set(JvmTarget.JVM_17)
    }
}

dependencies {
    implementation(libs.sentry.android.core)
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.lifecycle.runtime.ktx)
    implementation(libs.androidx.lifecycle.viewmodel.compose)
    implementation(libs.androidx.activity.compose)
    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.compose.ui)
    implementation(libs.androidx.compose.ui.graphics)
    implementation(libs.androidx.compose.ui.tooling.preview)
    implementation(libs.androidx.compose.material3)
    debugImplementation(libs.androidx.compose.ui.tooling)

    implementation(libs.mwa.clientlib.ktx)
    implementation(libs.okhttp)
    implementation(libs.coil.compose)
    implementation(libs.coil.network.okhttp)

    implementation(libs.camerax.core)
    implementation(libs.camerax.camera2)
    implementation(libs.camerax.lifecycle)
    implementation(libs.camerax.view)
    implementation(libs.camerax.video)
    implementation(libs.mlkit.face.detection)
    implementation(libs.litert)
    implementation(libs.androidx.media3.exoplayer)
    implementation(libs.androidx.media3.ui)

    testImplementation(libs.junit)
}

