import Foundation
import SwiftUI

// DisplayLine lives in Models/Models.swift (pure layer) so the offline cache
// and the Linux test harness can use it.

/// Session store: owns the header run, the live log, and the SSE LogStream task
/// for one job (API.md §4/§5). Re-pages /logs and re-attaches on foreground.
@MainActor
final class SessionStore: ObservableObject {
    let jobId: String

    @Published private(set) var header: Run?
    @Published private(set) var lines: [DisplayLine] = []
    @Published private(set) var state: String = ""
    @Published private(set) var done: SSEDonePayload?
    @Published private(set) var tree: [Job] = []
    @Published private(set) var streamError: String?

    /// R108: the session view DEFAULTS to the distilled rendering of the agent
    /// stream-json (assistant text, `→ Tool: summary`, truncated results; noise
    /// suppressed). Flipping this to `true` reveals the raw firehose. The toggle
    /// re-renders the rows already in hand — it does not re-fetch — because the
    /// raw `rows` are retained and `lines` is derived from them.
    @Published var showRaw = false {
        didSet { if showRaw != oldValue { rebuildLines() } }
    }

    // Follow-up composer (DESIGN §3.2 / API.md §4, R38).
    @Published var draft: String = ""
    @Published private(set) var sending = false
    @Published private(set) var sendOutcome: String?   // last input's queued/delivered/dropped line

    private weak var app: AppState?
    private var streamTask: Task<Void, Never>?
    private var seen = Set<Int>()     // guards against any double-append
    /// Raw wire rows (source of truth). `lines` is the distilled/raw rendering
    /// derived from these, so the toggle re-renders without re-fetching.
    private var rows: [LogRow] = []
    private var linesSincePersist = 0

    init(jobId: String) { self.jobId = jobId }

    func bind(_ app: AppState) { self.app = app }

    var isTerminal: Bool {
        ["succeeded", "failed", "cancelled"].contains(state) || done != nil
    }

    /// Load the one-line header (`GET /jobs/{id}/derived`).
    func loadHeader() async {
        guard let api = app?.api else { return }
        do {
            header = try await api.jobDerived(jobId)
            if state.isEmpty { state = header?.state ?? "" }
        } catch ApiError.unauthorized {
            app?.handleUnauthorized()
        } catch {
            // Header is best-effort; the log stream still carries state events.
        }
    }

    /// Start (or restart) the resumable log stream. Idempotent: a running task
    /// is left alone so foreground re-entry doesn't spawn duplicates. The
    /// LogStream yields Sendable updates we apply here on the main actor.
    func startStream() {
        guard streamTask == nil, let api = app?.api else { return }
        // Repaint last-known lines from the offline cache; resume from their tail.
        let since = seedFromCacheOnce()
        let stream = LogStream(api: api, jobId: jobId, since: since)
        streamTask = Task { [weak self] in
            for await update in stream.events() {
                guard let self else { return }
                switch update {
                case .log(let row): self.appendLog(row)
                case .state(let s): self.state = s
                case .done(let d): self.handleDone(d)
                case .error(let e): self.handleStreamError(e)
                }
            }
        }
    }

    func stopStream() {
        streamTask?.cancel()
        streamTask = nil
        persistLines()   // flush the tail so a cold start repaints fully
    }

    /// Called on scenePhase → active: refresh header and ensure the stream is
    /// attached (it re-pages /logs from the persisted cursor on its own).
    func resume() {
        Task { await loadHeader() }
        startStream()
    }

    func loadTree() async {
        guard let api = app?.api else { return }
        do { tree = try await api.tree(jobId) }
        catch ApiError.unauthorized { app?.handleUnauthorized() }
        catch { /* tree is optional */ }
    }

    func cancel() async {
        guard let api = app?.api else { return }
        do { try await api.cancel(jobId); await loadHeader() }
        catch ApiError.unauthorized { app?.handleUnauthorized() }
        catch { streamError = "Cancel failed." }
    }

