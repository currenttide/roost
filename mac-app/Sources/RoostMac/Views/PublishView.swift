#if os(macOS)
import RoostKit
import SwiftUI
import UniformTypeIdentifiers

// Publish verb (mirrors the mobile §6a flow): pick a tar.gz bundle, name it (the
// server slugifies `?name=`), ship it one-shot, then show + copy the live URL.
// Deletion stays admin-only and CLI-only — this pane publishes and lists.

/// Holds the publish-a-site flow for the pane. The pure slug/bundle rules live in
/// RoostKit (`PublishSlug`/`BundleCheck`, Linux-tested); this is the AppKit
/// orchestration around the open panel + client.
@MainActor
@Observable
final class PublishModel {
    private let store: FleetStore

    var name = ""
    private(set) var fileName: String?
    private(set) var data: Data?
    private(set) var publishing = false
    private(set) var site: Site?           // last successful publish (URL shown)
    var error: String?

    private(set) var sites: [Site] = []
    private(set) var loadingSites = false
    private(set) var hasLoadedSites = false
    /// Classified failure from the last sites() load. A 404 (older CP without
    /// `/publish`) classifies as `.endpointMissing`, NOT a generic error — see
    /// `sitesState`. Surfacing this is the whole point of R93: the old
    /// `(try? client.sites()) ?? sites` swallowed every failure silently.
    private(set) var sitesError: PublishLoadError?

    init(store: FleetStore) { self.store = store }

    /// The single state for the published-sites list. The decision (404 ⇒
    /// unavailable, transport ⇒ retryable error, never a silent empty) lives in
    /// RoostKit so it's Linux-tested and the view is a dumb renderer.
    var sitesState: PublishListState {
        PublishListState.decide(
            siteCount: sites.count,
            loadError: sitesError,
            loading: loadingSites,
            hasLoaded: hasLoadedSites)
    }

    /// The slug the server will store, previewed live from `name`.
    var slugPreview: String { PublishSlug.normalize(name) }

    /// Publish is allowed once a gzip bundle is loaded and the name yields a valid
    /// slug (and we're not mid-flight) — the exact server 200-condition.
    var canPublish: Bool {
        data != nil && PublishSlug.isValid(name) && !publishing
    }

    /// Read + sniff a picked file. The one-shot endpoint 400s a non-tar.gz body, so
    /// reject an obviously-wrong pick before uploading; seed a slug from the name.
    func loadBundle(from url: URL) {
        site = nil
        error = nil
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
        if name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            name = PublishSlug.suggestion(fromFilename: picked)
        }
    }

    /// Ship the bundle one-shot. On success stores the `Site` and refreshes the list.
    func publish() async {
        guard let client = store.client, let data else { return }
        guard PublishSlug.isValid(name) else {
            error = "Name must be lowercase letters, numbers, or hyphens (≤40)."
            return
        }
        error = nil
        publishing = true
        defer { publishing = false }
        do {
            site = try await client.publishBundle(name: name, data: data)
            await refreshSites()
        } catch RoostClientError.unauthorized {
            error = "A client or admin token is required to publish."
        } catch RoostClientError.server(let status, let message) where status == 413 {
            error = "Bundle is too large to publish." + (message.isEmpty ? "" : " (\(message))")
        } catch let RoostClientError.server(_, message) {
            error = message.isEmpty ? "Publish failed." : "Publish failed: \(message)"
        } catch {
            self.error = error.localizedDescription
        }
    }

    func refreshSites() async {
        guard let client = store.client else { return }
        loadingSites = true
        defer { loadingSites = false }
        do {
            sites = try await client.sites()
            sitesError = nil
        } catch {
            sitesError = PublishLoadError.from(error)
        }
        hasLoadedSites = true
    }

    func reset() {
        fileName = nil
        data = nil
        name = ""
        site = nil
        error = nil
    }
}

struct PublishPane: View {
    @Environment(AppModel.self) private var model
    @State private var pub: PublishModel?

