import XCTest
@testable import Roost

/// Decode EVERY golden fixture through the app's Codable models (API.md §6).
/// If the server changes a shape, these pinpoint the drift.
final class DecodeTests: XCTestCase {
    private let dec = JSONDecoder()

    func testDerived() throws {
        let d = try dec.decode(Derived.self, from: Fixtures.data("derived.json"))
        XCTAssertEqual(d.fleetVerdict.level, "alert")
        XCTAssertTrue(d.fleetVerdict.isAlert)
        XCTAssertEqual(d.runs.count, 3)
        XCTAssertEqual(d.workers.count, 1)
        // Locate runs by goal/state — ids are random per fixture recording.
        // Run-row `result` is a STRING here (not the job-detail object).
        let verified = d.runs.first { $0.state == "succeeded" }
        XCTAssertEqual(verified?.result, "fixed: tests green")
        XCTAssertEqual(verified?.healthStatus, .verified)
        XCTAssertEqual(verified?.healthStatus.glyph, "✓")
        // Unplaceable run keeps its glyph + error classification.
        let unplaceable = d.runs.first { $0.goal == "train on the gpu box" }
        XCTAssertEqual(unplaceable?.healthStatus, .unplaceable)
        XCTAssertTrue(unplaceable?.healthStatus.isError ?? false)
        // Live worker count = idle+busy.
        XCTAssertTrue(d.workers[0].isLive)
    }

    func testJobDetails() throws {
        for name in ["job_detail_queued.json", "job_detail_running.json",
                     "job_detail_succeeded.json", "job_submit_response.json"] {
            let job = try dec.decode(Job.self, from: Fixtures.data(name))
            XCTAssertFalse(job.id.isEmpty, "\(name)")
            XCTAssertNotNil(job.spec, "\(name) spec")
        }
        let ok = try dec.decode(Job.self, from: Fixtures.data("job_detail_succeeded.json"))
        // Job-detail `result` is the OBJECT shape.
        XCTAssertEqual(ok.result?.output, "fixed: tests green")
        XCTAssertEqual(ok.result?.verified, true)
        XCTAssertEqual(ok.exitCode, 0)
        XCTAssertEqual(ok.tokensUsed, 48213)
        // requires carries heterogeneous JSON (array under "tools").
        XCTAssertNotNil(ok.spec?.requires?["tools"])
    }

    func testRequiresHeterogeneous() throws {
        // queued job pins gpu_vram_gb as a STRING ">=24"; ensure JSONValue copes.
        let q = try dec.decode(Job.self, from: Fixtures.data("job_detail_queued.json"))
        XCTAssertEqual(q.requires?["gpu_vram_gb"], .string(">=24"))
        XCTAssertEqual(q.spec?.kind, "claude")
    }

    func testJobsListAndTree() throws {
        let list = try dec.decode([Job].self, from: Fixtures.data("jobs_list.json"))
        XCTAssertEqual(list.count, 3)
        let tree = try dec.decode([Job].self, from: Fixtures.data("job_tree.json"))
        XCTAssertEqual(tree.count, 1)
        // Ids are random per recording; pin shape, not value.
        XCTAssertEqual(tree[0].state, "succeeded")
        XCTAssertFalse(tree[0].id.isEmpty)
    }

    func testLogs() throws {
        let page = try dec.decode(LogPage.self, from: Fixtures.data("job_logs.json"))
        XCTAssertEqual(page.logs.count, 6)
        XCTAssertEqual(page.logs.first?.stream, "event")
        XCTAssertEqual(page.logs[1].data, "running pytest -q ...")
        let since = try dec.decode(LogPage.self, from: Fixtures.data("job_logs_since_2.json"))
        // Resumed page starts at seq 3 (since=2, exclusive).
        XCTAssertEqual(since.logs.first?.seq, 3)
    }

    func testHeaderAndWorkers() throws {
        let run = try dec.decode(Run.self, from: Fixtures.data("job_derived_running.json"))
        XCTAssertEqual(run.healthStatus, .running)
        XCTAssertEqual(run.healthStatus.glyph, "▶")
        let workers = try dec.decode([Worker].self, from: Fixtures.data("workers.json"))
        XCTAssertEqual(workers.first?.status, "busy")
        XCTAssertTrue(workers.first?.isLive ?? false)
    }

    func testHealthzPairAndCancel() throws {
        let h = try dec.decode(Healthz.self, from: Fixtures.data("healthz.json"))
        XCTAssertTrue(h.ok)
        XCTAssertEqual(h.version, "0.2.0")
        let pt = try dec.decode(PairTokenResponse.self,
                                from: Fixtures.data("pair_token_response.json"))
        XCTAssertEqual(pt.scope, "mobile")
        XCTAssertTrue(pt.token.hasPrefix("rst-mob-"))
        let cancel = try dec.decode(CancelResponse.self,
                                    from: Fixtures.data("job_cancel_response.json"))
        XCTAssertEqual(cancel.cancelled, 1)
    }

    func testErrorEnvelopes() throws {
        for name in ["error_401.json", "error_403_admin_endpoint.json",
                     "error_404_job.json"] {
            let e = try dec.decode(ErrorEnvelope.self, from: Fixtures.data(name))
            XCTAssertFalse(e.detail.isEmpty, name)
        }
        // mapError routing.
        XCTAssertEqual(ApiClient.mapError(status: 401,
                       data: Fixtures.data("error_401.json")), .unauthorized)
        if case .forbidden(let d) = ApiClient.mapError(status: 403,
                       data: Fixtures.data("error_403_admin_endpoint.json")) {
            XCTAssertEqual(d, "admin auth required")
        } else { XCTFail("expected forbidden") }
        if case .notFound = ApiClient.mapError(status: 404,
                       data: Fixtures.data("error_404_job.json")) {} else {
            XCTFail("expected notFound")
        }
    }

    /// Belt-and-suspenders: every JSON fixture must at least parse as JSON,
    /// proving the test bundle actually carries them.
    func testAllFixturesPresentAndValidJSON() throws {
        for name in Fixtures.allJSON {
            let data = Fixtures.data(name)
            XCTAssertFalse(data.isEmpty, "missing fixture \(name)")
            XCTAssertNoThrow(try JSONSerialization.jsonObject(with: data), name)
        }
    }
}
