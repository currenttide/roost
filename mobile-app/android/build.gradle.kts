// Root build script — plugins declared here (apply false) so the app module can
// apply them by alias. No buildscript classpath block needed with the plugins DSL.
plugins {
    alias(libs.plugins.android.application) apply false
    alias(libs.plugins.kotlin.android) apply false
}
