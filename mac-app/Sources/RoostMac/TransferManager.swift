#if os(macOS)
import Foundation
import Observation
import RoostKit

/// One file moving to or from a worker (DESIGN.md §14). The worker-side leg
/// is a normal command job, so transfers inherit placement, retries, and the
/// dashboard for free.
@MainActor
@Observable
final class Transfer: Identifiable {
    enum Direction { case send, fetch }

    enum Phase: Equatable {
        case uploading(Double)        // local → CP (progress 0…1)
        case delivering               // job queued/running on the worker
        case waitingForWorker         // fetch: job queued/running
        case downloading              // CP → local
        case done(String)             // human detail ("sha256 ✓", path)
        case failed(String)

        var isTerminal: Bool {
            switch self {
            case .done, .failed: return true
            default: return false
            }
        }
    }

    let id = UUID()
    let direction: Direction
    let fileName: String
    let workerName: String
    let startedAt = Date()
    var phase: Phase
    var jobID: String?
    var blob: Blob?
    var localURL: URL?       // fetch: where the file landed
    var destination: String  // send: remote path · fetch: remote source path

    init(direction: Direction, fileName: String, workerName: String,
         destination: String, phase: Phase) {
        self.direction = direction
        self.fileName = fileName
        self.workerName = workerName
        self.destination = destination
        self.phase = phase
    }

    var phaseLabel: String {
        switch phase {
        case .uploading(let p): "uploading \(Int(p * 100))%"
        case .delivering: "delivering…"
        case .waitingForWorker: "waiting for worker…"
        case .downloading: "downloading…"
        case .done(let detail): "✓ \(detail)"
        case .failed(let why): "✗ \(why)"
        }
    }
}

@MainActor
@Observable
final class TransferManager {
    private let store: FleetStore
    private(set) var transfers: [Transfer] = []
    private(set) var staged: [Blob] = []
    private(set) var loadingStaged = false
    private(set) var hasLoadedStaged = false
    /// Classified failure from the last staged-blob load. A 404 (older CP without
    /// `/blobs`) classifies as `.endpointMissing`, NOT a generic error — see
    /// `stagedState`. Surfacing this is the point of R93: the old
    /// `try? refreshStaged()` swallowed every load failure silently.
    private(set) var stagedError: TransfersLoadError?

    /// The single state for the staged-blob list. The decision (404 ⇒ unavailable,
    /// transport ⇒ retryable error, never a silent empty) lives in RoostKit so it's
    /// Linux-tested and the view is a dumb renderer.
    var stagedState: TransfersListState {
        TransfersListState.decide(
            blobCount: staged.count,
            loadError: stagedError,
            loading: loadingStaged,
            hasLoaded: hasLoadedStaged)
    }

    struct PendingSend: Identifiable {
        let id = UUID()
        let worker: Worker
        let files: [URL]
    }

    /// Drag-drop staging: set when files are dropped on a worker row; a sheet
    /// confirms destination before anything moves.
    var pendingSend: PendingSend?

    var activeCount: Int { transfers.filter { !$0.phase.isTerminal }.count }

    init(store: FleetStore) {
        self.store = store
    }

    // MARK: defaults

    func defaultDestination(for worker: Worker, fileName: String) -> String {
        let key = "transfer.destdir.\(worker.id)"
        let dir = UserDefaults.standard.string(forKey: key) ?? "~/roost-inbox"
        return "\(dir)/\(fileName)"
    }

    private func rememberDestination(_ path: String, for worker: Worker) {
        let dir = (path as NSString).deletingLastPathComponent
        guard !dir.isEmpty else { return }
        UserDefaults.standard.set(dir, forKey: "transfer.destdir.\(worker.id)")
    }

    // MARK: send (drag & drop → worker)

    func send(_ fileURL: URL, to worker: Worker, destination: String) {
        rememberDestination(destination, for: worker)
        let transfer = Transfer(
            direction: .send, fileName: fileURL.lastPathComponent,
            workerName: worker.name, destination: destination,
            phase: .uploading(0))
        transfers.insert(transfer, at: 0)

        Task { @MainActor in
            guard let client = store.client else {
                transfer.phase = .failed("not connected")
                return
            }
            do {
                let blob = try await client.uploadBlob(
                    fileURL: fileURL,
                    progress: { fraction in
                        Task { @MainActor in
                            if case .uploading = transfer.phase {
                                transfer.phase = .uploading(fraction)
                            }
                        }
                    })
                transfer.blob = blob
                try await refreshStaged()

                guard let hostname = worker.hostname, !hostname.isEmpty else {
                    transfer.phase = .failed("worker reports no hostname to pin to")
                    return
                }
                let job = try await client.submit(JobSubmission(
                    intent: "deliver \(blob.name) → \(worker.name)",
                    command: Self.deliverCommand(
                        getURL: blob.getURL, destination: destination,
                        sha256: blob.sha256, os: worker.os),
                    requires: ["hostname": hostname],
                    maxAttempts: 1))
                transfer.jobID = job.id
                transfer.phase = .delivering
                store.poke()  // the run appears in ACTIVE immediately
                await watch(job: job.id, for: transfer) { final in
                    .done("delivered · sha256 ✓ · \(destination)")
                }
            } catch {
                transfer.phase = .failed(error.localizedDescription)
            }
        }
    }