    /// Send the composer draft as a follow-up input (R38, API.md §4): POST it,
    /// clear the field, then poll the inputs queue so the user learns whether it
    /// was delivered or dropped (agent/docker jobs run with stdin closed → dropped
    /// with a reason). Mirrors `roost send --wait` (cli.py) and the Android VM.
    func sendFollowUp() async {
        guard let api = app?.api, !sending, Composer.canSend(draft) else { return }
        let text = draft
        sending = true
        streamError = nil
        sendOutcome = nil
        do {
            let ack = try await api.sendInput(jobId, text: text)
            // Sent: clear the field, show it's queued, then poll for the outcome.
            draft = ""
            sending = false
            sendOutcome = Composer.outcome(state: "queued", detail: nil)
            await pollInputOutcome(ack.inputId)
        } catch ApiError.unauthorized {
            sending = false
            app?.handleUnauthorized()
        } catch let ApiError.http(_, detail) {
            sending = false
            streamError = detail
        } catch let ApiError.forbidden(detail) {
            sending = false
            streamError = detail
        } catch {
            sending = false
            streamError = "Send failed."
        }
    }

    /// Poll `GET /jobs/{id}/inputs` for ~10 s until this input leaves the queue.
    private func pollInputOutcome(_ inputId: String) async {
        guard let api = app?.api else { return }
        for _ in 0..<10 {
            if let row = try? await api.inputs(jobId).inputs.first(where: { $0.id == inputId }),
               row.state != "queued" {
                sendOutcome = Composer.outcome(state: row.state, detail: row.detail)
                return
            }
            try? await Task.sleep(nanoseconds: 1_000_000_000)
        }
    }

    // MARK: - Event handling

    private func appendLog(_ row: LogRow) {
        guard !seen.contains(row.seq) else { return }
        seen.insert(row.seq)
        rows.append(row)
        // Keep rows ordered even if events arrive out of seq, so the derived
        // `lines` (and any later raw/distilled re-render) stay in seq order.
        if rows.count > 1, rows[rows.count - 1].seq < rows[rows.count - 2].seq {
            rows.sort { $0.seq < $1.seq }
        }
        if let line = DisplayLine.from(row, raw: showRaw) {
            lines.append(line)
            if lines.count > 1, lines[lines.count - 1].seq < lines[lines.count - 2].seq {
                lines.sort { $0.seq < $1.seq }
            }
        } else if rows.count > 1, rows[rows.count - 1].seq < rows[rows.count - 2].seq {
            // Out-of-order suppressed line: rebuild so `lines` reflects sorted rows.
            rebuildLines()
        }
        // Throttled write-behind to the offline cache (DESIGN §5); done/stop
        // flush the tail.
        linesSincePersist += 1
        if linesSincePersist >= 25 { persistLines() }
    }

    /// Re-derive `lines` from the retained raw `rows` in the current mode. Cheap
    /// (rows are capped) and only runs on a mode toggle or an out-of-order
    /// suppressed row.
    private func rebuildLines() {
        lines = rows.compactMap { DisplayLine.from($0, raw: showRaw) }
    }

    private func handleDone(_ d: SSEDonePayload) {
        done = d
        if let s = d.state { state = s }
        persistLines()
        Task { await loadHeader() }   // refresh result card details
    }

    // MARK: - Offline cache (DESIGN §5)

    /// Cold-start seed: repaint last-known rows and derive the resume cursor
    /// from them in one step. This deliberately supersedes seeding from the
    /// bare persisted cursor — a cursor without its rows would leave all
    /// pre-cursor history invisible (catch-up only pages seq > cursor). Raw rows
    /// (not pre-rendered lines) are cached so the distilled/raw toggle re-renders
    /// cold-start history too (R108).
    private func seedFromCacheOnce() -> Int {
        guard rows.isEmpty else { return seen.max() ?? 0 }
        let cached = OfflineCache.shared.loadRows(jobId)
        guard !cached.isEmpty else { return 0 }
        rows = cached.sorted { $0.seq < $1.seq }
        cached.forEach { seen.insert($0.seq) }
        rebuildLines()
        return rows.map(\.seq).max() ?? 0
    }

    private func persistLines() {
        linesSincePersist = 0
        guard !rows.isEmpty else { return }
        OfflineCache.shared.saveRows(jobId, rows)
    }

    private func handleStreamError(_ e: String) {
        if e == "unauthorized" { app?.handleUnauthorized(); return }
        streamError = e
    }

    /// Turn an `event` log row's JSON (`{"type": "started"|"succeeded"|…}`)
    /// into a short divider label, or nil if unparseable (then skip it).
    /// Implementation lives in `LogRow` (pure layer) so the Linux harness covers it.
    static func eventLabel(_ json: String) -> String? { LogRow.eventLabel(json) }
}
