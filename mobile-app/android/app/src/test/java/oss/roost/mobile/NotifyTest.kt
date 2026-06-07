package oss.roost.mobile

import oss.roost.mobile.model.NotifyPayload
import oss.roost.mobile.model.NotifyRoute
import oss.roost.mobile.model.NotifyRouter
import oss.roost.mobile.model.NtfyTopic
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Pure-logic tests for push-notification client wiring (R37 / DESIGN.md §6 v1.1):
 * the ntfy-topic setting derivation and the payload→deep-link routing. Pure JVM
 * (org.json only), so they run on the kotlinc+JUnitCore harness.
 *
 * The CROSS-CONTRACT block parses payload literals lifted verbatim from the
 * server's own `tests/test_notify.py` emission so client/server drift is caught
 * here the moment the server changes a field name or shape.
 */
class NotifyTest {

    // ---- ntfy topic setting (NtfyTopic) ----

    @Test fun bareTopicGetsDefaultHost() {
        assertEquals("https://ntfy.sh/roost-yang", NtfyTopic.normalize("roost-yang"))
        assertEquals("https://ntfy.sh/roost_alerts", NtfyTopic.normalize("  roost_alerts  "))
    }

    @Test fun fullUrlPreserved() {
        assertEquals("https://ntfy.sh/roost-yang", NtfyTopic.normalize("https://ntfy.sh/roost-yang"))
        // Self-hosted server with a port; trailing slash dropped.
        assertEquals("http://ntfy.local:8080/mytopic", NtfyTopic.normalize("http://ntfy.local:8080/mytopic/"))
        // A query string is stripped to the bare subscribe URL.
        assertEquals("https://ntfy.sh/t", NtfyTopic.normalize("https://ntfy.sh/t?since=10"))
    }

    @Test fun hostWithoutSchemeDefaultsHttps() {
        assertEquals("https://ntfy.sh/roost-yang", NtfyTopic.normalize("ntfy.sh/roost-yang"))
    }

    @Test fun firstPathSegmentIsTheTopic() {
        assertEquals("https://ntfy.sh/roost", NtfyTopic.normalize("https://ntfy.sh/roost/json"))
    }

    @Test fun invalidTopicsRejected() {
        assertNull(NtfyTopic.normalize(""))
        assertNull(NtfyTopic.normalize("   "))
        assertNull(NtfyTopic.normalize("https://ntfy.sh/"))   // no topic segment
        assertNull(NtfyTopic.normalize("ntfy.sh/"))
        assertNull(NtfyTopic.normalize("bad topic"))          // space in bare topic
        assertNull(NtfyTopic.normalize("ftp://ntfy.sh/t"))    // non-http scheme
        assertNull(NtfyTopic.normalize("a".repeat(65)))       // over the 64 window
    }

    @Test fun displayTopicRoundTrips() {
        val url = NtfyTopic.normalize("roost-yang")!!
        assertEquals("roost-yang", NtfyTopic.displayTopic(url))
    }

    // ---- payload → route (NotifyRouter) ----

    @Test fun routesToSessionForJobId() {
        val json = """{"event":"job_terminal","job_id":"c7dedcc11a4c","state":"succeeded"}"""
        assertEquals(NotifyRoute.Session("c7dedcc11a4c"), NotifyRouter.route(json))
    }

    @Test fun malformedPayloadFallsBackToDashboard() {
        // Not JSON, JSON that isn't an object, missing/blank job_id — all dashboard.
        assertEquals(NotifyRoute.Dashboard, NotifyRouter.route("not json at all"))
        assertEquals(NotifyRoute.Dashboard, NotifyRouter.route("[1,2,3]"))
        assertEquals(NotifyRoute.Dashboard, NotifyRouter.route("{}"))
        assertEquals(NotifyRoute.Dashboard, NotifyRouter.route("""{"state":"failed"}"""))
        assertEquals(NotifyRoute.Dashboard, NotifyRouter.route("""{"job_id":""}"""))
        assertEquals(NotifyRoute.Dashboard, NotifyRouter.route("""{"job_id":"   "}"""))
        assertEquals(NotifyRoute.Dashboard, NotifyRouter.route("""{"job_id":null}"""))
    }

    @Test fun unknownFieldsIgnored() {
        // Additive-only contract: a future field must not break decode/route.
        val json = """{"event":"job_terminal","job_id":"x1","state":"succeeded","brand_new_field":42,"nested":{"a":1}}"""
        assertEquals(NotifyRoute.Session("x1"), NotifyRouter.route(json))
    }

    // ---- CROSS-CONTRACT: literals copied from tests/test_notify.py ----

    /**
     * Built from `_build_notification` (server) as pinned by
     * `tests/test_notify.py::test_build_notification_succeeded_payload`: the EXACT
     * field names + values the CP emits for a succeeded job. If the server
     * renames/drops a field, this decode breaks and flags the drift.
     */
    @Test fun contractSucceededPayloadDecodes() {
        // job_id "abc123", succeeded, intent "fix flaky auth test",
        // duration_sec 252.5, exit_code 0, worker_id "hubbase".
        val json = """{"event":"job_terminal","job_id":"abc123","state":"succeeded","intent":"fix flaky auth test","duration_sec":252.5,"exit_code":0,"worker_id":"hubbase","message":"succeeded: fix flaky auth test · 252.5s"}"""
        val p: NotifyPayload? = NotifyRouter.decode(json)
        assertNotNull(p)
        assertEquals("job_terminal", p!!.event)
        assertEquals("abc123", p.jobId)
        assertEquals("succeeded", p.state)
        assertEquals("fix flaky auth test", p.intent)
        assertEquals(252.5, p.durationSec!!, 0.0001)
        assertEquals(0, p.exitCode)
        assertEquals("hubbase", p.workerId)
        assertTrue(p.message!!.contains("fix flaky auth test"))
        assertEquals(NotifyRoute.Session("abc123"), NotifyRouter.route(p))
    }

    /** From `test_build_notification_failed_is_high_priority`: failed, exit 1, worker_id "pi4". */
    @Test fun contractFailedPayloadDecodes() {
        val json = """{"event":"job_terminal","job_id":"def456","state":"failed","intent":"migrate db schema","duration_sec":3.0,"exit_code":1,"worker_id":"pi4","message":"failed: migrate db schema · 3s"}"""
        val p = NotifyRouter.decode(json)
        assertNotNull(p)
        assertEquals("failed", p!!.state)
        assertEquals("migrate db schema", p.intent)
        assertEquals(1, p.exitCode)
        assertEquals("pi4", p.workerId)     // server emits the worker id
        assertEquals(NotifyRoute.Session("def456"), NotifyRouter.route(p))
    }

    /** From `test_build_notification_missing_timestamps_duration_none`: null duration. */
    @Test fun contractNullDurationDecodes() {
        val json = """{"event":"job_terminal","job_id":"x","state":"succeeded","intent":"t","duration_sec":null,"exit_code":null,"worker_id":null,"message":"succeeded: t"}"""
        val p = NotifyRouter.decode(json)
        assertNotNull(p)
        assertNull(p!!.durationSec)
        assertNull(p.exitCode)
        assertEquals(NotifyRoute.Session("x"), NotifyRouter.route(p))
    }
}
