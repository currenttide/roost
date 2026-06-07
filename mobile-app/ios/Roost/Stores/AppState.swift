import Foundation
import SwiftUI

/// Root app store: owns the paired credential and the live `ApiClient`. Bridges
/// the Keychain (persistence) and the UI (paired vs unpaired). A 401 anywhere
/// in the app routes here via `handleUnauthorized()` to drop back to pairing;
/// 403 is handled locally by each store (show error, stay paired).
@MainActor
final class AppState: ObservableObject {
    @Published private(set) var credential: Credential?
    @Published private(set) var api: ApiClient?

    /// True once we've loaded (or failed to load) the stored credential, so the
    /// root view doesn't flash the pairing screen on cold start.
    @Published private(set) var ready = false

    /// A `roost://pair?d=…` URL captured by `.onOpenURL` before the pairing
    /// screen is on-screen; the pairing view consumes it on appear.
    @Published var pendingPairURL: URL?

    /// Automation hook (simulator demos / UI tests / screenshot capture): set
    /// `ROOST_OPEN_PUBLISH=1` and the dashboard presents the publish sheet on
    /// appear, so a screenshot of the publish screen is deterministic without
    /// tapping through the menu. Same spirit as `ROOST_PAIR_URI`.
    let autoOpenPublish: Bool

    init() {
        autoOpenPublish =
            ProcessInfo.processInfo.environment["ROOST_OPEN_PUBLISH"] == "1"
        restore()
        // Automation hook (simulator demos / UI tests): pair from an injected
        // URI without the system open-URL confirm dialog, e.g.
        //   SIMCTL_CHILD_ROOST_PAIR_URI='roost://pair?d=…' simctl launch …
        // Same code path as a scanned QR; ignored when already paired.
        if !isPaired,
           let s = ProcessInfo.processInfo.environment["ROOST_PAIR_URI"],
           let url = URL(string: s) {
            pendingPairURL = url
        }
    }

    var isPaired: Bool { credential != nil }

    func restore() {
        if let cred = Keychain.shared.load(), let client = Self.makeClient(cred) {
            credential = cred
            api = client
        }
        ready = true
    }

    /// Accept a freshly decoded pairing payload (already healthz-probed by the
    /// pairing store). Persists to Keychain and goes live.
    func pair(_ payload: PairPayload) {
        let cred = Credential(url: payload.url, token: payload.token, name: payload.name)
        guard let client = Self.makeClient(cred) else { return }
        Keychain.shared.save(cred)
        credential = cred
        api = client
    }

    /// 401 → token revoked/invalid: clear and return to pairing (API.md §1).
    /// The offline cache goes too — fleet goals/logs shouldn't outlive the pairing.
    func handleUnauthorized() {
        Keychain.shared.clear()
        OfflineCache.shared.clear()
        credential = nil
        api = nil
    }

    /// Manual sign-out (settings affordance).
    func unpair() { handleUnauthorized() }

    private static func makeClient(_ cred: Credential) -> ApiClient? {
        guard let url = URL(string: cred.url) else { return nil }
        // A bespoke session: SSE needs an effectively-infinite per-request
        // timeout (the stream stays open), but a sane resource timeout would
        // kill it — so we disable the resource timeout and rely on our own
        // reconnect/backoff for liveness.
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 30
        config.timeoutIntervalForResource = TimeInterval(Int.max)
        config.waitsForConnectivity = true
        let session = URLSession(configuration: config)
        return ApiClient(baseURL: url, token: cred.token, session: session)
    }
}
