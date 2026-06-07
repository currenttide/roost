import Foundation

/// Drives the pairing screen: decode a scanned URL or pasted string, probe
/// `/healthz` before accepting (API.md §1), surface a friendly error otherwise.
///
/// R83: the flow is now a `PairingState` machine (idle/contacting/failed/
/// cancelled/paired). While contacting, the view shows a "Contacting <host>…"
/// caption + a Cancel button; the probe uses a short fail-fast timeout
/// (`ApiClient.pairingProbeSession`) so a dead host surfaces in ~5 s instead of
/// the old silent ~30 s. Cancel actually cancels the in-flight `Task`.
@MainActor
final class PairingStore: ObservableObject {
    @Published var pasteText: String = ""
    @Published private(set) var state: PairingState = .idle

    /// The in-flight probe task, so Cancel can truly cancel it (not just hide UI).
    private var probeTask: Task<Void, Never>?

    /// A short, fail-fast session for the pairing probe. Lazily made once; the
    /// normal app/SSE session is unaffected (see `ApiClient.makePairingProbeSession`).
    private lazy var probeSession: URLSession = ApiClient.makePairingProbeSession()

    // Back-compat / view convenience.
    var busy: Bool { state.isBusy }
    var error: String? { state.errorMessage }
    var caption: String? { state.caption }

    /// Attempt to pair from a `roost://` URL (open-URL event).
    func pair(url: URL, into app: AppState) async {
        await attempt({ try Pairing.decode(url: url) }, into: app)
    }

    /// Attempt to pair from the manual paste field.
    func pairFromPaste(into app: AppState) async {
        let raw = pasteText
        await attempt({ try Pairing.decode(base64url: raw) }, into: app)
    }

    /// User tapped Cancel mid-probe: cancel the in-flight task and drop the
    /// spinner back to a quiet cancelled state (no error — a deliberate cancel
    /// is not a failure).
    func cancel() {
        probeTask?.cancel()
        probeTask = nil
        state = state.cancelling()
    }

    private func attempt(_ decode: () throws -> PairPayload, into app: AppState) async {
        // Decode is synchronous; do it before entering the contacting state so a
        // bad code shows its error immediately (no spinner flash).
        let payload: PairPayload
        do {
            payload = try decode()
        } catch let e as Pairing.PairError {
            state = .failed(message: Self.message(for: e))
            return
        } catch {
            state = .failed(message: "Couldn't read that pairing code.")
            return
        }
        guard let url = URL(string: payload.url) else {
            state = .failed(message: "Pairing code has an invalid URL.")
            return
        }

        // Enter contacting(host) — the view now shows the caption + Cancel.
        state = .beginning(url: payload.url)
        let session = probeSession

        // Run the probe in a cancellable child task we hold a handle to.
        let task: Task<Void, Never> = Task { [weak self] in
            guard let self else { return }
            await self.runProbe(url: url, payload: payload, session: session, into: app)
        }
        probeTask = task
        await task.value
        probeTask = nil
    }

    /// The cancellable probe body. On a CancellationError (or an explicit Cancel
    /// having already moved us out of `contacting`), it leaves the state alone so
    /// `cancel()` owns the cancelled transition.
    private func runProbe(url: URL, payload: PairPayload,
                          session: URLSession, into app: AppState) async {
        do {
            let health = try await ApiClient.healthz(
                baseURL: url, session: session,
                timeout: ApiClient.pairingProbeTimeout)
            // If Cancel fired while we were awaiting, honor it: don't overwrite
            // the cancelled state with a result.
            guard state.isBusy else { return }
            guard health.ok else {
                state = .failed(message: "Control plane reported not-ok.")
                return
            }
        } catch {
            guard state.isBusy else { return }   // cancelled mid-flight
            // Surface the underlying error — "can't reach" has too many causes
            // (ATS, local-network permission, DNS, refused, timeout) to hide it.
            let detail = (error as? ApiError).map(String.init(describing:))
                ?? error.localizedDescription
            state = .failed(
                message: "Can't reach \(payload.url) — \(detail). Same network as the host?")
            return
        }
        state = .paired
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
