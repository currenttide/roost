import XCTest
@testable import RoostKit

/// Fixtures mirror the real payload shapes produced by roost/server.py
/// (_derive_run, _fleet_verdict, _list_workers, _read_logs). If the backend
/// changes a field the app reads, these are the tests that should break.
final class ModelDecodingTests: XCTestCase {

    // MARK: /derived

    let derivedFixture = """
    {
      "generated_at": 1765432100.5,
      "fleet_verdict": {"level": "ok", "summary": "3 nodes · 1 running · 1 verifying — all healthy"},
      "workers": [
        {
          "id": "abc123def456", "name": "dgx-1", "status": "busy",
          "capabilities": {
            "os": "linux", "arch": "x86_64", "hostname": "dgx-1", "cpus": 64,
            "ram_gb": 512.0, "gpu": ["NVIDIA A100"], "gpu_count": 1,
            "gpu_vram_gb": 80.0, "tools": ["claude", "docker", "git"],
            "load": {"capacity": 2, "running": 1, "loadavg1": 3.2, "free_vram_gb": 79.0}
          },
          "registered_at": 1765000000.0, "last_seen": 1765432099.0,
          "enroll_id": "tok-hash", "policy": {"trust_skip_perms": true},
          "last_assigned_at": 1765432000.0, "revoked": 0,
          "capacity": 2, "running": 1
        },
        {
          "id": "fffffff00000", "name": "pi-4", "status": "idle",
          "capabilities": {"os": "linux", "arch": "aarch64", "cpus": 4, "tools": []},
          "registered_at": 1765000001.0, "last_seen": 1765432098.0,
          "policy": {}, "revoked": 0, "capacity": 1, "running": 0
        }
      ],
      "runs": [
        {
          "run_id": "1a2b3c4d5e6f", "goal": "retrain embeddings on the GPU box",
          "state": "running", "phase": "running",
          "health": {"status": "running", "reason": "fine-tuning epoch 3/5"},
          "worker": "abc123def456", "verified": null, "evidence": null,
          "result": "", "diagnosis": null,
          "last_activity": "fine-tuning epoch 3/5", "idle_sec": 4.2,
          "queued_sec": null, "capable_workers": null, "decline_count": 0,
          "cost": {"tokens_used": 3100, "cost_est_usd": 0.0366},
          "narration": "fine-tuning epoch 3/5, loss 0.041", "progress": 61,
          "eta_sec": 480, "root_job_id": "1a2b3c4d5e6f",
          "created_at": 1765431000.0, "finished_at": null
        },
        {
          "run_id": "9f8e7d6c5b4a", "goal": "report free VRAM on a GPU box",
          "state": "succeeded", "phase": "succeeded",
          "health": {"status": "verified", "reason": "nvidia-smi output matches"},
          "worker": "abc123def456", "verified": true,
          "evidence": "nvidia-smi output matches",
          "result": "A100, 79 GB free", "diagnosis": null,
          "last_activity": "done", "idle_sec": null, "queued_sec": null,
          "capable_workers": null, "decline_count": 0,
          "cost": {"tokens_used": 2100, "cost_est_usd": 0.0306, "budget_pct": 12.5},
          "narration": null, "progress": null, "eta_sec": null,
          "root_job_id": "9f8e7d6c5b4a",
          "created_at": 1765430000.0, "finished_at": 1765430200.0
        },
        {
          "run_id": "deadbeef0000", "goal": "build docs on windows box",
          "state": "failed", "phase": "failed",
          "health": {"status": "failed", "reason": "npm not installed"},
          "worker": "fffffff00000", "verified": null, "evidence": null,
          "result": "npm: command not found",
          "diagnosis": "npm not installed on win-wsl",
          "last_activity": null, "idle_sec": null, "queued_sec": null,
          "capable_workers": null, "decline_count": 1,
          "cost": {"tokens_used": 0, "cost_est_usd": 0.0},
          "narration": null, "progress": null, "eta_sec": null,
          "root_job_id": "deadbeef0000",
          "created_at": 1765429000.0, "finished_at": 1765429100.0
        }
      ]
    }
    """

