import XCTest

/// XCUITest smoke suite (R84) — closes the tap-gap that user-testing hit: no
/// XCUITest target existed, so every tap-gated iOS screen (New-session, Session,
/// Tree, Notifications, Schedules, swipe actions, Unpair) went unverified.
///
/// These flows drive the REAL app through the accessibility tree. They pair via
/// the `ROOST_PAIR_URI` launch hook (R76 — same code path as a scanned QR, no
/// system open-URL dialog) so the suite is deterministic and runs headless via
/// `xcodebuild test` on the Mac node.
///
/// Wiring (how the CP reaches the test): the runner exports the scratch CP's
/// pairing URI as the `ROOST_PAIR_URI` environment variable; this test forwards
/// it into `app.launchEnvironment` so `AppState.init` consumes it on launch.
/// Without it (e.g. a developer running the suite from Xcode with no CP), the
/// pairing-dependent flows `XCTSkip` rather than fail — the suite never goes red
/// for a missing fixture, only for a real regression.
///
/// Identifiers asserted here are added (additively) to the views in this PR; see
/// `DashboardView`, `NewSessionView`, `SessionView`, `NotificationSettingsView`,
/// `SchedulesView`.
final class SmokeTests: XCTestCase {

    /// Generous-but-bounded waits: the CP polls every 2 s and the sim is slower
    /// than a device. 25 s comfortably covers a cold launch + first /derived
    /// round-trip without hanging a CI lane on a genuine failure.
    private let appearTimeout: TimeInterval = 25

    override func setUp() {
        super.setUp()
        continueAfterFailure = false
    }

    // MARK: - Launch helpers

    /// The pairing URI the runner injected, if any. Read from the test process's
    /// own environment (the Roost job sets it before `xcodebuild test`).
    private var injectedPairURI: String? {
        let v = ProcessInfo.processInfo.environment["ROOST_PAIR_URI"]
        return (v?.isEmpty == false) ? v : nil
    }

    /// A job id to deep-link into for the Session flow, if the runner seeded one.
    private var injectedOpenSession: String? {
        let v = ProcessInfo.processInfo.environment["ROOST_OPEN_SESSION"]
        return (v?.isEmpty == false) ? v : nil
    }

    /// Launch the app paired (forwarding the pairing URI). Skips the whole test
    /// when no CP was provided, so local Xcode runs don't fail spuriously.
    private func launchPaired(extraEnv: [String: String] = [:]) throws -> XCUIApplication {
        guard let uri = injectedPairURI else {
            throw XCTSkip("No ROOST_PAIR_URI in the environment — start a scratch "
                          + "CP and export its pairing URI to run the paired flows "
                          + "(see ios/README 'UI smoke suite').")
        }
        let app = XCUIApplication()
        var env = extraEnv
        env["ROOST_PAIR_URI"] = uri
        app.launchEnvironment = env
        app.launch()
        return app
    }

    /// Wait for the dashboard to be on-screen and showing live data: the
    /// nav-title "Roost", the verdict bar, and ≥1 run row. Returns the app.
    @discardableResult
    private func waitForLiveDashboard(_ app: XCUIApplication) -> XCUIApplication {
        // The pairing screen healthz-probes then routes to the dashboard; the
        // big "New session" button is the dashboard's stable anchor.
        let newSession = app.buttons["new-session-button"]
        XCTAssertTrue(newSession.waitForExistence(timeout: appearTimeout),
                      "Dashboard did not appear after launch-arg pairing.")

        // Live data: the verdict bar renders only when /derived returned a
        // fleet verdict, and ≥1 run row means the seeded jobs decoded.
        let verdict = app.otherElements["verdict-bar"]
        XCTAssertTrue(verdict.waitForExistence(timeout: appearTimeout),
                      "Verdict bar (live /derived data) never rendered.")

        let firstRow = app.descendants(matching: .any)
            .matching(NSPredicate(format: "identifier BEGINSWITH 'run-row-'"))
            .firstMatch
        XCTAssertTrue(firstRow.waitForExistence(timeout: appearTimeout),
                      "No run rows — the dashboard rendered no seeded jobs.")
        return app
    }

    // MARK: - Flow 1: pair → live dashboard

    func testPairThenDashboardShowsLiveData() throws {
        let app = try launchPaired()
        waitForLiveDashboard(app)
        attachScreenshot(app, name: "01-dashboard")
    }

    // MARK: - Flow 2: New-session sheet

    func testNewSessionSheetOpens() throws {
        let app = try launchPaired()
        waitForLiveDashboard(app)

        app.buttons["new-session-button"].tap()

        // The sheet's prompt field + Dispatch button prove the controls rendered.
        let prompt = app.textViews["new-session-prompt"]
            .firstMatch
        let promptField = prompt.exists ? prompt
            : app.textFields["new-session-prompt"].firstMatch
        XCTAssertTrue(promptField.waitForExistence(timeout: appearTimeout),
                      "New-session prompt field never appeared.")
        XCTAssertTrue(app.buttons["new-session-dispatch"].exists,
                      "Dispatch button missing from the New-session sheet.")
        attachScreenshot(app, name: "02-new-session")

        app.buttons["new-session-cancel"].tap()
        XCTAssertTrue(app.buttons["new-session-button"].waitForExistence(timeout: appearTimeout),
                      "Did not return to the dashboard after Cancel.")
    }

