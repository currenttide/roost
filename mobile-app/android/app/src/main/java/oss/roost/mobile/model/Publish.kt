package oss.roost.mobile.model

/**
 * Pure helpers for the publish flow (API.md §6). Android-free (no android.*),
 * so the JVM/kotlinc harness exercises them exactly like the iOS `PublishSlug`/
 * `BundleCheck` (Foundation-only) layer they mirror.
 *
 * The server is authoritative — it slugifies `?name=` and 400s an unfixable
 * name — but we derive a sensible default slug from the picked file and
 * pre-validate so the phone gives instant feedback instead of a round-trip.
 * Mirrors `roost/publish.py::normalize_slug`
 * (`strip().lower().replace(" ", "-")` → `^[a-z0-9][a-z0-9-]{0,39}$`).
 */
object PublishSlug {
    /** The server's slug grammar: lowercase alnum start, then alnum/hyphen, ≤40. */
    val pattern = Regex("^[a-z0-9][a-z0-9-]{0,39}$")

    /**
     * Apply the server's normalization (lowercase, spaces→`-`, trim) WITHOUT
     * validating. Used to live-preview what the server will store as the user
     * types, so the field shows the same slug the URL will carry.
     */
    fun normalize(name: String): String =
        name.trim().lowercase().replace(" ", "-")

    /**
     * True iff `name` normalizes to a slug the server will accept (so we can
     * enable the Publish button only when it'll succeed).
     */
    fun isValid(name: String): Boolean {
        val slug = normalize(name)
        return slug.isNotEmpty() && pattern.matches(slug)
    }

    /**
     * Best-effort default slug from a picked filename. Strips the tar suffix
     * (`.tar.gz`/`.tgz`/`.tar`), normalizes, then coerces stray characters to
     * `-` and trims to the 40-char window so a typical name ("My Site.tar.gz")
     * yields a usable proposal ("my-site"). Returns "" if nothing survives —
     * the UI then asks the user to type a name.
     */
    fun suggestion(filename: String): String {
        var stem = filename
        for (suffix in listOf(".tar.gz", ".tgz", ".tar")) {
            if (stem.lowercase().endsWith(suffix)) {
                stem = stem.dropLast(suffix.length)
                break
            }
        }
        // Normalize as the server would, then replace any remaining illegal
        // characters with hyphens and collapse/trim them.
        val lowered = normalize(stem)
        val mapped = buildString {
            for (ch in lowered) {
                append(if (ch in 'a'..'z' || ch in '0'..'9' || ch == '-') ch else '-')
            }
        }
        var slug = mapped
        // Collapse runs of hyphens and trim leading/trailing ones.
        while (slug.contains("--")) slug = slug.replace("--", "-")
        slug = slug.trim('-')
        if (slug.length > 40) slug = slug.take(40)
        slug = slug.trim('-')
        return if (isValid(slug)) slug else ""
    }
}

/**
 * Sniffs whether `bytes` looks like a gzip stream (magic bytes `1f 8b`). The
 * one-shot endpoint 400s a body that isn't a valid `tar.gz`; this lets the app
 * reject an obviously-wrong pick (e.g. a plain folder or a `.zip`) before the
 * upload. A cheap necessary check, not a full tar.gz validation — the server
 * still has the final say. Mirrors iOS `BundleCheck.looksLikeGzip`.
 */
object BundleCheck {
    fun looksLikeGzip(bytes: ByteArray): Boolean =
        bytes.size >= 2 && bytes[0] == 0x1f.toByte() && bytes[1] == 0x8b.toByte()
}

/**
 * Client-side size guard for the one-shot upload. The CP rejects a bundle over
 * `SITE_MAX_BYTES` with a 413 (`roost/publish.py::SITE_MAX_BYTES = 256 MiB`,
 * measured uncompressed); we mirror the same ceiling against the *compressed*
 * bytes we are about to PUT so an obviously-too-big pick fails instantly on the
 * phone instead of after a long doomed upload. Conservative by design: a bundle
 * whose compressed size already exceeds the uncompressed cap cannot possibly fit
 * once expanded, so rejecting it locally never produces a false negative the
 * server would have accepted. The server remains authoritative for the
 * uncompressed total.
 */
object PublishSizeGuard {
    /** Same ceiling as `roost/publish.py::SITE_MAX_BYTES` (256 MiB). */
    const val MAX_BYTES: Long = 256L * 1024 * 1024

    /** True iff a `byteCount`-byte body is within the cap (and non-empty). */
    fun isWithinCap(byteCount: Long): Boolean = byteCount in 1..MAX_BYTES
}

/**
 * Maps a publish failure to the user-facing message + side effect, as a pure
 * function so the state machine is testable on the JVM (mirrors the error
 * handling in iOS `PublishStore.publish()`):
 *   - 401 → drop to pairing (caller calls `container.unpair()`); §1
 *   - 403 → show "Not allowed: …", stay paired (scope bug, not auth)
 *   - 413 → "Bundle is too large to publish." (§6a size cap)
 *   - 400 / other → "Publish failed: <detail>"
 */
object PublishError {
    data class Mapped(
        /** The message to surface inline under the form. */
        val message: String,
        /** True only for 401: the caller must unpair and bounce to pairing. */
        val unpair: Boolean,
    )

    fun map(status: Int, detail: String): Mapped = when (status) {
        401 -> Mapped("Pairing expired — pair again.", unpair = true)
        403 -> Mapped("Not allowed: $detail", unpair = false)
        413 -> Mapped("Bundle is too large to publish.", unpair = false)
        else -> Mapped("Publish failed: $detail", unpair = false)
    }
}
