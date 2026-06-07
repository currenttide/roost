import Foundation

/// Push-notification client logic for the R37 / DESIGN.md §6 (v1.1) terminal-state
/// notifier. Two PURE pieces, so the Linux harness covers them:
///
///  1. `NtfyTopic` — derive/validate the ntfy subscribe URL the app watches. The
///     control plane is configured with a `--notify-url` (an ntfy topic) and POSTs
///     there on terminal jobs; it does NOT expose that topic over the API, so the
///     app takes it as a SETTING (manual entry per DESIGN.md §6 — "ntfy.sh
///     self-hosted or UnifiedPush-style webhooks"). The user can paste a full
///     `https://ntfy.sh/<topic>` URL or just the bare topic name.
///  2. `NotifyRoute` — map an incoming R37 payload to an in-app destination
///     (the job's Session view, or a safe Dashboard fallback for a malformed
///     payload). This is the deep-link routing the tapped notification triggers.
///
/// The DEVICE-ONLY half — registering with a UnifiedPush distributor, holding the
/// foreground-service subscription, and rendering the system notification — lives
/// in `PushService` (below), which is `#if canImport(UIKit)` and untested here
/// (no devices); these pure types are what the system notification handler calls
/// once a payload arrives.

// MARK: - ntfy topic (a setting)

/// Parses + canonicalizes the ntfy topic the app subscribes to. The grammar
/// mirrors ntfy's own: a topic is `[-_A-Za-z0-9]{1,64}`. We accept either a bare
/// topic or a full `http(s)://host/topic` URL (self-hosted servers are common —
/// DESIGN.md §6 names "ntfy.sh self-hosted"), and we always store a full URL so
/// the subscriber has an unambiguous endpoint.
enum NtfyTopic {
    /// Default server when the user types only a bare topic name.
    static let defaultHost = "https://ntfy.sh"

    /// ntfy's topic grammar (matches the server's `[-_A-Za-z0-9]{1,64}`).
    static let topicPattern = "^[-_A-Za-z0-9]{1,64}$"

    /// True iff `s`, taken as a bare topic name, is a legal ntfy topic.
    static func isValidTopic(_ s: String) -> Bool {
        let t = s.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return false }
        return t.range(of: topicPattern, options: .regularExpression) != nil
    }

    /// Canonical subscribe URL for whatever the user typed, or nil if it can't be
    /// made into one. Accepts:
    ///   - a bare topic        → `https://ntfy.sh/<topic>`
    ///   - `ntfy.sh/<topic>`   → `https://ntfy.sh/<topic>` (scheme defaulted)
    ///   - a full URL          → host + first path segment as the topic
    /// Trailing slashes and a `?…` query are dropped; the topic segment is
    /// validated so junk ("ntfy.sh/" or "https://host/") returns nil.
    static func normalize(_ input: String) -> String? {
        let raw = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !raw.isEmpty else { return nil }

        // Bare topic (no host, no slash): default to ntfy.sh.
        if !raw.contains("/") {
            return isValidTopic(raw) ? "\(defaultHost)/\(raw)" : nil
        }

        // Looks like a URL or host/topic. Default the scheme so URLComponents
        // can split host vs path even for "ntfy.sh/mytopic".
        let withScheme = raw.contains("://") ? raw : "https://\(raw)"
        guard let comps = URLComponents(string: withScheme),
              let host = comps.host, !host.isEmpty,
              let scheme = comps.scheme, scheme == "http" || scheme == "https"
        else { return nil }

        // The topic is the FIRST non-empty path segment.
        let segments = comps.path.split(separator: "/").map(String.init)
        guard let topic = segments.first, isValidTopic(topic) else { return nil }

        var out = "\(scheme)://\(host)"
        if let port = comps.port { out += ":\(port)" }
        out += "/\(topic)"
        return out
    }

    /// The bare topic name from a normalized URL (last path segment), for display.
    static func displayTopic(_ normalizedURL: String) -> String? {
        URLComponents(string: normalizedURL)?.path
            .split(separator: "/").last.map(String.init)
    }
}

// MARK: - payload → in-app route

/// Where a tapped notification should land. `dashboard` is the safe fallback when
/// the payload is missing/garbled — we never crash or guess a job id.
enum NotifyRoute: Equatable {
    case session(jobId: String)
    case dashboard
}

/// The R37 terminal-state payload the CP emits (see `roost/server.py`
/// `_build_notification` and `tests/test_notify.py`). We decode ONLY the fields
/// the app routes/renders on; per API.md's additive-only rule unknown fields are
/// ignored. Every field except the discriminator is optional so a partial or
/// future payload still decodes to a safe route.
struct NotifyPayload: Decodable, Equatable {
    let event: String?
    let jobId: String?
    let state: String?
    let intent: String?
    let durationSec: Double?
    let exitCode: Int?
    let workerId: String?
    let message: String?

    enum CodingKeys: String, CodingKey {
        case event
        case jobId = "job_id"
        case state
        case intent
        case durationSec = "duration_sec"
        case exitCode = "exit_code"
        case workerId = "worker_id"
        case message
    }
}

enum NotifyRouter {
    /// Decode the JSON body of an R37 notification. Returns nil on non-JSON or a
    /// shape that isn't a dictionary — the caller routes to `.dashboard`.
    static func decode(_ data: Data) -> NotifyPayload? {
        try? JSONDecoder().decode(NotifyPayload.self, from: data)
    }

    static func decode(json: String) -> NotifyPayload? {
        decode(Data(json.utf8))
    }

    /// Route a decoded payload: a non-empty `job_id` opens that Session view;
    /// anything else (nil payload, blank/missing job id) falls back to the
    /// Dashboard. Deep-linking to a specific job is the whole point of the push.
    static func route(_ payload: NotifyPayload?) -> NotifyRoute {
        guard let id = payload?.jobId?.trimmingCharacters(in: .whitespacesAndNewlines),
              !id.isEmpty
        else { return .dashboard }
        return .session(jobId: id)
    }

    /// Convenience: raw JSON body → route, in one step (the system notification
    /// handler holds the raw `userInfo`/data and just wants the destination).
    static func route(json: String) -> NotifyRoute { route(decode(json: json)) }

    static func route(data: Data) -> NotifyRoute { route(decode(data)) }
}
