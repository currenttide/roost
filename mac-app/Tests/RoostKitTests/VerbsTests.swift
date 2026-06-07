import XCTest
@testable import RoostKit

/// Linux-runnable tests for the menu-bar verb expansion (R62): Publish, Schedules,
/// Send. Covers the pure logic that must track the server (slug grammar, the `every`
/// grammar + every-format display, the input-kind delivery gate) and the new client
/// calls' request building + response decoding — the contracted evidence for this
/// item, since UI render isn't asserted on Linux.
final class VerbsTests: XCTestCase {

    // MARK: - Publish: slug grammar (mirrors roost/publish.py::normalize_slug)

    func testSlugNormalizeMatchesServer() {
        // strip → lowercase → spaces-to-hyphen, no validation.
        XCTAssertEqual(PublishSlug.normalize("  My Site  "), "my-site")
        XCTAssertEqual(PublishSlug.normalize("Already-Good"), "already-good")
        XCTAssertEqual(PublishSlug.normalize(""), "")
    }

    func testSlugValidityMatchesPattern() {
        // Accepted: starts alnum, then alnum/hyphen, ≤40.
        XCTAssertTrue(PublishSlug.isValid("my-site"))
        XCTAssertTrue(PublishSlug.isValid("a"))
        XCTAssertTrue(PublishSlug.isValid("0"))
        XCTAssertTrue(PublishSlug.isValid("My Site"))       // normalizes to my-site
        XCTAssertTrue(PublishSlug.isValid(String(repeating: "a", count: 40)))
        // Rejected.
        XCTAssertFalse(PublishSlug.isValid(""))
        XCTAssertFalse(PublishSlug.isValid("-leading"))     // can't start with hyphen
        XCTAssertFalse(PublishSlug.isValid("under_score"))  // underscore illegal
        XCTAssertFalse(PublishSlug.isValid("dots.here"))    // dot illegal
        XCTAssertFalse(PublishSlug.isValid("café"))         // non-ascii illegal
        XCTAssertFalse(PublishSlug.isValid(String(repeating: "a", count: 41)))  // >40
    }

    /// The mac-app's grammar must be byte-identical to the server's `_SLUG_RE`.
    func testSlugPatternIsPinnedToServer() {
        XCTAssertEqual(PublishSlug.pattern, "^[a-z0-9][a-z0-9-]{0,39}$")
    }

    func testSlugSuggestionFromFilename() {
        XCTAssertEqual(PublishSlug.suggestion(fromFilename: "My Site.tar.gz"), "my-site")
        XCTAssertEqual(PublishSlug.suggestion(fromFilename: "report.tgz"), "report")
        XCTAssertEqual(PublishSlug.suggestion(fromFilename: "docs_v2.tar"), "docs-v2")
        XCTAssertEqual(PublishSlug.suggestion(fromFilename: "a__b.tar.gz"), "a-b")  // collapse runs
        XCTAssertEqual(PublishSlug.suggestion(fromFilename: "----.tar.gz"), "")     // nothing survives
        // Over-length stem trims to the 40-char window and stays valid.
        let long = PublishSlug.suggestion(fromFilename: String(repeating: "x", count: 60) + ".tar.gz")
        XCTAssertTrue(PublishSlug.isValid(long))
        XCTAssertEqual(long.count, 40)
    }

    func testGzipSniff() {
        XCTAssertTrue(BundleCheck.looksLikeGzip(Data([0x1f, 0x8b, 0x08, 0x00])))
        XCTAssertFalse(BundleCheck.looksLikeGzip(Data([0x50, 0x4b])))  // zip
        XCTAssertFalse(BundleCheck.looksLikeGzip(Data([0x1f])))        // too short
        XCTAssertFalse(BundleCheck.looksLikeGzip(Data()))
    }

    // MARK: - Schedules: `every` grammar (mirrors server::parse_every + 30s floor)

