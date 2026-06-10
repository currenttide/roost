package oss.roost.mobile

import oss.roost.mobile.model.Parsers
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Decodes EVERY JSON fixture in mobile-app/fixtures/ and asserts the load-bearing fields.
 * This is the cross-platform contract guard (API.md §6): if the server changes a shape,
 * these tests pinpoint the drift.
 */
class ParserFixtureTest {

    @Test fun healthz() {
        val h = Parsers.parseHealthz(Fixtures.read("healthz.json"))
        assertTrue(h.ok)
        assertEquals("0.2.0", h.version)
    }

    @Test fun errors() {
        assertEquals("invalid bearer token", Parsers.parseError(401, Fixtures.read("error_401.json")).detail)
        assertEquals("admin auth required", Parsers.parseError(403, Fixtures.read("error_403_admin_endpoint.json")).detail)
        assertEquals("job not found", Parsers.parseError(404, Fixtures.read("error_404_job.json")).detail)
    }

    @Test fun pairTokenResponse() {
        // Not a PairPayload itself, but the mint response the QR feeds off — assert it
        // carries the mobile-scoped token shape.
        val o = org.json.JSONObject(Fixtures.read("pair_token_response.json"))
        assertEquals("mobile", o.getString("scope"))
        assertTrue(o.getString("token").startsWith("rst-mob-"))
    }

    @Test fun derived() {
        val d = Parsers.parseDerived(Fixtures.read("derived.json"))
        assertEquals("alert", d.fleetVerdict.level)
        assertFalse(d.fleetVerdict.isOk)
        assertEquals(6, d.runs.size)
        // R121: the fixture fleet is busy + idle-GPU + offline (API.md §2a).
        assertEquals(3, d.workers.size)
        // first worker is busy → live; the offline row is not.
        assertTrue(d.workers.first().isLive)
        assertFalse(d.workers.last().isLive)

        // Locate runs by goal/state — ids are random per fixture recording.
        val unplaceable = d.runs.first { it.goal == "train on the gpu box" }
        assertEquals("unplaceable", unplaceable.health.status)
        assertEquals("queued", unplaceable.state)
        // R86: an agent goal has no special summary — displayGoal == goal.
        assertEquals(unplaceable.goal, unplaceable.displayGoal)

        val verified = d.runs.first { it.state == "succeeded" }
        assertEquals("verified", verified.health.status)
        assertEquals(48213, verified.tokensUsed)
        assertEquals(true, verified.verified)
        // result was a plain string "fixed: tests green".
        assertEquals("fixed: tests green", verified.result)

        val running = d.runs.first { it.state == "running" }
        assertTrue(running.isActive)
        assertEquals("uv lock --upgrade ...", running.bestLine)

        // R85: the run row carries the job's EFFECTIVE kind, so the subtitle reads
        // the truth instead of a hardcoded "claude". A `command` job is "command";
        // a docker job is "docker"; an agent job (intent, no/claude kind) is "claude".
        assertEquals("claude", verified.kind)
        assertEquals("command", d.runs.first { it.goal == "echo ANDROID_UXTEST" }.kind)
        assertEquals("docker", d.runs.first { it.goal == "python -V" }.kind)

        // R86: the raw `command` row carries a glanceable `goal_display` summary
        // distinct from the full `goal`; displayGoal prefers it.
        val cmd = d.runs.first { it.goal.startsWith("cd /tmp") }
        assertTrue(cmd.goalDisplay!!.startsWith("curl"))
        assertTrue(cmd.displayGoal.startsWith("curl"))
        assertTrue(cmd.displayGoal != cmd.goal)
    }

    @Test fun workers() {
        val w = Parsers.parseWorkers(Fixtures.read("workers.json"))
        // R121: busy + idle-GPU + offline rows (FleetTest covers the full shape).
        assertEquals(3, w.size)
        assertEquals("fixture-node", w.first().name)
        assertEquals("busy", w.first().status)
        assertEquals("offline", w.last().status)
    }

    @Test fun jobDetailStates() {
        val queued = Parsers.parseJob(Fixtures.read("job_detail_queued.json"))
        assertEquals("queued", queued.state)
        assertNull(queued.workerId)
        // resubmit spec round-trips kind/intent.
        assertEquals("claude", queued.specKind)
        assertEquals("train on the gpu box", queued.specIntent)

        val running = Parsers.parseJob(Fixtures.read("job_detail_running.json"))
        assertEquals("running", running.state)
        assertNotNull(running.workerId)   // id value is random per recording

        val ok = Parsers.parseJob(Fixtures.read("job_detail_succeeded.json"))
        assertEquals("succeeded", ok.state)
        assertEquals(0, ok.exitCode)
        assertEquals(48213, ok.tokensUsed)
        // result object flattened: output/verified/evidence.
        assertEquals("fixed: tests green", ok.resultOutput)
        assertEquals(true, ok.resultVerified)
        assertEquals("pytest -q: 255 passed", ok.resultEvidence)
        assertTrue(ok.isTerminal)
    }

    @Test fun submitResponse() {
        val job = Parsers.parseJob(Fixtures.read("job_submit_response.json"))
        assertTrue(job.id.isNotEmpty())
        assertEquals("queued", job.state)
    }

