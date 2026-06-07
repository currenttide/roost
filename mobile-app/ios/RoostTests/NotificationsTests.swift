import XCTest
@testable import Roost

/// Pure-logic tests for push-notification client wiring (R37 / DESIGN.md §6
/// v1.1): the ntfy-topic setting derivation and the payload→deep-link routing.
/// All Foundation-only, so they run on the Linux harness AND in the iOS bundle.
///
/// The CROSS-CONTRACT test below parses payload literals lifted verbatim from
/// the server's own `tests/test_notify.py` emission so client/server drift is
/// caught here the moment the server changes a field name or shape.
final class NotificationsTests: XCTestCase {

    // MARK: - ntfy topic setting (NtfyTopic)

    func testBareTopicGetsDefaultHost() {
        XCTAssertEqual(NtfyTopic.normalize("roost-yang"), "https://ntfy.sh/roost-yang")
        XCTAssertEqual(NtfyTopic.normalize("  roost_alerts  "),
                       "https://ntfy.sh/roost_alerts")
    }

    func testFullURLPreserved() {
        XCTAssertEqual(NtfyTopic.normalize("https://ntfy.sh/roost-yang"),
                       "https://ntfy.sh/roost-yang")
        // Self-hosted server with a port; trailing slash dropped.
        XCTAssertEqual(NtfyTopic.normalize("http://ntfy.local:8080/mytopic/"),
                       "http://ntfy.local:8080/mytopic")
        // A query string is stripped to the bare subscribe URL.
        XCTAssertEqual(NtfyTopic.normalize("https://ntfy.sh/t?since=10"),
                       "https://ntfy.sh/t")
    }

    func testHostWithoutSchemeDefaultsHttps() {
        XCTAssertEqual(NtfyTopic.normalize("ntfy.sh/roost-yang"),
                       "https://ntfy.sh/roost-yang")
    }

    func testInvalidTopicsRejected() {
        // No topic segment, illegal characters, over the 64-char window, junk.
        XCTAssertNil(NtfyTopic.normalize(""))
        XCTAssertNil(NtfyTopic.normalize("   "))
        XCTAssertNil(NtfyTopic.normalize("https://ntfy.sh/"))   // no topic
        XCTAssertNil(NtfyTopic.normalize("ntfy.sh/"))
        XCTAssertNil(NtfyTopic.normalize("bad topic"))          // space in bare topic
        XCTAssertNil(NtfyTopic.normalize(String(repeating: "a", count: 65)))
    }

    func testFirstPathSegmentIsTheTopic() {
        // ntfy subscribe URLs are host/<topic>; deeper paths keep the first seg.
        XCTAssertEqual(NtfyTopic.normalize("https://ntfy.sh/roost/json"),
                       "https://ntfy.sh/roost")
    }

    func testDisplayTopicRoundTrips() {
        let url = NtfyTopic.normalize("roost-yang")!
        XCTAssertEqual(NtfyTopic.displayTopic(url), "roost-yang")
    }

    // MARK: - payload → route (NotifyRouter)

    func testRoutesToSessionForJobId() {
        let json = #"{"event":"job_terminal","job_id":"c7dedcc11a4c","state":"succeeded"}"#
        XCTAssertEqual(NotifyRouter.route(json: json), .session(jobId: "c7dedcc11a4c"))
    }

