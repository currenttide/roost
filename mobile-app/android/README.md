# Roost Mobile — Android

A lightweight Jetpack Compose phone client for a Roost fleet: pair by QR, watch the
dashboard live, dispatch agent jobs by voice or text, and stream a session log. Built
against the pinned contract in [`../API.md`](../API.md) and the design in
[`../DESIGN.md`](../DESIGN.md).

**Dependency budget (enforced):** Kotlin stdlib, kotlinx-coroutines, androidx/compose
essentials (BOM, activity-compose, navigation-compose, lifecycle), `org.json` (platform)
for JSON, `java.net.HttpURLConnection` for HTTP. **No** Retrofit/OkHttp/Moshi/Gson/
kotlinx-serialization, no ML Kit, no Hilt. `org.json` appears only on the *test* classpath
(`org.json:json`) so the pure-Kotlin parsers run on the JVM; it adds zero bytes to the APK.

- `minSdk 26`, `targetSdk 35`, `compileSdk 35`.

## Build

The Gradle wrapper JAR is intentionally **not committed**. Generate it once, then build:

```bash
# From this directory (mobile-app/android):

# 1. One-time: generate the wrapper (needs a local Gradle ≥ 8.5 on PATH).
gradle wrapper --gradle-version 8.9

# 2. Build the debug APK (uses the auto-generated debug keystore — no signing setup).
./gradlew :app:assembleDebug
#   → app/build/outputs/apk/debug/app-debug.apk

# 3. Run the pure-JVM unit tests (decode every fixture, SSE transcript, pairing, etc.).
./gradlew :app:testDebugUnitTest
```

Or open the `android/` folder in **Android Studio** (Koala or newer): it provisions the
SDK, generates the wrapper, and offers Run/Test from the gutter.

You need an Android SDK with platform 35 + build-tools installed (`sdkmanager
"platforms;android-35" "build-tools;35.0.0"`), and JDK 17.

## Pairing

On the control-plane host:

```bash
roost pair            # prints a roost://pair?d=<base64url> QR + string (mobile-scoped token)
```

Then either:

- **Scan the QR** with your phone's camera app / Google Lens. The `roost://` link opens
  the app straight onto the pairing screen, which decodes + probes `GET /healthz` and
  stores the pairing, or
- **Paste** the `roost://pair?d=…` string (or even the raw JSON) into the manual field.

The token is stored encrypted: an AES-256/GCM key in the AndroidKeyStore wraps the token
inside a normal `SharedPreferences` value (`store/KeystoreBox.kt` + `SecureStore.kt`) — no
deprecated `security-crypto` artifact. A later `401` unpairs you back to this screen; a
`403` shows the error but keeps you paired (scope bug, not an auth failure).

## What's wired

- **Dashboard** — polls `GET /derived?limit=40` every 2 s while foregrounded (paused in
  background via a lifecycle observer). Fleet-verdict bar, live-node chip, run rows sorted
  running/assigned-first then `created_at` desc, health-status glyphs (unknown → plain
  text), a "data N s old" staleness pill, long-press row actions (cancel running w/
  confirm, retry failed by resubmitting its spec), and the big "New session" button.
- **Session** — header from `GET /jobs/{id}/derived`; a hand-rolled SSE client on
  `HttpURLConnection` (`net/SseClient.kt`) implementing the resume protocol: page
  `GET /logs?since=` to catch up, attach `GET /stream?since=`, dedupe `seq <= last-seen`,
  persist max seq per job id, reconnect with jittered 1→30 s backoff. Monospace log with
  ANSI stripping, `event` rows as dividers, auto-follow tail + jump-to-bottom FAB, a result
  card on `done`, cancel while running, and a `Tree ▸` child list that recurses.
- **New-session sheet** — editable field + hold-to-talk `SpeechRecognizer` (offline-
  preferred, live partials, haptics, mic hidden if unavailable, runtime RECORD_AUDIO
  prompt), `auto` vs pin-worker target, `agent` vs `command` kind, last-10-prompt chips.
  Dispatches `POST /jobs` and jumps into the session.

## Tests

`./gradlew :app:testDebugUnitTest` runs plain JVM tests (no Robolectric — all parsing is
android-free in `model/` + `sse/`):

- `ParserFixtureTest` decodes **every** JSON fixture in `../fixtures/`.
- `SseTest` parses `stream_succeeded.sse.txt`, asserts the exact `state` + 6 `log` + `done`
  sequence, and that replaying seq 5/6 is dropped by the de-dup buffer.
- `PairUriTest` round-trips the pairing URI incl. base64url padding restoration and
  rejects `v > 1`.
