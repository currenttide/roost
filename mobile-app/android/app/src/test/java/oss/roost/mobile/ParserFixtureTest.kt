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
        assertEquals(3, d.runs.size)
        assertEquals(1, d.workers.size)
        // worker is busy → live.
        assertTrue(d.workers.first().isLive)

        // Locate runs by goal/state — ids are random per fixture recording.
        val unplaceable = d.runs.first { it.goal == "train on the gpu box" }
        assertEquals("unplaceable", unplaceable.health.status)
        assertEquals("queued", unplaceable.state)

        val verified = d.runs.first { it.state == "succeeded" }
        assertEquals("verified", verified.health.status)
        assertEquals(48213, verified.tokensUsed)
        assertEquals(true, verified.verified)
        // result was a plain string "fixed: tests green".
        assertEquals("fixed: tests green", verified.result)

        val running = d.runs.first { it.state == "running" }
        assertTrue(running.isActive)
        assertEquals("uv lock --upgrade ...", running.bestLine)
    }

    @Test fun workers() {
        val w = Parsers.parseWorkers(Fixtures.read("workers.json"))
        assertEquals(1, w.size)
        assertEquals("fixture-node", w.first().name)
        assertEquals("busy", w.first().status)
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
        assertEquals(3, jobs.size)
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

        // List shape.
        val sites = Parsers.parseSites(Fixtures.read("publish_list.json"))
        assertEquals(listOf("phone-site"), sites.map { it.slug })
    }
}
