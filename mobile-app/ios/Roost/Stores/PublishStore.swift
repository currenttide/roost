import Foundation

/// Publish-a-site sheet store (API.md §6). Holds the picked `tar.gz`, derives a
/// default slug from its filename, and ships it with the ONE-SHOT call
/// (`publishBundle(name:data:)`) — preferred on the phone because nothing is
/// staged, so a dropped connection can't leave a dangling blob (§6a). The
/// resulting `Site` carries the live URL the UI shows + shares.
///
/// Pure slug/bundle logic lives in `PublishSlug`/`BundleCheck` (Foundation-only,
/// Linux-tested); this store is the iOS orchestration around the picker + client.
@MainActor
final class PublishStore: ObservableObject {
    /// The chosen bundle: display name (for the row + slug default) and bytes.
    @Published var fileName: String?
    @Published private(set) var data: Data?
    /// Site name → slug. Seeded from the filename; user-editable. The server
    /// slugifies this; we preview/validate with the same rules.
    @Published var name: String = ""
    @Published private(set) var site: Site?
    @Published var error: String?
    @Published var publishing = false

    private weak var app: AppState?

    func bind(_ app: AppState) { self.app = app }

    /// Slug the server will store, previewed live from `name` (API.md §6a).
    var slugPreview: String { PublishSlug.normalize(name) }

    /// Publish is allowed once a gzip bundle is loaded and the name yields a
    /// valid slug (and we're not mid-flight).
    var canPublish: Bool {
        data != nil && PublishSlug.isValid(name) && !publishing
    }

    /// Accept a file the document picker / share sheet handed us. Reads the
    /// bytes, sniffs the gzip magic (the one-shot endpoint 400s a non-tar.gz
    /// body), and proposes a slug from the filename. Surfaces a friendly error
    /// rather than throwing — the view binds to `self.error`.
    func loadBundle(from url: URL) {
        site = nil
        error = nil
        // Document-picker URLs are security-scoped; bracket the read.
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        let bytes: Data
        do {
            bytes = try Data(contentsOf: url)
        } catch {
            self.error = "Couldn't read that file."
            return
        }
        guard BundleCheck.looksLikeGzip(bytes) else {
            self.error = "Pick a .tar.gz bundle (this file isn't gzip)."
            return
        }
        let picked = url.lastPathComponent
        fileName = picked
        data = bytes
        // Only seed the name if the user hasn't already typed one.
        if name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            name = PublishSlug.suggestion(fromFilename: picked)
        }
    }

    /// Ship the bundle one-shot. On success stores the `Site` (the view shows its
    /// URL + a share affordance). Mirrors the error handling the other stores use:
    /// 401 → drop to pairing; 403 → show, stay paired (API.md §1).
    func publish() async {
        guard let api = app?.api, let data else { return }
        guard PublishSlug.isValid(name) else {
            error = "Name must be lowercase letters, numbers, or hyphens (≤40)."
            return
        }
        error = nil
        publishing = true
        defer { publishing = false }
        do {
            site = try await api.publishBundle(name: name, data: data)
        } catch ApiError.unauthorized {
            app?.handleUnauthorized()
        } catch let ApiError.forbidden(detail) {
            error = "Not allowed: \(detail)"
        } catch let ApiError.http(status, detail) {
            // §6a: 400 bad slug / empty / not-tar.gz, 413 over the size cap.
            error = status == 413 ? "Bundle is too large to publish."
                                  : "Publish failed: \(detail)"
        } catch {
            // `self.` is load-bearing: bare `error` is the catch binding.
            self.error = "Publish failed."
        }
    }
}
