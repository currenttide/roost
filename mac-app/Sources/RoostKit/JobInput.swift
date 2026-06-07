import Foundation

/// Pure logic for the Send verb (`POST /jobs/{id}/input`, R38): steer a running
/// job by writing to its stdin. Delivery is honest about job kind — and that
/// honesty is the whole point of this helper, so it lives in the Linux-tested
/// layer rather than buried in a view.
///
/// Pinned to `roost/worker.py::_supports_live_input`: only a plain `command` job
/// runs an ordinary process whose stdin the worker keeps open mid-run. Agent kinds
/// (`claude`/`auto`/`codex`) run `claude -p` one-shot with stdin closed, and
/// `kind: docker` runs `docker run` without `-i` — so input to those is accepted by
/// the control plane (queued, never silently lost) but the owning worker marks it
/// `dropped` with a reason. We surface that up front instead of pretending it
/// landed (mobile API.md §4).
public enum InputDelivery: Equatable, Sendable {
    /// A `command` job: the worker writes the text to the live process stdin.
    case delivered
    /// An agent/docker job: the control plane queues it, but the worker drops it
    /// (stdin is closed). `reason` is the operator-facing explanation.
    case dropped(reason: String)

    /// True only when the input will actually reach a live process.
    public var isLive: Bool {
        if case .delivered = self { return true }
        return false
    }

    /// The drop reason, if this kind can't receive live input.
    public var dropReason: String? {
        if case .dropped(let reason) = self { return reason }
        return nil
    }
}

/// Classifies whether a job can receive interactive follow-up input on a live
/// stdin, from its submit `spec`. Mirrors `_supports_live_input` exactly:
/// `kind: auto`/`docker` never deliver; otherwise delivery needs a `command`
/// (agent kinds have none). The `spec` here is the same free-form object the
/// control plane echoes on `GET /jobs/{id}` (`Job.spec`).
public enum InputKindGate {
    /// The drop reason the worker attaches, paraphrased for the UI. Kept close to
    /// `worker.py::INPUT_DELIVERY_UNSUPPORTED` so the two don't drift.
    public static let dropReason =
        "This job kind runs with stdin closed — agent (claude/auto/codex) jobs run "
        + "one-shot and docker jobs run without an interactive stdin, so the message "
        + "would be dropped, not delivered."

    /// The `kind` strings whose processes never expose a writable stdin.
    private static let stdinClosedKinds: Set<String> = ["auto", "docker"]

    /// Whether a `command` value is actually present (string or argv array). Agent
    /// kinds carry no `command`; a `command` job is the shell/argv path the worker
    /// keeps stdin open for.
    private static func hasCommand(_ spec: JSONValue) -> Bool {
        if let s = spec["command"]?.stringValue, !s.isEmpty { return true }
        if let parts = spec["command"]?.arrayValue, !parts.isEmpty { return true }
        return false
    }

    /// True iff a job with this `spec` would receive input live (a plain `command`
    /// job whose kind isn't `auto`/`docker`). The inverse of "the worker drops it".
    public static func supportsLiveInput(spec: JSONValue) -> Bool {
        let kind = (spec["kind"]?.stringValue ?? "").lowercased()
        if stdinClosedKinds.contains(kind) { return false }
        return hasCommand(spec)
    }

    /// The honest delivery verdict for a job spec: `.delivered` for a command job,
    /// `.dropped(reason:)` otherwise. Drives the Send sheet's up-front warning.
    public static func delivery(for spec: JSONValue) -> InputDelivery {
        supportsLiveInput(spec: spec) ? .delivered : .dropped(reason: dropReason)
    }
}