    /// The worker-side leg: fetch via presigned URL (no credentials), land it
    /// at the destination, prove integrity with a checksum.
    static func deliverCommand(
        getURL: String, destination: String, sha256: String?, os: String?
    ) -> String {
        let dest = shellPath(destination)
        let shaCmd = os == "darwin" ? "shasum -a 256" : "sha256sum"
        var cmd = """
        D=\(dest); mkdir -p "$(dirname "$D")" && \
        curl -fsS '\(getURL)' -o "$D" && \
        S=$(\(shaCmd) "$D" | cut -d' ' -f1) && echo "sha256: $S"
        """
        if let sha256 {
            cmd += """
             && [ "$S" = "\(sha256)" ] || { echo "checksum mismatch"; exit 1; }
            """
        }
        return cmd
    }

    // MARK: fetch (worker → here)

    func fetch(remotePath: String, from worker: Worker, saveTo localURL: URL) {
        let name = (remotePath as NSString).lastPathComponent
        let transfer = Transfer(
            direction: .fetch, fileName: name, workerName: worker.name,
            destination: remotePath, phase: .waitingForWorker)
        transfer.localURL = localURL
        transfers.insert(transfer, at: 0)

        Task { @MainActor in
            guard let client = store.client else {
                transfer.phase = .failed("not connected")
                return
            }
            do {
                let blob = try await client.presignBlobUpload(name: name)
                transfer.blob = blob
                guard let putURL = blob.putURL else {
                    transfer.phase = .failed("control plane returned no upload URL")
                    return
                }
                guard let hostname = worker.hostname, !hostname.isEmpty else {
                    transfer.phase = .failed("worker reports no hostname to pin to")
                    return
                }
                let src = Self.shellPath(remotePath)
                let job = try await client.submit(JobSubmission(
                    intent: "fetch \(name) ← \(worker.name)",
                    command: """
                    F=\(src); [ -f "$F" ] || { echo "no such file: $F"; exit 1; }; \
                    curl -fsS -T "$F" '\(putURL)' && echo uploaded
                    """,
                    requires: ["hostname": hostname],
                    maxAttempts: 1))
                transfer.jobID = job.id
                store.poke()
                await watch(job: job.id, for: transfer) { _ in nil }
                guard case .delivering = transfer.phase else { return }
                transfer.phase = .downloading
                try await client.downloadBlob(id: blob.id, to: localURL)
                transfer.phase = .done("saved to \(localURL.path)")
                try? await client.deleteBlob(id: blob.id)  // staging, not storage
                try? await refreshStaged()
            } catch {
                transfer.phase = .failed(error.localizedDescription)
            }
        }
    }

    // MARK: staged blobs (the fleet clipboard)

    /// Reload the staged list, propagating failures to internal callers
    /// (send/fetch want to know). State (`stagedError`, `hasLoadedStaged`) is
    /// recorded on every outcome so the pane's `stagedState` can never go silent.
    func refreshStaged() async throws {
        guard let client = store.client else { return }
        loadingStaged = true
        defer { loadingStaged = false }
        do {
            staged = try await client.listBlobs()
            stagedError = nil
        } catch {
            stagedError = TransfersLoadError.from(error)
            hasLoadedStaged = true
            throw error
        }
        hasLoadedStaged = true
    }

    /// Non-throwing reload for the pane's `.task`/Retry: failures are captured into
    /// `stagedState` (loading/list/empty/unavailable/error) instead of being
    /// swallowed by a bare `try?`.
    func loadStaged() async {
        try? await refreshStaged()
    }

    func stageFile(_ fileURL: URL) async {
        guard let client = store.client else { return }
        _ = try? await client.uploadBlob(fileURL: fileURL)
        try? await refreshStaged()
    }

    func deleteStaged(_ blob: Blob) async {
        guard let client = store.client else { return }
        try? await client.deleteBlob(id: blob.id)
        try? await refreshStaged()
    }

    func clearFinished() {
        transfers.removeAll { $0.phase.isTerminal }
    }

    // MARK: job watching

    /// Poll the delivery/fetch job to terminal state. On success for sends,
    /// `onSuccess` maps the job to a done-phase; fetches continue downloading.
    private func watch(
        job jobID: String, for transfer: Transfer,
        onSuccess: (Job) -> Transfer.Phase?
    ) async {
        guard let client = store.client else { return }
        for _ in 0..<900 {  // up to ~30 min at 2 s
            do {
                let job = try await client.job(id: jobID)
                switch job.state {
                case "succeeded":
                    if let phase = onSuccess(job) {
                        transfer.phase = phase
                    } else {
                        transfer.phase = .delivering  // marker: job leg finished
                    }
                    return
                case "failed":
                    transfer.phase = .failed(
                        job.diagnosis ?? job.error ?? "job failed on \(transfer.workerName)")
                    return
                case "cancelled":
                    transfer.phase = .failed("cancelled")
                    return
                default:
                    break
                }
            } catch {
                // transient — keep polling
            }
            try? await Task.sleep(nanoseconds: 2_000_000_000)
        }
        transfer.phase = .failed("timed out waiting for the worker")
    }

    /// Quote a user-entered remote path for sh, expanding a leading `~`.
    static func shellPath(_ path: String) -> String {
        var p = path
        var prefix = ""
        if p == "~" || p.hasPrefix("~/") {
            prefix = "$HOME"
            p = String(p.dropFirst(1))
        }
        // single-quote the rest; escape embedded single quotes
        let quoted = p.replacingOccurrences(of: "'", with: "'\\''")
        return "\"\(prefix)\"'\(quoted)'"
    }
}
#endif