    func testDecodeDerivedSnapshot() throws {
        let snap = try JSONDecoder().decode(
            DerivedSnapshot.self, from: Data(derivedFixture.utf8))

        XCTAssertEqual(snap.fleetVerdict.level, .ok)
        XCTAssertEqual(snap.workers.count, 2)
        XCTAssertEqual(snap.runs.count, 3)

        let dgx = snap.workers[0]
        XCTAssertEqual(dgx.name, "dgx-1")
        XCTAssertEqual(dgx.status, .busy)
        XCTAssertEqual(dgx.capacity, 2)
        XCTAssertEqual(dgx.running, 1)
        XCTAssertFalse(dgx.revoked)             // integer 0 → false
        XCTAssertEqual(dgx.gpuNames, ["NVIDIA A100"])
        XCTAssertEqual(dgx.freeVRAMGB, 79.0)
        XCTAssertTrue(dgx.hasClaude)
        XCTAssertTrue(dgx.headline.contains("A100"))

        let running = snap.runs[0]
        XCTAssertTrue(running.isActive)
        XCTAssertEqual(running.progress, 61)
        XCTAssertEqual(running.etaSec, 480)
        XCTAssertNil(running.verified)

        let verifiedRun = snap.runs[1]
        XCTAssertTrue(verifiedRun.isTerminal)
        XCTAssertEqual(verifiedRun.verified, true)
        XCTAssertEqual(verifiedRun.health.status, "verified")
        XCTAssertEqual(verifiedRun.cost.budgetPct, 12.5)
        XCTAssertEqual(verifiedRun.cost.tokensUsed, 2100)

        let failed = snap.runs[2]
        XCTAssertEqual(failed.diagnosis, "npm not installed on win-wsl")
    }

    func testUnknownFieldsAndMissingOptionalsAreTolerated() throws {
        // A future backend adds fields and drops optionals — must still decode.
        let future = """
        {
          "generated_at": 1.0, "schema_rev": 99,
          "fleet_verdict": {"level": "alert", "summary": "1 need attention", "new_field": [1,2]},
          "workers": [{"id": "w1", "name": "x", "status": "hibernating", "shiny": true}],
          "runs": [{"run_id": "r1", "state": "queued", "phase": "queued",
                    "health": {"status": "queued", "reason": "queued"},
                    "cost": {"tokens_used": 0, "cost_est_usd": 0}}]
        }
        """
        let snap = try JSONDecoder().decode(DerivedSnapshot.self, from: Data(future.utf8))
        XCTAssertEqual(snap.fleetVerdict.level, .alert)
        XCTAssertEqual(snap.workers[0].status, .unknown)  // unfamiliar status string
        XCTAssertEqual(snap.runs[0].id, "r1")
        XCTAssertEqual(snap.runs[0].cost.tokensUsed, 0)
    }

    func testMalformedRowDoesNotSinkSnapshot() throws {
        let mixed = """
        {
          "generated_at": 1.0,
          "fleet_verdict": {"level": "ok", "summary": ""},
          "workers": [{"name": "no-id"}, {"id": "w2", "name": "ok", "status": "idle"}],
          "runs": [{"goal": "no run_id"}, {"run_id": "r2", "state": "running",
                    "phase": "running", "health": {"status": "running", "reason": ""},
                    "cost": {"tokens_used": 0, "cost_est_usd": 0}}]
        }
        """
        let snap = try JSONDecoder().decode(DerivedSnapshot.self, from: Data(mixed.utf8))
        XCTAssertEqual(snap.workers.map(\.id), ["w2"])
        XCTAssertEqual(snap.runs.map(\.id), ["r2"])
    }

    // MARK: goal_display (R86)

    func testDisplayGoalPrefersServerSummaryWithFallback() throws {
        // A command run carries the server's glanceable `goal_display` summary;
        // `displayGoal` shows it, while `goal` keeps the full shell text.
        let withSummary = """
        {"run_id": "c1", "state": "running", "phase": "running",
         "goal": "cd /tmp && curl -s -o run.sh 'http://h/blobs/x' && bash run.sh",
         "goal_display": "curl -s -o run.sh 'http://h/blobs/x'…",
         "health": {"status": "running", "reason": ""},
         "cost": {"tokens_used": 0, "cost_est_usd": 0}}
        """
        let run = try JSONDecoder().decode(Run.self, from: Data(withSummary.utf8))
        XCTAssertEqual(run.displayGoal, "curl -s -o run.sh 'http://h/blobs/x'…")
        XCTAssertTrue(run.goal.hasPrefix("cd /tmp"))  // full text preserved

        // Older control plane: no `goal_display` → fall back to the full goal.
        let noSummary = """
        {"run_id": "c2", "state": "running", "phase": "running",
         "goal": "echo ok", "health": {"status": "running", "reason": ""},
         "cost": {"tokens_used": 0, "cost_est_usd": 0}}
        """
        let run2 = try JSONDecoder().decode(Run.self, from: Data(noSummary.utf8))
        XCTAssertNil(run2.goalDisplay)
        XCTAssertEqual(run2.displayGoal, "echo ok")

        // An empty-string `goal_display` is treated as absent (fall back).
        let emptySummary = """
        {"run_id": "c3", "state": "running", "phase": "running",
         "goal": "agent goal", "goal_display": "",
         "health": {"status": "running", "reason": ""},
         "cost": {"tokens_used": 0, "cost_est_usd": 0}}
        """
        let run3 = try JSONDecoder().decode(Run.self, from: Data(emptySummary.utf8))
        XCTAssertEqual(run3.displayGoal, "agent goal")
    }