    @Test fun jobsList() {
        val jobs = Parsers.parseJobs(Fixtures.read("jobs_list.json"))
        // 6 = the five R85 jobs + R86's long-`command` job; the golden lagged
        // the scenario until the R121 regen (values-only drift).
        assertEquals(6, jobs.size)
    }

    @Test fun tree() {
        val tree = Parsers.parseTree(Fixtures.read("job_tree.json"))
        assertEquals(1, tree.size)
        assertEquals("succeeded", tree.first().state)
        assertTrue(tree.first().id.isNotEmpty())
    }

    @Test fun runStory() {
        val run = Parsers.parseRunStory(Fixtures.read("job_derived_running.json"))
        assertTrue(run.runId.isNotEmpty())
        assertEquals("running", run.state)
        assertEquals("running", run.health.status)
    }

    @Test fun logPageFullAndResumed() {
        val full = Parsers.parseLogPage(Fixtures.read("job_logs.json"))
        assertTrue(full.jobId.isNotEmpty())
        assertEquals(6, full.logs.size)
        assertEquals(1, full.logs.first().seq)
        assertEquals("event", full.logs.first().stream)

        val resumed = Parsers.parseLogPage(Fixtures.read("job_logs_since_2.json"))
        // since=2 is EXCLUSIVE → first row is seq 3 (API.md §4).
        assertEquals(3, resumed.logs.first().seq)
        assertEquals(4, resumed.logs.size)
    }

    @Test fun cancelAck() {
        assertEquals(1, Parsers.parseCancel(Fixtures.read("job_cancel_response.json")).cancelled)
    }

    @Test fun publishFlow() {
        // Staged bundle (publish step 1, API.md §6).
        val blob = Parsers.parseBlob(Fixtures.read("blob_upload_response.json"))
        assertTrue(blob.isReady)
        assertEquals("phone-site.tar.gz", blob.name)
        assertTrue(blob.id.isNotEmpty())
        assertTrue(blob.expiresAt > blob.createdAt)

        // Published site (step 2): slug defaulted from the blob name stem.
        val site = Parsers.parseSite(Fixtures.read("publish_response.json"))
        assertEquals("phone-site", site.slug)
        assertTrue(site.url.endsWith("/pub/phone-site/"))
        assertNull(site.publicUrl)          // fixture CP has no publish domain
        assertEquals(site.url, site.shareUrl)
        assertEquals(1, site.files)

        // One-shot publish (API.md §6a): same Site shape, slug from ?name=.
        val shot = Parsers.parseSite(Fixtures.read("publish_oneshot_response.json"))
        assertEquals("phone-oneshot", shot.slug)
        assertTrue(shot.url.endsWith("/pub/phone-oneshot/"))
        assertNull(shot.publicUrl)
        assertEquals(shot.url, shot.shareUrl)
        assertEquals(1, shot.files)

        // List shape (covers sites from either flow).
        val sites = Parsers.parseSites(Fixtures.read("publish_list.json"))
        assertEquals(listOf("phone-oneshot", "phone-site"), sites.map { it.slug }.sorted())
    }

    @Test fun schedules() {
        // Created interval schedule (API.md §7a).
        val sched = Parsers.parseSchedule(Fixtures.read("schedule_create_response.json"))
        assertTrue(sched.id.isNotEmpty())
        assertEquals("nightly-tidy", sched.name)
        assertEquals(21600.0, sched.intervalSec, 0.0)   // "6h"
        assertTrue(sched.enabled)
        assertNull(sched.lastJobId)                      // not yet fired
        assertNull(sched.lastRunAt)
        assertNotNull(sched.nextRunAt)
        // The stored spec round-trips (the §3 submit shape).
        assertEquals("claude", sched.specKind)
        assertEquals("nightly: tidy the repo and run the tests", sched.spec["intent"])

        // List shape (API.md §7b).
        val list = Parsers.parseSchedules(Fixtures.read("schedules_list.json"))
        assertEquals(1, list.size)
        assertEquals(sched.id, list.first().id)
    }

    @Test fun followUpInput() {
        // Ack for POST /jobs/{id}/input (R38, API.md §4): queued immediately.
        val ack = Parsers.parseJobInputAck(Fixtures.read("job_input_response.json"))
        assertTrue(ack.inputId.isNotEmpty())
        assertTrue(ack.jobId.isNotEmpty())
        assertEquals("queued", ack.state)

        // GET /jobs/{id}/inputs: the job's queue. The fixture is the running job
        // with one freshly-queued input (no worker has pulled it yet).
        val inputs = Parsers.parseJobInputs(Fixtures.read("job_inputs_list.json"))
        assertEquals("running", inputs.state)          // the JOB's state
        assertEquals(ack.jobId, inputs.jobId)
        assertEquals(1, inputs.inputs.size)
        val row = inputs.inputs.first()
        assertEquals(ack.inputId, row.id)
        assertEquals("queued", row.state)
        assertTrue(row.isQueued)
        assertFalse(row.isDelivered)
        assertNull(row.deliveredAt)                    // not delivered yet
        assertNull(row.detail)
    }
}