    func testEveryParseUnitsAndBareNumber() {
        XCTAssertEqual(ScheduleInterval.parse("30s"), 30)
        XCTAssertEqual(ScheduleInterval.parse("5m"), 300)
        XCTAssertEqual(ScheduleInterval.parse("6h"), 21600)
        XCTAssertEqual(ScheduleInterval.parse("1d"), 86400)
        XCTAssertEqual(ScheduleInterval.parse("1.5h"), 5400)   // decimal allowed
        XCTAssertEqual(ScheduleInterval.parse("30M"), 1800)    // case-insensitive
        XCTAssertEqual(ScheduleInterval.parse(" 90 "), 90)     // bare number, trimmed
        XCTAssertNil(ScheduleInterval.parse("soon"))
        XCTAssertNil(ScheduleInterval.parse("10x"))            // unknown unit
        XCTAssertNil(ScheduleInterval.parse(""))
    }

    func testEveryValidityHonorsFloor() {
        // The server's exact 200-condition: parses AND >= 30s.
        XCTAssertEqual(ScheduleInterval.minSeconds, 30)
        XCTAssertTrue(ScheduleInterval.isValid("30s"))
        XCTAssertTrue(ScheduleInterval.isValid("30"))
        XCTAssertTrue(ScheduleInterval.isValid("5m"))
        XCTAssertFalse(ScheduleInterval.isValid("29s"))   // below floor
        XCTAssertFalse(ScheduleInterval.isValid("0"))
        XCTAssertFalse(ScheduleInterval.isValid("nope"))
    }

    func testEveryValidationMessageDistinguishesCases() {
        XCTAssertNil(ScheduleInterval.validationMessage(""))         // empty → no message yet
        XCTAssertNil(ScheduleInterval.validationMessage("5m"))       // valid → no message
        XCTAssertEqual(ScheduleInterval.validationMessage("15s"),
                       "Minimum interval is 30s.")
        XCTAssertEqual(ScheduleInterval.validationMessage("garbage"),
                       "Use seconds or <N>[smhd] — e.g. 30s, 15m, 6h, 1d.")
    }

    /// Every display path must match the CLI's `_fmt_interval` (largest whole unit).
    func testEveryFormatMatchesCLI() {
        XCTAssertEqual(ScheduleInterval.format(30), "30s")
        XCTAssertEqual(ScheduleInterval.format(90), "90s")     // not a whole minute
        XCTAssertEqual(ScheduleInterval.format(300), "5m")
        XCTAssertEqual(ScheduleInterval.format(3600), "1h")
        XCTAssertEqual(ScheduleInterval.format(21600), "6h")
        XCTAssertEqual(ScheduleInterval.format(86400), "1d")
        XCTAssertEqual(ScheduleInterval.format(90000), "25h")  // not a whole day
        XCTAssertEqual(ScheduleInterval.format(0), "0s")
    }

    func testEveryRelativeLabels() {
        let now = 1_000_000.0
        XCTAssertNil(ScheduleInterval.relative(to: nil, now: now))
        XCTAssertEqual(ScheduleInterval.relative(to: now + 300, now: now), "in 5m")
        XCTAssertEqual(ScheduleInterval.relative(to: now - 300, now: now), "5m ago")
        XCTAssertEqual(ScheduleInterval.relative(to: now, now: now), "now")  // due
    }

    func testEveryPresetsAreAllValid() {
        for preset in ["30s", "5m", "15m", "30m", "1h", "6h", "12h", "1d"] {
            XCTAssertTrue(ScheduleInterval.isValid(preset), "\(preset) should be valid")
        }
    }

    // MARK: - Send: input-kind delivery gate (mirrors worker::_supports_live_input)

    private func spec(_ json: String) throws -> JSONValue {
        try JSONDecoder().decode(JSONValue.self, from: Data(json.utf8))
    }

