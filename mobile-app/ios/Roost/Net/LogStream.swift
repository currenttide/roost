import Foundation

/// Per-job seq cursor, persisted so a stream resumes exactly where it left off
/// across backgrounding / relaunch (API.md §5, DESIGN §5 "survive the pocket").
/// UserDefaults is fine — the cursor is a tiny int, not a secret.
enum SeqCursor {
    private static func key(_ jobId: String) -> String { "roost.seq.\(jobId)" }

    static func load(_ jobId: String) -> Int {
        UserDefaults.standard.integer(forKey: key(jobId))  // defaults to 0
    }

    static func store(_ jobId: String, _ seq: Int) {
        // Monotonic: never move the cursor backward.
        if seq > load(jobId) {
            UserDefaults.standard.set(seq, forKey: key(jobId))
        }
    }
}

/// What a LogStream emits. All payloads are Sendable value types, so the stream
/// crosses the actor boundary cleanly and the store consumes it on @MainActor
/// without capturing non-Sendable state inside the producer.
enum StreamUpdate: Sendable {
    case log(LogRow)
    case state(String)
    case done(SSEDonePayload)
    case error(String)          // "unauthorized" is special-cased by the store
}

/// Drives the §5 resume protocol for one job:
///   1. page `GET /logs?since=<last>` until caught up,
///   2. attach `GET /jobs/{id}/stream?since=<last>` via URLSession.bytes,
///   3. dedupe `log` rows with `seq <= lastSeen`,
///   4. reconnect on drop with jittered exponential backoff 1→30 s.
///
/// Exposes one `events()` AsyncStream the store iterates. Cancelling the
/// consuming Task tears the producer down via Task cancellation.
actor LogStream {
    private let api: ApiClient
    private let jobId: String
    private var lastSeq: Int

    /// Backoff bounds (seconds), API.md §5.
    private let backoffMin = 1.0
    private let backoffMax = 30.0

    /// `since` comes from the caller's offline-cache seed (max cached seq), NOT
    /// from the bare persisted cursor: lines and cursor must travel together or
    /// pre-cursor history becomes invisible after a cold start. SeqCursor is
    /// still persisted as a write-behind for diagnostics/compat.
    init(api: ApiClient, jobId: String, since: Int = 0) {
        self.api = api
        self.jobId = jobId
        self.lastSeq = since
    }

    /// An async stream of updates. Finishes after a `done` event or on
    /// cancellation. The store does `for await u in stream.events() { … }`.
    nonisolated func events() -> AsyncStream<StreamUpdate> {
        AsyncStream { continuation in
            let task = Task { await self.pump(continuation) }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    private func pump(_ cont: AsyncStream<StreamUpdate>.Continuation) async {
        var attempt = 0
        while !Task.isCancelled {
            do {
                // (1) Catch up via /logs paging before attaching the stream, so
                // the gap that built up while backgrounded is filled first.
                try await catchUp(cont)
                // (2) Attach the live stream. Returns true on `done`.
                let finished = try await attachStream(cont)
                if finished { break }      // done seen → stop reconnecting
                attempt = 0                // clean EOF without done → reconnect
            } catch is CancellationError {
                break
            } catch {
                // (4) transport error → jittered backoff, retry from the cursor.
                attempt += 1
                let delay = backoff(attempt)
                try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            }
        }
        cont.finish()
    }

    /// Page /logs until a page returns no rows beyond the cursor.
    private func catchUp(_ cont: AsyncStream<StreamUpdate>.Continuation) async throws {
        while !Task.isCancelled {
            let page = try await api.logs(jobId, since: lastSeq)
            let fresh = page.logs.filter { $0.seq > lastSeq }.sorted { $0.seq < $1.seq }
            if fresh.isEmpty { return }
            for row in fresh {
                lastSeq = row.seq
                SeqCursor.store(jobId, row.seq)
                cont.yield(.log(row))
            }
            // If the server capped the page (1000) there may be more; loop.
            if page.logs.count < 1000 { return }
        }
    }

    /// Attach the SSE stream. Returns true if a `done` event was received (the
    /// server closes the stream after done — we should not reconnect).
    private func attachStream(_ cont: AsyncStream<StreamUpdate>.Continuation) async throws -> Bool {
        let req = api.request("jobs/\(jobId)/stream",
                              query: [.init(name: "since", value: String(lastSeq))])
        let (bytes, resp) = try await api.session.bytes(for: req)
        if let http = resp as? HTTPURLResponse, !(200...299).contains(http.statusCode) {
            if http.statusCode == 401 { cont.yield(.error("unauthorized")) }
            throw ApiError.http(http.statusCode, "stream")
        }
        var parser = SSEParser()
        var sawDone = false
        // Iterate raw bytes, not `.lines`: AsyncLineSequence drops blank lines
        // on some SDKs, and the blank line IS the SSE frame delimiter — losing
        // it would stall every frame until EOF. Byte-level line assembly keeps
        // the "\n\n" boundary intact for the parser.
        var lineBuf = Data()
        for try await byte in bytes {
            lineBuf.append(byte)
            if byte == UInt8(ascii: "\n") {
                try Task.checkCancellation()
                let text = String(decoding: lineBuf, as: UTF8.self)
                lineBuf.removeAll(keepingCapacity: true)
                for event in parser.consume(text) where dispatch(event, cont) {
                    sawDone = true
                }
            }
        }
        if !lineBuf.isEmpty {
            _ = parser.consume(String(decoding: lineBuf, as: UTF8.self))
        }
        for event in parser.flush() where dispatch(event, cont) {
            sawDone = true
        }
        return sawDone
    }

    /// Apply one parsed event to the cursor and forward it; returns true if it
    /// was a `done` (so the caller stops reconnecting).
    private func dispatch(_ event: SSEEvent,
                          _ cont: AsyncStream<StreamUpdate>.Continuation) -> Bool {
        switch event {
        case .log(let row):
            // (3) dedupe replays at the catch-up/stream boundary.
            guard row.seq > lastSeq else { return false }
            lastSeq = row.seq
            SeqCursor.store(jobId, row.seq)
            cont.yield(.log(row))
        case .state(let p):
            cont.yield(.state(p.state))
        case .done(let p):
            cont.yield(.done(p))
            return true
        case .error(let p):
            cont.yield(.error(p.error))
        case .unknown:
            break
        }
        return false
    }

    /// Jittered exponential backoff capped at 30 s (API.md §5).
    private func backoff(_ attempt: Int) -> Double {
        let exp = min(backoffMax, backoffMin * pow(2.0, Double(attempt - 1)))
        return Double.random(in: (exp / 2)...exp)   // full-ish jitter
    }
}
