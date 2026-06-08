import Foundation

/// Distilled live-stream transform (R108) — the iOS mirror of the
/// LANGUAGE-NEUTRAL contract in `mobile-app/fixtures/distilled/SPEC.md`.
///
/// An agent job (`kind: claude`/`auto`) runs `claude -p --output-format
/// stream-json --verbose`; the worker relays each raw stdout line as a `log`
/// SSE event whose `data` field is ONE line of that stream-json. Shown raw it is
/// a firehose (base64 `signature` blobs, 100 KB `tool_result` bodies, init
/// banners, rate-limit pings, JSON wrappers around one line of assistant text).
///
/// `DistilledLine.from(data:)` turns one raw `data` line into at most one
/// readable line, or `nil` to suppress it. It is the iOS reference of the
/// cross-platform contract: the CLI (`roost.cli.distill_log_line`) is the
/// canonical reference impl, and the golden fixtures under
/// `mobile-app/fixtures/distilled/cases.json` pin all three clients to the same
/// output. The function is PURE (no I/O, never throws on odd input) so the Linux
/// pure-layer harness can run it against the shared fixtures.
///
/// This is intentionally separate from the SwiftUI view: the transform is pure
/// logic; the SessionView only chooses distilled vs raw and renders the string.
enum DistilledLine {

    /// Tool-input keys, in priority order, used to summarise a `tool_use` call.
    /// MUST match `SPEC.md` `TOOL_HINT_KEYS` and the CLI `_TOOL_HINT_KEYS`.
    static let toolHintKeys = [
        "command", "file_path", "path", "pattern", "query", "url", "description",
        "prompt", "intent",
    ]
    /// Caps for a one-line summary / truncated tool result (SPEC.md constants).
    static let hintMax = 80
    static let resultMax = 200

    /// Distil ONE raw `log` SSE `data` line into a readable line, or `nil` to
    /// suppress it. Pure + total: never throws on odd input.
    ///
    /// Non-stream-json input (a `command` job's plain stdout, or a roost-internal
    /// JSON `event` envelope) passes through verbatim so nothing is lost.
    /// Recognised Anthropic stream-json envelopes are distilled per SPEC.md;
    /// base64 signatures, reasoning blobs, and oversized tool-result bodies are
    /// suppressed or truncated.
    static func from(_ data: String) -> String? {
        let text = data.trimmingCharacters(in: .whitespacesAndNewlines)
        // Rule 1: not JSON → passthrough verbatim (preserve `command` stdout).
        if text.isEmpty || !text.hasPrefix("{") { return data }
        guard let bytes = text.data(using: .utf8),
              let parsed = try? JSONSerialization.jsonObject(with: bytes),
              let obj = parsed as? [String: Any]
        else {
            return data  // looked like JSON but isn't, or isn't an object → verbatim
        }

        let mtype = obj["type"] as? String
        // Anthropic stream-json envelopes carry a `message` (assistant/user) or
        // are one of the known top-level control types. Anything else (e.g.
        // roost's own `{"type": "started", ...}` envelope) is not stream-json →
        // passthrough verbatim (Rule 2).
        switch mtype {
        case "system":
            // Rule 3: init → phase divider; any other subtype → suppress.
            return (obj["subtype"] as? String) == "init" ? "🔎 starting…" : nil
        case "rate_limit_event":
            return nil
        case "result":
            return isTruthy(obj["is_error"]) ? "✗ failed" : "✓ done"
        case "assistant", "user":
            return distillMessage(obj)
        default:
            // Unknown JSON shape (roost event envelope, etc.) — passthrough
            // verbatim so the raw view and roost-internal markers are never
            // silently dropped (Rule 2).
            return data
        }
    }

    // MARK: - Message content (Rule 4)

    private static func distillMessage(_ obj: [String: Any]) -> String? {
        let message = obj["message"] as? [String: Any]
        let content = message?["content"]
        // String content → first line, collapsed, capped (suppress if empty).
        if let s = content as? String {
            let flat = firstLine(s, resultMax)
            return flat.isEmpty ? nil : flat
        }
        // List content → map each block, join non-empty with "\n", suppress if
        // nothing survives.
        if let blocks = content as? [Any] {
            var out: [String] = []
            for case let item as [String: Any] in blocks {
                switch item["type"] as? String {
                case "text":
                    // Render only a STRING `text` (real stream-json always is);
                    // a null/non-string `text` → empty → block suppressed, so the
                    // three clients agree instead of leaking `<null>` / `123`.
                    let flat = (item["text"] as? String).map { firstLine($0, resultMax) } ?? ""
                    if !flat.isEmpty { out.append(flat) }
                case "tool_use":
                    out.append(distillToolUse(item))
                case "tool_result":
                    out.append(distillToolResult(item))
                default:
                    // thinking / redacted_thinking (signature blob) and any other
                    // block type → ignored.
                    break
                }
            }
            return out.isEmpty ? nil : out.joined(separator: "\n")
        }
        return nil
    }

    private static func distillToolUse(_ item: [String: Any]) -> String {
        let name = (item["name"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? "tool"
        var hint = ""
        if let input = item["input"] as? [String: Any] {
            for key in toolHintKeys {
                // Render only a non-empty STRING hint, so the three clients stay
                // byte-identical: a number/bool/list/object hint coerces
                // differently per language, so such values are skipped and the
                // scan continues to the next key (SPEC.md rule 4).
                if let v = input[key] as? String, !v.isEmpty {
                    hint = firstLine(v, hintMax)
                    break
                }
            }
        }
        return hint.isEmpty ? "→ \(name)" : "→ \(name): \(hint)"
    }

    private static func distillToolResult(_ item: [String: Any]) -> String {
        var resultText = ""
        if let s = item["content"] as? String {
            resultText = s
        } else if let blocks = item["content"] as? [Any] {
            for blk in blocks {
                if let d = blk as? [String: Any],
                   (d["type"] as? String) == "text",
                   let t = d["text"] as? String, !t.isEmpty {
                    resultText = t
                    break
                }
                if let s = blk as? String, !s.isEmpty {
                    resultText = s
                    break
                }
            }
        }
        let summary = resultText.isEmpty ? "(result)" : firstLine(resultText, resultMax)
        return isTruthy(item["is_error"]) ? "  ⎿ ✗ \(summary)" : "  ⎿ \(summary)"
    }

    // MARK: - Helpers (Rule 5: collapse + truncate)

    /// Whitespace-collapse `text` (split on any whitespace, rejoin with single
    /// spaces) and cap at `limit`, appending a single `…` when it overflows.
    static func firstLine(_ text: String, _ limit: Int) -> String {
        let flat = text.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
        if flat.count > limit {
            return String(flat.prefix(limit)) + "…"
        }
        return flat
    }

    /// JSON truthiness matching Python's `if v:` / `obj.get(...)` semantics (the
    /// CLI's `is_error` checks): a value is truthy unless
    /// it is missing, `null`, `false`, `0`, an empty string, or an empty
    /// container. Portable — no CoreFoundation (`kCFBoolean*` is unavailable on
    /// Linux). `Bool` is probed before the numeric branch so a genuine JSON
    /// `false`/`true` maps correctly on both platforms.
    private static func isTruthy(_ value: Any?) -> Bool {
        switch value {
        case .none: return false
        case is NSNull: return false
        case let b as Bool: return b
        case let s as String: return !s.isEmpty
        case let a as [Any]: return !a.isEmpty
        case let d as [String: Any]: return !d.isEmpty
        case let n as NSNumber: return n.doubleValue != 0
        default: return true
        }
    }
}