    func testMalformedPayloadFallsBackToDashboard() {
        // Not JSON, JSON that isn't an object, missing job_id, blank job_id — all
        // must land on the dashboard, never crash, never guess an id.
        XCTAssertEqual(NotifyRouter.route(json: "not json at all"), .dashboard)
        XCTAssertEqual(NotifyRouter.route(json: "[1,2,3]"), .dashboard)
        XCTAssertEqual(NotifyRouter.route(json: "{}"), .dashboard)
        XCTAssertEqual(NotifyRouter.route(json: #"{"state":"failed"}"#), .dashboard)
        XCTAssertEqual(NotifyRouter.route(json: #"{"job_id":""}"#), .dashboard)
        XCTAssertEqual(NotifyRouter.route(json: #"{"job_id":"   "}"#), .dashboard)
        XCTAssertEqual(NotifyRouter.route(data: Data()), .dashboard)
    }

    func testUnknownFieldsIgnored() {
        // Additive-only contract: a future field must not break decode/route.
        let json = #"""
        {"event":"job_terminal","job_id":"x1","state":"succeeded",
         "brand_new_field":42,"nested":{"a":1}}
        """#
        XCTAssertEqual(NotifyRouter.route(json: json), .session(jobId: "x1"))
    }

    // MARK: - CROSS-CONTRACT: literals copied from tests/test_notify.py

    /// Built from `_build_notification` (server) as pinned by
    /// `tests/test_notify.py::test_build_notification_succeeded_payload`: the
    /// EXACT field names + values the CP emits for a succeeded job. If the server
    /// renames/drops a field, this decode breaks and flags the drift.
    func testContractSucceededPayloadDecodes() {
        // job_id "abc123", succeeded, intent "fix flaky auth test",
        // duration_sec 252.5, exit_code 0, worker_id "hubbase".
        let json = #"""
        {"event":"job_terminal","job_id":"abc123","state":"succeeded",
         "intent":"fix flaky auth test","duration_sec":252.5,"exit_code":0,
         "worker_id":"hubbase","message":"succeeded: fix flaky auth test · 252.5s"}
        """#
        let p = NotifyRouter.decode(json: json)
        XCTAssertNotNil(p)
        XCTAssertEqual(p?.event, "job_terminal")
        XCTAssertEqual(p?.jobId, "abc123")
        XCTAssertEqual(p?.state, "succeeded")
        XCTAssertEqual(p?.intent, "fix flaky auth test")
        XCTAssertEqual(p?.durationSec, 252.5)
        XCTAssertEqual(p?.exitCode, 0)
        XCTAssertEqual(p?.workerId, "hubbase")
        XCTAssertTrue(p?.message?.contains("fix flaky auth test") ?? false)
        // …and it routes to that job's session.
        XCTAssertEqual(NotifyRouter.route(p), .session(jobId: "abc123"))
    }

    /// From `test_build_notification_failed_is_high_priority`: failed job, exit 1,
    /// intent pulled from `spec.task`, worker_id "pi4". Pins the failed shape with
    /// the EXACT worker_id the server emits for that fixture.
    func testContractFailedPayloadDecodes() {
        let json = #"""
        {"event":"job_terminal","job_id":"def456","state":"failed",
         "intent":"migrate db schema","duration_sec":3.0,"exit_code":1,
         "worker_id":"pi4","message":"failed: migrate db schema · 3s"}
        """#
        let p = NotifyRouter.decode(json: json)
        XCTAssertEqual(p?.state, "failed")
        XCTAssertEqual(p?.intent, "migrate db schema")
        XCTAssertEqual(p?.exitCode, 1)
        XCTAssertEqual(p?.workerId, "pi4")   // server emits the worker id
        XCTAssertEqual(NotifyRouter.route(p), .session(jobId: "def456"))
    }

    /// From `test_build_notification_missing_timestamps_duration_none`: a
    /// `duration_sec: null` payload must decode (duration nil) and still route.
    func testContractNullDurationDecodes() {
        let json = #"{"event":"job_terminal","job_id":"x","state":"succeeded","intent":"t","duration_sec":null,"exit_code":null,"worker_id":null,"message":"succeeded: t"}"#
        let p = NotifyRouter.decode(json: json)
        XCTAssertNotNil(p)
        XCTAssertNil(p?.durationSec)
        XCTAssertNil(p?.exitCode)
        XCTAssertEqual(NotifyRouter.route(p), .session(jobId: "x"))
    }
}
