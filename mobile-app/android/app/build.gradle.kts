plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
}

android {
    namespace = "oss.roost.mobile"
    compileSdk = 35

    defaultConfig {
        applicationId = "oss.roost.mobile"
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    buildTypes {
        debug {
            // Debug builds use the auto-generated debug keystore — no signing
            // config needed. Release is deliberately minimal (no shrinker) so a
            // contributor can `assembleRelease` without extra setup; ship signing
            // before any store upload.
            isMinifyEnabled = false
        }
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    buildFeatures {
        compose = true
    }
    composeOptions {
        // Compose compiler matched to Kotlin 1.9.24 (see Compose-Kotlin map).
        kotlinCompilerExtensionVersion = "1.5.14"
    }
    packaging {
        resources {
            excludes += "/META-INF/{AL2.0,LGPL2.1}"
        }
    }
    testOptions {
        unitTests {
            isReturnDefaultValues = true
        }
    }

    // The pure-Kotlin parsers are tested against the ONE canonical fixture copy
    // in mobile-app/fixtures/ — no duplication. They are exposed to JVM unit
    // tests via this resources srcDir so `getResourceAsStream` finds them.
    sourceSets {
        getByName("test") {
            resources.srcDir("../../fixtures")
        }
    }
}

dependencies {
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.lifecycle.runtime.ktx)
    implementation(libs.androidx.lifecycle.runtime.compose)
    implementation(libs.androidx.lifecycle.viewmodel.compose)
    implementation(libs.androidx.activity.compose)
    implementation(libs.androidx.navigation.compose)
    implementation(libs.kotlinx.coroutines.android)

    val composeBom = platform(libs.androidx.compose.bom)
    implementation(composeBom)
    implementation(libs.androidx.compose.ui)
    implementation(libs.androidx.compose.ui.graphics)
    implementation(libs.androidx.compose.ui.tooling.preview)
    implementation(libs.androidx.compose.material3)
    implementation(libs.androidx.compose.material.icons)
    debugImplementation(libs.androidx.compose.ui.tooling)

    // org.json ships in the Android platform; on the JVM unit-test classpath it is
    // provided by AGP's mock android.jar. No external JSON dep.

    testImplementation(libs.junit)
    testImplementation(libs.kotlinx.coroutines.test)
    // org.json is a PLATFORM class at runtime (android.jar), so it is NOT a
    // shipped dependency. But AGP's unit-test android.jar is a stub that throws
    // "not mocked"; the real implementation here lives ONLY on the test
    // classpath so the pure-Kotlin parsers can be exercised on the JVM. It adds
    // zero bytes to the APK.
    testImplementation(libs.json)
}