    func testCommandJobDeliversLive() throws {
        // A plain command (string) job, kind not auto/docker → delivered.
        XCTAssertTrue(InputKindGate.supportsLiveInput(
            spec: try spec(#"{"command": "tail -f log"}"#)))
        XCTAssertEqual(InputKindGate.delivery(
            for: try spec(#"{"command": "tail -f log"}"#)), .delivered)
        // argv-array command also delivers.
        XCTAssertTrue(InputKindGate.supportsLiveInput(
            spec: try spec(#"{"command": ["bash", "-c", "read x"]}"#)))
        // Explicit kind: command with a command → delivered.
        XCTAssertTrue(InputKindGate.supportsLiveInput(
            spec: try spec(#"{"kind": "command", "command": "cat"}"#)))
    }

    func testAgentAndDockerJobsDrop() throws {
        // kind: auto (agent) → dropped even if some command field were present.
        let auto = try spec(#"{"kind": "auto", "task": "fix the bug"}"#)
        XCTAssertFalse(InputKindGate.supportsLiveInput(spec: auto))
        XCTAssertFalse(InputKindGate.delivery(for: auto).isLive)
        XCTAssertNotNil(InputKindGate.delivery(for: auto).dropReason)
        // docker → dropped.
        let docker = try spec(#"{"kind": "docker", "image": "py", "command": "python x.py"}"#)
        XCTAssertFalse(InputKindGate.supportsLiveInput(spec: docker))
        XCTAssertEqual(InputKindGate.delivery(for: docker),
                       .dropped(reason: InputKindGate.dropReason))
    }

    func testNoCommandDrops() throws {
        // An intent-only job (no command, no auto/docker) has no live process stdin.
        XCTAssertFalse(InputKindGate.supportsLiveInput(
            spec: try spec(#"{"intent": "deploy the site"}"#)))
        // Empty command string is not a command.
        XCTAssertFalse(InputKindGate.supportsLiveInput(
            spec: try spec(#"{"command": ""}"#)))
        // Empty spec → drop.
        XCTAssertFalse(InputKindGate.supportsLiveInput(spec: try spec("{}")))
    }

    func testDeliveryEnumAccessors() {
        XCTAssertTrue(InputDelivery.delivered.isLive)
        XCTAssertNil(InputDelivery.delivered.dropReason)
        XCTAssertFalse(InputDelivery.dropped(reason: "x").isLive)
        XCTAssertEqual(InputDelivery.dropped(reason: "x").dropReason, "x")
    }

    // MARK: - Client request building (new endpoints)

    private func client() -> RoostClient {
        RoostClient(connection: RoostConnection(
            urlString: "http://hub:8787", token: "tok")!)
    }

    func testPublishBundleRequest() {
        var req = client().makeRequest("POST", "/publish", query: ["name": "my-site"])
        // The body + non-JSON content type are set on the actual call; assert the
        // route + auth + query that makeRequest produces.
        XCTAssertEqual(req.url?.absoluteString, "http://hub:8787/publish?name=my-site")
        XCTAssertEqual(req.value(forHTTPHeaderField: "Authorization"), "Bearer tok")
        XCTAssertEqual(req.httpMethod, "POST")
        // Content-Type for the one-shot body is application/gzip (non-JSON path).
        req.setValue("application/gzip", forHTTPHeaderField: "Content-Type")
        XCTAssertEqual(req.value(forHTTPHeaderField: "Content-Type"), "application/gzip")
    }

    func testScheduleAndInputRoutes() {
        XCTAssertEqual(
            client().makeRequest("GET", "/schedules").url?.absoluteString,
            "http://hub:8787/schedules")
        XCTAssertEqual(
            client().makeRequest("PATCH", "/schedules/abc123").url?.absoluteString,
            "http://hub:8787/schedules/abc123")
        XCTAssertEqual(
            client().makeRequest("DELETE", "/schedules/abc123").url?.absoluteString,
            "http://hub:8787/schedules/abc123")
        XCTAssertEqual(
            client().makeRequest("POST", "/jobs/j1/input").url?.absoluteString,
            "http://hub:8787/jobs/j1/input")
        XCTAssertEqual(
            client().makeRequest("GET", "/jobs/j1/inputs").url?.absoluteString,
            "http://hub:8787/jobs/j1/inputs")
    }

    func testMutationBodiesEncode() throws {
        XCTAssertEqual(
            try jsonObject(JobInputSubmit(text: "stop now"))["text"] as? String, "stop now")
        XCTAssertEqual(
            try jsonObject(SchedulePatchBody(enabled: false))["enabled"] as? Bool, false)
    }

    private func jsonObject(_ value: some Encodable) throws -> [String: Any] {
        let data = try JSONEncoder().encode(value)
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    // MARK: - Response decoding (fixtures mirror server payload shapes)

    func testDecodeSite() throws {
        // Shape from roost/publish.py::public_dict, with the public_url branch.
        let json = """
        {"slug": "demo", "url": "http://hub:8787/pub/demo/",
         "public_url": "https://demo.roost.pub/", "files": 3, "size": 4096,
         "created_at": 1765430000.0, "updated_at": 1765430500.0}
        """
        let site = try JSONDecoder().decode(Site.self, from: Data(json.utf8))
        XCTAssertEqual(site.slug, "demo")
        XCTAssertEqual(site.id, "demo")
        XCTAssertEqual(site.files, 3)
        XCTAssertEqual(site.shareURL, "https://demo.roost.pub/")  // prefers public
    }

    func testDecodeSiteWithoutPublicURL() throws {
        let json = #"{"slug": "x", "url": "http://hub:8787/pub/x/", "files": 1, "size": 10}"#
        let site = try JSONDecoder().decode(Site.self, from: Data(json.utf8))
        XCTAssertNil(site.publicURL)
        XCTAssertEqual(site.shareURL, "http://hub:8787/pub/x/")   // falls back to LAN
    }

    func testDecodeSchedule() throws {
        // Shape from roost/server.py::_schedule_to_public (enabled as 0/1).
        let json = """
        {"id": "sched12345678", "name": "nightly", "interval_sec": 86400,
         "enabled": 1, "next_run_at": 1765500000.0, "last_run_at": 1765413600.0,
         "last_job_id": "job987", "created_at": 1765400000.0,
         "spec": {"kind": "auto", "task": "summarize yesterday's commits"}}
        """
        let sched = try JSONDecoder().decode(Schedule.self, from: Data(json.utf8))
        XCTAssertEqual(sched.id, "sched12345678")
        XCTAssertTrue(sched.enabled)                       // integer 1 → true
        XCTAssertEqual(sched.intervalSec, 86400)
        XCTAssertEqual(ScheduleInterval.format(sched.intervalSec), "1d")
        XCTAssertEqual(sched.taskSummary, "summarize yesterday's commits")
        XCTAssertEqual(sched.lastJobID, "job987")
    }

    func testScheduleTaskSummaryFallbacks() throws {
        // command job → command text.
        let cmd = try JSONDecoder().decode(Schedule.self, from: Data("""
        {"id": "s1", "interval_sec": 3600, "enabled": 0,
         "spec": {"command": ["backup.sh", "--full"]}}
        """.utf8))
        XCTAssertFalse(cmd.enabled)                        // integer 0 → false
        XCTAssertEqual(cmd.taskSummary, "backup.sh --full")
        // nothing useful → name, then a generic label.
        let named = try JSONDecoder().decode(Schedule.self, from: Data("""
        {"id": "s2", "interval_sec": 3600, "enabled": 1, "name": "labelled", "spec": {}}
        """.utf8))
        XCTAssertEqual(named.taskSummary, "labelled")
        let bare = try JSONDecoder().decode(Schedule.self, from: Data("""
        {"id": "s3", "interval_sec": 3600, "enabled": 1, "spec": {"kind": "docker"}}
        """.utf8))
        XCTAssertEqual(bare.taskSummary, "docker job")
    }

    func testDecodeScheduleDeleteResponse() throws {
        let resp = try JSONDecoder().decode(
            ScheduleDeleteResponse.self, from: Data(#"{"deleted": true, "id": "s9"}"#.utf8))
        XCTAssertTrue(resp.deleted)
        XCTAssertEqual(resp.id, "s9")
    }

    func testDecodeJobInputAckAndList() throws {
        let ack = try JSONDecoder().decode(JobInputAck.self, from: Data("""
        {"input_id": "in123", "job_id": "j1", "state": "queued"}
        """.utf8))
        XCTAssertEqual(ack.inputID, "in123")
        XCTAssertEqual(ack.state, "queued")

        // The list carries the honest delivered/dropped outcome with a reason.
        let list = try JSONDecoder().decode(JobInputsResponse.self, from: Data("""
        {"job_id": "j1", "state": "running", "inputs": [
          {"id": "a", "state": "delivered", "detail": null,
           "created_at": 1.0, "delivered_at": 2.0, "created_by": "shared"},
          {"id": "b", "state": "dropped",
           "detail": "this job kind cannot receive live input",
           "created_at": 3.0, "delivered_at": null, "created_by": "client"}
        ]}
        """.utf8))
        XCTAssertEqual(list.inputs.count, 2)
        XCTAssertEqual(list.inputs[0].state, "delivered")
        XCTAssertEqual(list.inputs[1].state, "dropped")
        XCTAssertEqual(list.inputs[1].detail, "this job kind cannot receive live input")
    }
}
