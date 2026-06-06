#if os(macOS)
import AppKit
import Foundation
import Observation

/// Sparkle-free update check (DESIGN.md M3): one GET against a GitHub
/// releases feed + a download link — keeping the zero-dependency rule.
///
/// Disabled unless the bundle carries a `RoostUpdateFeed` Info.plist key
/// (e.g. "https://api.github.com/repos/OWNER/REPO/releases/latest"), so dev
/// builds and forks never phone home.
@MainActor
@Observable
final class UpdateChecker {
    struct Release: Equatable {
        let version: String
        let url: URL
    }

    private(set) var available: Release?
    private(set) var lastChecked: Date?
    private(set) var checking = false

    var isConfigured: Bool { feedURL != nil }

    private var feedURL: URL? {
        (Bundle.main.object(forInfoDictionaryKey: "RoostUpdateFeed") as? String)
            .flatMap(URL.init(string:))
    }

    private var currentVersion: String {
        (Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString")
            as? String) ?? "0"
    }

    /// Silent daily check on launch; manual checks pass force: true.
    func check(force: Bool = false) async {
        guard let feedURL, !checking else { return }
        if !force, let lastChecked,
           Date().timeIntervalSince(lastChecked) < 86_400 { return }
        checking = true
        defer { checking = false }
        lastChecked = Date()

        struct GitHubRelease: Decodable {
            let tag_name: String?
            let html_url: String?
            let draft: Bool?
            let prerelease: Bool?
        }

        var request = URLRequest(url: feedURL)
        request.timeoutInterval = 10
        request.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              (response as? HTTPURLResponse)?.statusCode == 200,
              let release = try? JSONDecoder().decode(GitHubRelease.self, from: data),
              release.draft != true, release.prerelease != true,
              let tag = release.tag_name,
              let urlString = release.html_url, let url = URL(string: urlString)
        else { return }

        let latest = tag.hasPrefix("v") ? String(tag.dropFirst()) : tag
        if isNewer(latest, than: currentVersion) {
            available = Release(version: latest, url: url)
        } else if force {
            available = nil
        }
    }

    func openDownload() {
        if let available { NSWorkspace.shared.open(available.url) }
    }

    /// Numeric dotted-version compare; non-numeric parts compare as 0.
    private func isNewer(_ a: String, than b: String) -> Bool {
        let pa = a.split(separator: ".").map { Int($0) ?? 0 }
        let pb = b.split(separator: ".").map { Int($0) ?? 0 }
        for i in 0..<max(pa.count, pb.count) {
            let x = i < pa.count ? pa[i] : 0
            let y = i < pb.count ? pb[i] : 0
            if x != y { return x > y }
        }
        return false
    }
}
#endif
