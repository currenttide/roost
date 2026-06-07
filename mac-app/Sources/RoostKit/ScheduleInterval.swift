import Foundation

/// Pure helpers for the Schedules verb (`POST/GET/PATCH/DELETE /schedules`). The
/// server is authoritative — it parses `every` and 400s an unparseable value or
/// one below the 30 s floor — but we parse/validate the SAME grammar so the UI can
/// disable a doomed action, and we format `interval_sec` back to the compact
/// `30s`/`5m`/`6h`/`1d` display the CLI uses. Foundation-only, so the Linux harness
/// covers it (mirrors the iOS `ScheduleInterval`).
///
/// The grammar is pinned to `roost/server.py` + `roost/cli.py`:
///   - `parse_every`  — `^\s*(\d+(?:\.\d+)?)\s*([smhd])\s*$` (lowercased first),
///     or a bare number → seconds; multipliers s=1 m=60 h=3600 d=86400.
///   - `SCHEDULE_MIN_INTERVAL_SEC = 30.0` — `every` must be `>= 30`.
///   - `_fmt_interval` (cli.py) — d/h/m when `sec >= div && sec % div == 0`, else `<sec>s`.
public enum ScheduleInterval {
    /// The server's 30 s floor (`SCHEDULE_MIN_INTERVAL_SEC`). Below this → 400.
    public static let minSeconds: Double = 30

    /// Multipliers for the single trailing unit, matching the server's table.
    private static let units: [Character: Double] = ["s": 1, "m": 60, "h": 3600, "d": 86400]

    /// The unit-suffix grammar from `server.py::_EVERY_RE`: optional surrounding
    /// whitespace, a non-negative decimal, optional inner whitespace, one of smhd.
    /// (Matched case-insensitively — the server lowercases the string first.)
    public static let everyPattern = #"^\s*(\d+(?:\.\d+)?)\s*([smhd])\s*$"#

    /// Parse `every` exactly as the server's `parse_every` does (the STRING path:
    /// a unit-suffixed value or a bare numeric string). Returns the interval in
    /// seconds, or nil when it can't be parsed (the caller maps nil → "invalid").
    ///
    /// Note: this is the string grammar only — the app always sends `every` as a
    /// string, so we never exercise the server's number/bool branches here. A bare
    /// numeric string ("90") still parses, matching `parse_every`'s `float(every)`
    /// fallback.
    public static func parse(_ every: String) -> Double? {
        let lowered = every.lowercased()
        if let re = try? NSRegularExpression(pattern: everyPattern, options: []),
           let match = re.firstMatch(
               in: lowered, range: NSRange(lowered.startIndex..., in: lowered)),
           let numR = Range(match.range(at: 1), in: lowered),
           let unitR = Range(match.range(at: 2), in: lowered),
           let value = Double(lowered[numR]),
           let unit = lowered[unitR].first,
           let mult = units[unit] {
            return value * mult
        }
        // No unit suffix: fall back to a bare number, like `float(every)`.
        // Trim whitespace so " 90 " parses the same as the regex path would.
        let trimmed = lowered.trimmingCharacters(in: .whitespaces)
        return Double(trimmed)
    }

    /// True iff `every` parses AND is at or above the 30 s floor — i.e. the exact
    /// condition under which `POST /schedules` returns 200 for the interval.
    public static func isValid(_ every: String) -> Bool {
        guard let sec = parse(every) else { return false }
        return sec >= minSeconds
    }

    /// A friendly reason the current `every` is rejected, or nil when it's valid.
    /// Distinguishes "can't parse" from "below the floor" so the field can say
    /// which (the server returns distinct 400s for the two). Empty → nil (no
    /// message yet, just a disabled button).
    public static func validationMessage(_ every: String) -> String? {
        let trimmed = every.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty { return nil }
        guard let sec = parse(every) else {
            return "Use seconds or <N>[smhd] — e.g. 30s, 15m, 6h, 1d."
        }
        if sec < minSeconds {
            return "Minimum interval is 30s."
        }
        return nil
    }

    /// Compact human interval from a second count — byte-for-byte the CLI's
    /// `_fmt_interval` (`30s` / `5m` / `6h` / `1d`): prefer the largest whole unit.
    /// Used to render `Schedule.intervalSec` and to label interval presets.
    public static func format(_ sec: Double) -> String {
        for (unit, div) in [("d", 86400.0), ("h", 3600.0), ("m", 60.0)] {
            if sec >= div && sec.truncatingRemainder(dividingBy: div) == 0 {
                return "\(Int(sec / div))\(unit)"
            }
        }
        return "\(Int(sec))s"
    }

    /// Relative "next run in …" / "ran … ago" label from an absolute epoch and a
    /// reference now. Returns nil for a nil timestamp. A due/overdue next-run reads
    /// as "now" rather than a negative interval (the tick fires imminently).
    public static func relative(to epoch: Double?, now: Double) -> String? {
        guard let epoch else { return nil }
        let delta = epoch - now
        if delta <= 0 {
            let ago = -delta
            return ago < 1 ? "now" : "\(format(ago.rounded())) ago"
        }
        return "in \(format(delta.rounded()))"
    }
}
