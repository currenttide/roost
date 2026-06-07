import SwiftUI
import UniformTypeIdentifiers

/// Publish-a-site sheet (API.md §6, production north star #3): pick a `tar.gz`,
/// name it, ship it one-shot, then show the live site URL with a share button.
/// Mirrors `NewSessionView`'s shape — a `Form` in its own `NavigationStack`,
/// Cancel/primary toolbar buttons, error footnote.
struct PublishView: View {
    @EnvironmentObject var app: AppState
    @StateObject private var store = PublishStore()
    @Environment(\.dismiss) private var dismiss

    @State private var showImporter = false

    /// Content types the importer offers: a gzip'd tar, falling back to any data
    /// so a bundle the OS doesn't tag as gzip is still selectable (the store
    /// sniffs the magic bytes and rejects a non-gzip pick).
    private var allowedTypes: [UTType] {
        var types: [UTType] = [.gzip]
        if let tgz = UTType(filenameExtension: "tgz") { types.append(tgz) }
        types.append(.data)
        return types
    }

    var body: some View {
        NavigationStack {
            Form {
                if let site = store.site {
                    publishedSection(site)
                } else {
                    bundleSection
                    if store.data != nil { nameSection }
                }

                if let error = store.error {
                    Text(error).font(.footnote).foregroundStyle(.red)
                }
            }
            .navigationTitle("Publish a site")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button(store.site == nil ? "Cancel" : "Done") { dismiss() }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    if store.site == nil {
                        Button("Publish") { Task { await store.publish() } }
                            .disabled(!store.canPublish)
                    }
                }
            }
            .fileImporter(isPresented: $showImporter,
                          allowedContentTypes: allowedTypes,
                          allowsMultipleSelection: false) { result in
                switch result {
                case .success(let urls):
                    if let url = urls.first { store.loadBundle(from: url) }
                case .failure(let err):
                    store.error = "Couldn't open that file: \(err.localizedDescription)"
                }
            }
        }
        .onAppear { store.bind(app) }
    }

    // MARK: - Pick the bundle

    @ViewBuilder
    private var bundleSection: some View {
        Section {
            Button {
                showImporter = true
            } label: {
                Label(store.fileName ?? "Choose a .tar.gz bundle…",
                      systemImage: "doc.zipper")
            }
            if let f = store.fileName, let d = store.data {
                LabeledContent("File", value: f)
                LabeledContent("Size", value: UIFormat.bytes(d.count))
            }
        } header: {
            Text("Bundle")
        } footer: {
            Text("A gzipped tar of a static site. Published in one transactional "
                 + "upload — nothing is staged.")
        }
    }

    // MARK: - Name → slug

    @ViewBuilder
    private var nameSection: some View {
        Section {
            TextField("my-site", text: $store.name)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
        } header: {
            Text("Name")
        } footer: {
            // Live preview of the slug the server will store + the URL path.
            if !store.name.isEmpty {
                if PublishSlug.isValid(store.name) {
                    Text("Will publish at /pub/\(store.slugPreview)/")
                } else {
                    Text("Lowercase letters, numbers, or hyphens (≤40).")
                        .foregroundStyle(.red)
                }
            } else {
                Text("Re-publishing an existing name replaces that site.")
            }
        }
    }

    // MARK: - Result

    @ViewBuilder
    private func publishedSection(_ site: Site) -> some View {
        Section("Published") {
            LabeledContent("Site", value: site.slug)
            // The live URL, selectable, with the internet-facing one preferred.
            VStack(alignment: .leading, spacing: 6) {
                Text(site.shareUrl)
                    .font(.callout.monospaced())
                    .foregroundStyle(.tint)
                    .textSelection(.enabled)
                if let url = URL(string: site.shareUrl) {
                    HStack(spacing: 12) {
                        ShareLink(item: url) {
                            Label("Share link", systemImage: "square.and.arrow.up")
                        }
                        Link(destination: url) {
                            Label("Open", systemImage: "safari")
                        }
                    }
                    .font(.callout)
                }
            }
            .padding(.vertical, 2)
            LabeledContent("Files", value: "\(site.files)")
            LabeledContent("Size", value: UIFormat.bytes(site.size))
        }
    }
}
