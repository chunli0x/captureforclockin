import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    // AGP 9 has built-in Kotlin support, so org.jetbrains.kotlin.android must
    // NOT be applied - AGP fails the build if it is.
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.compose)
}

android {
    namespace = "ai.loosh.union"
    compileSdk = 37

    defaultConfig {
        applicationId = "ai.loosh.union"
        minSdk = 26
        targetSdk = 37
        versionCode = 2
        versionName = "0.2.0"

        // Point this at your machine's LAN IP when running the backend locally;
        // the Seeker cannot reach your laptop's localhost. Override without
        // editing the file: -PseekerApiBase=http://192.168.1.20:8000
        val seekerBase = (project.findProperty("seekerApiBase") as String?) ?: "http://10.0.2.2:8001"
        require(!seekerBase.contains('"') && !seekerBase.contains('\n'))
        buildConfigField("String", "SEEKER_API_BASE", "\"$seekerBase\"")

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

    buildTypes {
        release {
            isMinifyEnabled = false
            // Release signing must be supplied by the release operator.
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