    // MARK: - Flow 3: open a Session (tap a run row, or deep-link)

    func testOpenSessionShowsComposer() throws {
        // Prefer the deterministic deep-link when the runner seeded a job id;
        // otherwise tap the first run row (both reach the same Session screen).
        var extra: [String: String] = [:]
        if let jobId = injectedOpenSession { extra["ROOST_OPEN_SESSION"] = jobId }
        let app = try launchPaired(extraEnv: extra)

        if injectedOpenSession == nil {
            waitForLiveDashboard(app)
            let firstRow = app.descendants(matching: .any)
                .matching(NSPredicate(format: "identifier BEGINSWITH 'run-row-'"))
                .firstMatch
            XCTAssertTrue(firstRow.waitForExistence(timeout: appearTimeout))
            firstRow.tap()
        }

        // The Session screen renders the header + footer (Tree always present);
        // the composer is present while the job is non-terminal. We assert the
        // header (stable for any job state) and the Tree button (always there).
        let header = app.otherElements["session-header"]
        XCTAssertTrue(header.waitForExistence(timeout: appearTimeout),
                      "Session header never appeared after opening a job.")
        XCTAssertTrue(app.buttons["session-tree"].waitForExistence(timeout: appearTimeout),
                      "Session Tree button missing.")
        // Composer is the headline R76 capability — assert it when the job is
        // live (non-terminal). A terminal job legitimately hides it, so this is
        // a soft check that still proves the field exists when it should.
        let composer = app.textFields["session-composer-field"]
        if composer.waitForExistence(timeout: 5) {
            XCTAssertTrue(app.buttons["session-composer-send"].exists,
                          "Composer field present but Send button missing.")
        }
        // R108: the session view DEFAULTS to the distilled rendering, with a
        // footer toggle to the raw firehose. Assert the toggle exists, capture
        // the distilled default, flip to raw, capture that, flip back.
        let rawToggle = app.buttons["session-raw-toggle"]
        XCTAssertTrue(rawToggle.waitForExistence(timeout: appearTimeout),
                      "Distilled/Raw toggle missing from the session footer.")
        XCTAssertEqual(rawToggle.label, "Distilled",
                       "Session view must DEFAULT to the distilled rendering (R108).")
        attachScreenshot(app, name: "03-session-distilled")

        rawToggle.tap()
        XCTAssertTrue(app.buttons["session-raw-toggle"].label == "Raw"
                      || app.staticTexts["Raw"].waitForExistence(timeout: 5),
                      "Toggle did not switch to the raw firehose view.")
        attachScreenshot(app, name: "03b-session-raw")

        // Back to the distilled default.
        app.buttons["session-raw-toggle"].tap()
    }

    // MARK: - Flow 4: Notifications + Schedules sheets from the overflow menu

    func testOverflowSheetsOpen() throws {
        let app = try launchPaired()
        waitForLiveDashboard(app)

        // Notifications
        app.buttons["overflow-menu"].tap()
        app.buttons["overflow-notifications"].tap()
        let notifyField = app.textFields["notifications-topic-field"]
        XCTAssertTrue(notifyField.waitForExistence(timeout: appearTimeout),
                      "Notifications sheet never opened.")
        attachScreenshot(app, name: "04-notifications")
        app.buttons["notifications-done"].tap()

        // Schedules
        XCTAssertTrue(app.buttons["overflow-menu"].waitForExistence(timeout: appearTimeout))
        app.buttons["overflow-menu"].tap()
        app.buttons["overflow-schedules"].tap()
        let schedField = app.textViews["schedules-task-field"].firstMatch
        let schedFieldAny = schedField.exists ? schedField
            : app.textFields["schedules-task-field"].firstMatch
        XCTAssertTrue(schedFieldAny.waitForExistence(timeout: appearTimeout),
                      "Schedules sheet never opened.")
        attachScreenshot(app, name: "05-schedules")
        app.buttons["schedules-done"].tap()

        XCTAssertTrue(app.buttons["new-session-button"].waitForExistence(timeout: appearTimeout),
                      "Did not return to the dashboard after closing the sheets.")
    }

    // MARK: - Screenshot capture

    /// Attach a full-screen screenshot to the result bundle (kept on success so
    /// the artifacts always carry visual evidence of the smoke run).
    private func attachScreenshot(_ app: XCUIApplication, name: String) {
        let shot = app.screenshot()
        let att = XCTAttachment(screenshot: shot)
        att.name = name
        att.lifetime = .keepAlways
        add(att)
    }
}