    var body: some View {
        Group {
            if let pub {
                content(pub)
            } else {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .onAppear {
            if pub == nil {
                let p = PublishModel(store: model.store)
                pub = p
                Task { await p.refreshSites() }
            }
        }
    }

    @ViewBuilder
    private func content(_ pub: PublishModel) -> some View {
        @Bindable var pub = pub
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                Text("Publish a site")
                    .font(.title3.weight(.semibold))
                Text("Pick a built site as a **.tar.gz** bundle; it goes live at a URL on this control plane. Deleting a site stays a CLI task (`roost publish --rm`).")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)

                composer(pub)

                if let site = pub.site {
                    publishedCard(site)
                }
                if let error = pub.error {
                    Label(error, systemImage: "exclamationmark.triangle")
                        .font(.caption)
                        .foregroundStyle(.red)
                }

                Divider().padding(.vertical, 2)
                sitesSection(pub)
            }
            .padding(16)
            .frame(maxWidth: 560, alignment: .leading)
        }
    }

    // MARK: composer

    @ViewBuilder
    private func composer(_ pub: PublishModel) -> some View {
        @Bindable var pub = pub
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Button(pub.fileName == nil ? "Choose bundle…" : "Choose different bundle…") {
                    chooseBundle(pub)
                }
                if let fileName = pub.fileName {
                    Text(fileName).font(.callout).foregroundStyle(.secondary).lineLimit(1)
                }
            }

            VStack(alignment: .leading, spacing: 4) {
                Text("Name").font(.caption).foregroundStyle(.secondary)
                TextField("my-site", text: $pub.name)
                    .textFieldStyle(.roundedBorder)
                    .frame(maxWidth: 320)
                let slug = pub.slugPreview
                if !slug.isEmpty {
                    Text(PublishSlug.isValid(pub.name)
                         ? "URL slug: \(slug)"
                         : "Slug must be lowercase letters, numbers, or hyphens (≤40).")
                        .font(.caption2)
                        .foregroundStyle(PublishSlug.isValid(pub.name) ? Color.secondary : Color.red)
                }
            }

            HStack {
                Button("Publish") {
                    Task { await pub.publish() }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(!pub.canPublish)
                if pub.publishing { ProgressView().controlSize(.small) }
            }
        }
    }

    private func publishedCard(_ site: Site) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Label("Published “\(site.slug)”", systemImage: "checkmark.circle.fill")
                .font(.headline)
                .foregroundStyle(.green)
            HStack(spacing: 8) {
                Text(site.shareURL)
                    .font(.system(size: 12, design: .monospaced))
                    .textSelection(.enabled)
                    .lineLimit(1)
                Button {
                    copy(site.shareURL)
                } label: { Image(systemName: "doc.on.doc") }
                    .buttonStyle(.borderless)
                    .help("Copy URL")
                Button {
                    if let url = URL(string: site.shareURL) { NSWorkspace.shared.open(url) }
                } label: { Image(systemName: "arrow.up.right.square") }
                    .buttonStyle(.borderless)
                    .help("Open in browser")
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.green.opacity(0.08), in: RoundedRectangle(cornerRadius: 8))
    }

    // MARK: existing sites

    @ViewBuilder
    private func sitesSection(_ pub: PublishModel) -> some View {
        HStack {
            SectionLabel("Published on this control plane")
            if pub.loadingSites { ProgressView().controlSize(.mini) }
            Button("Refresh") { Task { await pub.refreshSites() } }
                .buttonStyle(.link)
                .font(.caption)
        }
        // One state, one screen — the RoostKit decision guarantees a load failure
        // (404 or transport) is surfaced rather than swallowed into "No sites yet."
        switch pub.sitesState {
        case .loading:
            HStack { ProgressView().controlSize(.small); Text("Loading sites…").font(.caption).foregroundStyle(.secondary) }
        case .unavailable:
            Text("Publishing isn't available on this control plane (older server). Update the control plane to list and ship sites from here.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        case .error(let message):
            VStack(alignment: .leading, spacing: 6) {
                Label("Couldn't load published sites", systemImage: "exclamationmark.triangle")
                    .font(.caption)
                    .foregroundStyle(.red)
                Text(message).font(.caption2).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                Button("Retry") { Task { await pub.refreshSites() } }
                    .buttonStyle(.link)
                    .font(.caption)
            }
        case .empty:
            Text("No sites yet.").font(.caption).foregroundStyle(.secondary)
        case .list:
            ForEach(pub.sites) { site in
                HStack(spacing: 8) {
                    Image(systemName: "globe").foregroundStyle(.secondary)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(site.slug).font(.callout)
                        Text("\(ByteCountFormatter.string(fromByteCount: Int64(site.size), countStyle: .file)) · \(site.files) file\(site.files == 1 ? "" : "s") · updated \(Format.timeAgo(site.updatedAt))")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button { copy(site.shareURL) } label: {
                        Image(systemName: "doc.on.doc")
                    }
                    .buttonStyle(.borderless)
                    .help("Copy URL")
                    Button {
                        if let url = URL(string: site.shareURL) { NSWorkspace.shared.open(url) }
                    } label: { Image(systemName: "arrow.up.right.square") }
                        .buttonStyle(.borderless)
                        .help("Open in browser")
                }
                .padding(.vertical, 2)
            }
        }
    }

    // MARK: helpers

    private func chooseBundle(_ pub: PublishModel) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        panel.allowedContentTypes = [UTType("org.gnu.gnu-zip-tar-archive"),
                                     UTType.gzip, UTType("public.tar-archive")]
            .compactMap { $0 }
        guard panel.runModal() == .OK, let url = panel.url else { return }
        pub.loadBundle(from: url)
    }

    private func copy(_ string: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(string, forType: .string)
    }
}
#endif
