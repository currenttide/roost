import Foundation

/// Drives the pairing screen: decode a scanned URL or pasted string, probe
/// `/healthz` before accepting (API.md §1), surface a friendly error otherwise.
@MainActor
final class PairingStore: ObservableObject {
    @Published var pasteText: String = ""
    @Published var error: String?
    @Published var busy = false

    /// Attempt to pair from a `roost://` URL (open-URL event).
    func pair(url: URL, into app: AppState) async {
        await attempt({ try Pairing.decode(url: url) }, into: app)
    }

    /// Attempt to pair from the manual paste field.
    func pairFromPaste(into app: AppState) async {
        let raw = pasteText
        await attempt({ try Pairing.decode(base64url: raw) }, into: app)
    }

    private func attempt(_ decode: () throws -> PairPayload, into app: AppState) async {
        error = nil
        busy = true
        defer { busy = false }
        let payload: PairPayload
        do {
            payload = try decode()
        } catch let e as Pairing.PairError {
            error = Self.message(for: e)
            return
        } catch {
            // `self.` is load-bearing: bare `error` is the immutable catch binding.
            self.error = "Couldn't read that pairing code."
            return
        }
        guard let url = URL(string: payload.url) else {
            error = "Pairing code has an invalid URL."
            return
        }
        // Probe reachability before committing the credential.
        do {
            let health = try await ApiClient.healthz(baseURL: url)
            guard health.ok else {
                error = "Control plane reported not-ok."
                return
            }
        } catch {
            // Surface the underlying error — "can't reach" has too many causes
            // (ATS, local-network permission, DNS, refused) to hide it.
            let detail = (error as? ApiError).map(String.init(describing:))
                ?? error.localizedDescription
            self.error = "Can't reach \(payload.url) — \(detail). Same network as the host?"
            return
        }
        app.pair(payload)
        Haptics.success()
    }

    private static func message(for e: Pairing.PairError) -> String {
        switch e {
        case .unsupportedVersion: return "This code needs a newer app — update Roost."
        case .malformedURL, .notBase64, .notJSON:
            return "That doesn't look like a Roost pairing code."
        }
    }
}