    // MARK: raw jobs

    func testDecodeJobAndGoalText() throws {
        let jobJSON = """
        {
          "id": "1a2b3c4d5e6f", "state": "running",
          "spec": {"task": "lint the repo", "kind": "auto", "verify": true},
          "intent": null, "requires": {}, "worker_id": "w1",
          "created_at": 1765431000.0, "assigned_at": 1765431001.0,
          "started_at": 1765431002.0, "finished_at": null,
          "exit_code": null, "result": null, "error": null,
          "lease_expires_at": 1765431060.0, "attempt": 1, "max_attempts": 2,
          "parent_job_id": null, "root_job_id": "1a2b3c4d5e6f",
          "depth": 0, "max_depth": 3, "tokens_used": 1200,
          "last_activity_at": 1765431050.0, "last_activity": "linting roost/",
          "decline_count": 0, "declined_by": null,
          "narration": null, "progress": null, "eta_sec": null, "diagnosis": null,
          "model": null, "subagent_model": null, "idle_sec": 3.0
        }
        """
        let job = try JSONDecoder().decode(Job.self, from: Data(jobJSON.utf8))
        XCTAssertEqual(job.goal, "lint the repo")
        XCTAssertEqual(job.tokensUsed, 1200)
        XCTAssertFalse(job.isTerminal)
    }

    func testJobGoalFallsBackToCommand() throws {
        let jobJSON = """
        {"id": "x", "state": "queued", "spec": {"command": ["echo", "hi"]}}
        """
        let job = try JSONDecoder().decode(Job.self, from: Data(jobJSON.utf8))
        XCTAssertEqual(job.goal, "echo hi")
    }

    func testJobResultObject() throws {
        let jobJSON = """
        {"id": "x", "state": "succeeded",
         "spec": {"task": "t"},
         "result": {"verified": true, "evidence": "checked", "output": "done"}}
        """
        let job = try JSONDecoder().decode(Job.self, from: Data(jobJSON.utf8))
        XCTAssertEqual(job.result?["verified"]?.boolValue, true)
        XCTAssertEqual(job.result?["output"]?.stringValue, "done")
    }

    // MARK: logs

    func testDecodeLogsResponse() throws {
        let logsJSON = """
        {"job_id": "j1", "state": "running", "logs": [
          {"seq": 1, "stream": "stdout", "data": "hello", "ts": 1765431010.0},
          {"seq": 2, "stream": "stderr", "data": "warn: x", "ts": 1765431011.0},
          {"seq": 3, "stream": "event", "data": "{\\"type\\": \\"progress\\"}", "ts": 1765431012.0}
        ]}
        """
        let resp = try JSONDecoder().decode(LogsResponse.self, from: Data(logsJSON.utf8))
        XCTAssertEqual(resp.logs.count, 3)
        XCTAssertEqual(resp.logs[0].text, "hello")
        XCTAssertEqual(resp.logs[1].stream, "stderr")
        XCTAssertEqual(resp.logs[2].seq, 3)
    }

    // MARK: misc payloads

    func testDecodeMisc() throws {
        let h = try JSONDecoder().decode(
            Healthz.self, from: Data(#"{"ok": true, "version": "0.2.0"}"#.utf8))
        XCTAssertTrue(h.ok)
        XCTAssertEqual(h.version, "0.2.0")

        let c = try JSONDecoder().decode(
            CancelResponse.self, from: Data(#"{"cancelled": 3}"#.utf8))
        XCTAssertEqual(c.cancelled, 3)

        let p = try JSONDecoder().decode(
            PruneResponse.self, from: Data(#"{"pruned": 2, "names": ["a", "b"]}"#.utf8))
        XCTAssertEqual(p.pruned, 2)
        XCTAssertEqual(p.names, ["a", "b"])
    }
}
