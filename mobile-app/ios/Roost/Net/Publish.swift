import Foundation

/// Pure helpers for the publish flow (API.md §6). The server is authoritative —
/// it slugifies `?name=` and 400s an unfixable name — but we derive a sensible
/// default slug from the picked file and pre-validate so the phone gives instant
/// feedback instead of a round-trip. Mirrors `roost/publish.py::normalize_slug`
/// (`strip().lower().replace(" ", "-")` → `^[a-z0-9][a-z0-9-]{0,39}$`). Foundation
/// only, so the Linux harness covers it.
enum PublishSlug {
    /// The server's slug grammar: lowercase alnum start, then alnum/hyphen, ≤40.
    static let pattern = "^[a-z0-9][a-z0-9-]{0,39}$"

    /// Apply the server's normalization (lowercase, spaces→`-`, trim) WITHOUT
    /// validating. Used to live-preview what the server will store as the user
    /// types, so the field shows the same slug the URL will carry.
    static func normalize(_ name: String) -> String {
        name.trimmingCharacters(in: .whitespacesAndNewlines)
            .lowercased()
            .replacingOccurrences(of: " ", with: "-")
    }

    /// True iff `name` normalizes to a slug the server will accept (so we can
    /// enable the Publish button only when it'll succeed).
    static func isValid(_ name: String) -> Bool {
        let slug = normalize(name)
        guard !slug.isEmpty else { return false }
        return slug.range(of: pattern, options: .regularExpression) != nil
    }

    /// Best-effort default slug from a picked filename. Strips the tar suffix
    /// (`.tar.gz`/`.tgz`/`.tar`), normalizes, then coerces stray characters to
    /// `-` and trims to the 40-char window so a typical name ("My Site.tar.gz")
    /// yields a usable proposal ("my-site"). Returns "" if nothing survives —
    /// the UI then asks the user to type a name.
    static func suggestion(fromFilename filename: String) -> String {
        var stem = filename
        for suffix in [".tar.gz", ".tgz", ".tar"] {
            if stem.lowercased().hasSuffix(suffix) {
                stem = String(stem.dropLast(suffix.count))
                break
            }
        }
        // Normalize as the server would, then replace any remaining illegal
        // characters with hyphens and collapse/trim them.
        let lowered = normalize(stem)
        let mapped = lowered.map { ch -> Character in
            (ch.isLetter && ch.isASCII) || (ch.isNumber && ch.isASCII) || ch == "-"
                ? ch : "-"
        }
        var slug = String(mapped)
        // Collapse runs of hyphens and trim leading/trailing ones.
        while slug.contains("--") { slug = slug.replacingOccurrences(of: "--", with: "-") }
        slug = slug.trimmingCharacters(in: CharacterSet(charactersIn: "-"))
        if slug.count > 40 { slug = String(slug.prefix(40)) }
        slug = slug.trimmingCharacters(in: CharacterSet(charactersIn: "-"))
        return isValid(slug) ? slug : ""
    }
}

/// Sniffs whether `data` looks like a gzip stream (magic bytes `1f 8b`). The
/// one-shot endpoint 400s a body that isn't a valid `tar.gz`; this lets the app
/// reject an obviously-wrong pick (e.g. a plain folder or a `.zip`) before the
/// upload. It is a cheap necessary check, not a full tar.gz validation — the
/// server still has the final say.
enum BundleCheck {
    static func looksLikeGzip(_ data: Data) -> Bool {
        data.count >= 2 && data[data.startIndex] == 0x1f
            && data[data.startIndex + 1] == 0x8b
    }
}
