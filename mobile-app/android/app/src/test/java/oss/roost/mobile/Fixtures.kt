package oss.roost.mobile

/**
 * Loads a golden fixture from the test classpath. The app module's test sourceSet adds
 * `../../fixtures` as a resources srcDir, so the ONE canonical copy in mobile-app/fixtures/
 * is what these tests read — no duplication (per the build.gradle.kts comment).
 */
object Fixtures {
    fun read(name: String): String {
        val stream = Fixtures::class.java.classLoader!!.getResourceAsStream(name)
            ?: error("fixture not found on test classpath: $name (is ../../fixtures wired as a test resources srcDir?)")
        return stream.bufferedReader().use { it.readText() }
    }
}