- `MapperTest` confirms an unknown `health.status` renders as plain text (no crash) and
  ANSI stripping.
- `NotifyTest` covers the push-notification client logic (R37 / DESIGN.md §6): ntfy-topic
  normalize/validate, the R37 payload → deep-link route (job_id → Session; malformed →
  Dashboard fallback), and a **cross-contract** block parsing payload literals copied from
  the server's `tests/test_notify.py` so client/server drift is caught.

Fixtures are read from the single canonical copy via a test `resources.srcDir("../../fixtures")`
(see `app/build.gradle.kts`) — there is exactly one copy in the repo.

## Known build risks

This module was authored in an environment **without an Android SDK, Gradle, or a JVM**, so
it has not been compiled or run. Verify these when you first build:

1. **No `gradle-wrapper.jar` committed.** You must run `gradle wrapper` first (above). CI
   that expects `./gradlew` to exist standalone will fail until the JAR is generated.
2. **Compose BOM ↔ Kotlin ↔ compiler-extension pinning.** `libs.versions.toml` uses Kotlin
   1.9.24 with `kotlinCompilerExtensionVersion = "1.5.14"` and Compose BOM 2024.06.00. If
   you bump any one, re-check the official Compose-to-Kotlin compatibility map; a mismatch
   is the most likely first-build error. (AGP 8.5.2 also expects JDK 17.)
3. **`org.json` on the unit-test classpath.** AGP's stub `android.jar` makes `org.json`
   throw "not mocked" in unit tests, so the real `org.json:json` is a `testImplementation`.
   If a future AGP changes unit-test classpath ordering, the stub could shadow it — if a
   test fails with "Method … not mocked", confirm the real artifact wins on the classpath.
4. **`material-icons-extended`** is pulled only for the `Mic`/`ArrowBack`/`KeyboardArrowDown`
   icons; it is sizeable. If APK size matters, replace those three with inline
   `ImageVector`s and drop the dependency. (The DESIGN <10 MB budget is otherwise easily met.)
5. **`SpeechRecognizer` offline behavior** varies by device/OEM. `EXTRA_PREFER_OFFLINE`
   is a hint; some devices ignore it or have no on-device model and return
   `ERROR_NO_MATCH`. The mic is hidden when `isRecognitionAvailable` is false; typing is
   always available. Not testable without hardware.
6. **`network_security_config.xml` cleartext scope.** Android has no CIDR matcher, so the
   base config permits cleartext broadly (LAN/tunnel trust model, same as the CLI) with
   `localhost`/`127.0.0.1`/`*.ts.net` named for documentation. Tighten to specific hosts
   if you deploy on an untrusted network.
7. **Deep-link warm-launch uses `recreate()`** in `MainActivity.onNewIntent` to re-run
   `setContent` with the new intent — simple and fine for the rare pairing case, but if you
   add more deep links, switch to a `StateFlow<String?>` fed from `onNewIntent` instead.
8. **`adb`/emulator run** of voice, keystore, and SSE-over-flapping-network paths is
   unverified here; smoke-test on a device against a live `roost` control plane.

## JVM pure-layer harness (no Android SDK)

The model/ and sse/ packages are android-free, so they compile and test with
bare kotlinc + JUnit (verified 2026-06-05 on Kotlin 1.9.24):

```sh
kotlinc app/src/main/java/oss/roost/mobile/{model,sse}/*.kt \
        app/src/test/java/oss/roost/mobile/*.kt \
        -classpath json.jar:junit.jar:hamcrest.jar -d out
java -cp out:json.jar:junit.jar:hamcrest.jar:../fixtures:kotlin-stdlib.jar \
     org.junit.runner.JUnitCore oss.roost.mobile.ParserFixtureTest \
     oss.roost.mobile.SseTest oss.roost.mobile.PairUriTest oss.roost.mobile.MapperTest \
     oss.roost.mobile.NotifyTest
```

(jars: org.json 20240303, junit 4.13.2, hamcrest-core 1.3 from Maven Central.)

## Full build on Linux (no Android Studio)

Verified 2026-06-05: `assembleDebug` + `testDebugUnitTest` pass with JDK 17,
Gradle 8.9, and SDK cmdline-tools (platform 35, build-tools 34) — no Studio:

```sh
JAVA_HOME=<jdk17> ANDROID_HOME=<sdk> gradle :app:assembleDebug :app:testDebugUnitTest
```

Note: the **debug** APK is ~16 MB (unminified, all ABIs, debug symbols). The
<10 MB DESIGN budget applies to the release build — enable `isMinifyEnabled`
+ resource shrinking and/or inline the 3 icons out of `material-icons-extended`
before shipping.
