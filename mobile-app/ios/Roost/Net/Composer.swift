import Foundation

/// Pure helpers for the session follow-up composer (DESIGN §3.2, API.md §4). The
/// server is authoritative — `POST /jobs/{id}/input` 400s empty text and 413s a
/// body over 64 KiB — but we validate the SAME rules on the phone so the Send
/// button only enables when the server will accept it, and the user gets an
/// instant reason otherwise. Foundation-only, so the Linux harness covers it
/// (mirrors `ScheduleInterval` and the Android `Composer`).
///
/// Pinned to `roost/server.py::send_job_input`:
///   - empty `text` (after the server's truthiness check) → 400.
///   - UTF-8 byte length > `JOB_INPUT_MAX_BYTES` (64 KiB) → 413.
/// The server's emptiness check is `if not text`, which rejects "" but ACCEPTS a
/// whitespace-only string; we trim for the Send-enabled gate (a blank message is
/// never useful) while measuring the UNtrimmed payload for the size cap, since
/// that is exactly the bytes we POST.
enum Composer {
    /// The server's `JOB_INPUT_MAX_BYTES` (64 KiB) — over this → 413.
    static let maxBytes = 64 * 1024

    /// UTF-8 byte length of the message exactly as it would be POSTed.
    static func byteLength(_ text: String) -> Int { text.utf8.count }

    /// True iff `text` would be accepted by `POST /input`: non-blank after trim
    /// (so Send is disabled for an all-whitespace draft) and within the byte cap.
    /// Gates the Send button so an invalid message never round-trips.
    static func canSend(_ text: String) -> Bool {
        if text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return false }
        return byteLength(text) <= maxBytes
    }

    /// A friendly reason the current draft can't be sent, or nil when it's valid
    /// (or merely empty — empty = no message yet, just a disabled button). Maps to
    /// the two server rejections (400 empty vs 413 too-large).
    static func validationMessage(_ text: String) -> String? {
        if text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return nil }
        if byteLength(text) > maxBytes { return "Message too long (max 64 KB)." }
        return nil
    }

    /// One-line outcome for a posted input given its delivery state and detail,
    /// mirroring the CLI's `roost send` reporting (cli.py). `delivered` → a check;
    /// `dropped` → the honest reason (agent/docker jobs run with stdin closed);
    /// `queued` → still waiting on the worker. Mirrors the Android `Composer.outcome`.
    static func outcome(state: String, detail: String?) -> String {
        switch state {
        case "delivered": return "Delivered ✓ (\(detail ?? "to process"))"
        case "dropped": return "Dropped — \(detail ?? "undeliverable")"
        default: return "Queued — waiting for the worker to deliver it"
        }
    }
}
