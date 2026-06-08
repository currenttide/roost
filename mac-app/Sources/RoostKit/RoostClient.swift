import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

// MARK: - Connection

public struct RoostConnection: Equatable, Sendable {
    public var baseURL: URL
    public var token: String?

    public init(baseURL: URL, token: String? = nil) {
        self.baseURL = baseURL
        self.token = (token?.isEmpty == true) ? nil : token
    }

    public init?(urlString: String, token: String? = nil) {
        let trimmed = urlString.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty,
              let url = URL(string: trimmed.hasSuffix("/") ? String(trimmed.dropLast()) : trimmed),
              url.scheme == "http" || url.scheme == "https",
              url.host != nil
        else { return nil }
        self.init(baseURL: url, token: token)
    }

    public var isHTTPS: Bool { baseURL.scheme == "https" }
}

// MARK: - Errors

public enum RoostClientError: Error, LocalizedError, Sendable {
    case unauthorized                       // 401/403 — bad or revoked token
    case notFound(String)                   // 404
    case conflict(String)                   // 409 — e.g. cancel on a terminal job
    case server(status: Int, message: String)
    case transport(String)                  // connection refused / DNS / timeout
    case decoding(String)                   // payload didn't parse as expected
    case notARoostServer                    // reachable, but /healthz isn't roost

    public var errorDescription: String? {
        switch self {
        case .unauthorized: return "Unauthorized — check the token"
        case .notFound(let what): return "Not found: \(what)"
        case .conflict(let msg): return msg
        case .server(let status, let message): return "Server error \(status): \(message)"
        case .transport(let msg): return "Can't reach the control plane: \(msg)"
        case .decoding(let msg): return "Unexpected response: \(msg)"
        case .notARoostServer: return "That URL responds, but not like a Roost control plane"
        }
    }
}

// MARK: - Client

/// Typed async wrapper over the control-plane HTTP API. One method per
/// endpoint, no caching, no state beyond the connection — the app's stores
/// own all state (DESIGN.md §3).
public final class RoostClient: @unchecked Sendable {
    public let connection: RoostConnection
    private let session: URLSession

    public init(connection: RoostConnection, session: URLSession? = nil) {
        self.connection = connection
        if let session {
            self.session = session
        } else {
            let config = URLSessionConfiguration.ephemeral
            config.timeoutIntervalForRequest = 15
            config.httpAdditionalHeaders = ["User-Agent": "roost-mac/0.1"]
            self.session = URLSession(configuration: config)
        }
    }

    // MARK: endpoints

    public func healthz() async throws -> Healthz {
        try await get("/healthz")
    }

    /// One request that powers icon, popover, and notifications (§5).
    public func derived(limit: Int = 40) async throws -> DerivedSnapshot {
        try await get("/derived", query: ["limit": String(limit)])
    }

    public func jobs(
        state: String? = nil, root: String? = nil,
        parent: String? = nil, limit: Int = 100
    ) async throws -> [Job] {
        var query = ["limit": String(limit)]
        query["state"] = state
        query["root"] = root
        query["parent"] = parent
        let rows: [Tolerant<Job>] = try await get("/jobs", query: query)
        return rows.compactMap(\.value)
    }

    public func job(id: String) async throws -> Job {
        try await get("/jobs/\(id)")
    }

    public func derivedRun(id: String) async throws -> Run {
        try await get("/jobs/\(id)/derived")
    }

    public func tree(id: String) async throws -> [Job] {
        let rows: [Tolerant<Job>] = try await get("/jobs/\(id)/tree")
        return rows.compactMap(\.value)
    }

    public func logs(id: String, since: Int = 0, limit: Int = 1000) async throws -> LogsResponse {
        try await get("/jobs/\(id)/logs",
                      query: ["since": String(since), "limit": String(limit)])
    }

    public func submit(_ submission: JobSubmission) async throws -> Job {
        try await send("POST", "/jobs", body: submission)
    }

    @discardableResult
    public func cancel(id: String, tree: Bool = false) async throws -> Int {
        let resp: CancelResponse = try await send(
            "DELETE", "/jobs/\(id)", query: tree ? ["tree": "true"] : [:])
        return resp.cancelled
    }

    public func workers() async throws -> [Worker] {
        let rows: [Tolerant<Worker>] = try await get("/workers")
        return rows.compactMap(\.value)
    }

    public func pruneWorkers(olderThanDays: Double = 7) async throws -> PruneResponse {
        try await send("POST", "/workers/prune",
                       query: ["older_than_days": String(olderThanDays)])
    }

    // MARK: publish (built thing → live URL)

    /// One-shot publish (the menu-bar flow, mirroring mobile §6a): the `tar.gz`
    /// bundle IS the request body and `name` is REQUIRED (slugified server-side).
    /// Nothing is staged, so a dropped connection can't leave a dangling blob.
    ///
    /// Cross-version fallback (mirrors `roost/cli.py` publish, R78/R90): the
    /// one-shot raw-tar POST is new (R7). Older control planes only know the
    /// two-step blob flow and react to a raw body in version-specific ways (the
    /// deployed 0.1.0 CP `json.loads()`es the gzip bytes and 500s; others 422 or
    /// 404). We CANNOT enumerate every old failure, so the contract is: on ANY
    /// non-2xx from the one-shot EXCEPT auth (401/403 — a fallback would only hit
    /// it again), try the two-step `stageBlob` → `publishFromBlob`. If the
    /// fallback ALSO fails we surface BOTH errors, leading with the one-shot's
    /// (R90) so a real new-CP bug stays visible instead of being masked by the
    /// fallback's reroute.
    public func publishBundle(name: String, data: Data) async throws -> Site {
        var request = makeRequest("POST", "/publish", query: ["name": name])
        request.httpBody = data
        // A non-JSON Content-Type selects the server's one-shot path.
        request.setValue("application/gzip", forHTTPHeaderField: "Content-Type")

        let (body, response) = try await self.data(for: request)
        switch response.statusCode {
        case 200..<300:
            return try decodeSite(body)
        case 401, 403:
            // Auth fails identically on the fallback — surface it directly.
            throw RoostClientError.unauthorized
        default:
            break  // any other non-2xx → try the two-step fallback below.
        }

        // The one-shot failed with a non-auth error. Lead any fallback failure
        // with this so a genuine new-CP error stays the headline.
        let oneShot = RoostClientError.server(
            status: response.statusCode, message: detail(from: body) ?? "")

        let blob: Blob
        do {
            blob = try await stageBlob(name: "\(name).tar.gz", data: data)
        } catch RoostClientError.unauthorized {
            throw RoostClientError.unauthorized  // auth — surface directly, not wrapped.
        } catch {
            throw publishFallbackError(oneShot: oneShot, fallback: error)
        }
        do {
            return try await publishFromBlob(name: name, blobID: blob.id)
        } catch RoostClientError.unauthorized {
            throw RoostClientError.unauthorized
        } catch {
            throw publishFallbackError(oneShot: oneShot, fallback: error)
        }
    }

    /// Stage a raw `tar.gz` body on the control plane (`POST /blobs?name=`),
    /// returning the staged blob (its `id` feeds `publishFromBlob`). The legacy
    /// leg of the publish fallback; uses the raw body, not a file upload.
    public func stageBlob(name: String, data: Data) async throws -> Blob {
        var request = makeRequest("POST", "/blobs", query: ["name": name])
        request.httpBody = data
        let (body, response) = try await self.data(for: request)
        switch response.statusCode {
        case 200..<300:
            do {
                return try JSONDecoder().decode(Blob.self, from: body)
            } catch {
                throw RoostClientError.decoding(String(describing: error))
            }
        case 401, 403:
            throw RoostClientError.unauthorized
        default:
            throw RoostClientError.server(
                status: response.statusCode, message: detail(from: body) ?? "")
        }
    }

    /// Publish a previously-staged blob as a site (`POST /publish` JSON
    /// `{name, blob_id}` — the original two-step flow). The legacy leg of the
    /// publish fallback.
    public func publishFromBlob(name: String, blobID: String) async throws -> Site {
        var request = makeRequest("POST", "/publish")
        request.httpBody = try JSONEncoder().encode(
            PublishFromBlobBody(name: name, blob_id: blobID))
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let (body, response) = try await self.data(for: request)
        switch response.statusCode {
        case 200..<300:
            return try decodeSite(body)
        case 401, 403:
            throw RoostClientError.unauthorized
        default:
            throw RoostClientError.server(
                status: response.statusCode, message: detail(from: body) ?? "")
        }
    }

    private func decodeSite(_ data: Data) throws -> Site {
        do {
            return try JSONDecoder().decode(Site.self, from: data)
        } catch {
            throw RoostClientError.decoding(String(describing: error))
        }
    }

    /// Both legs failed: report the fallback's error WITH the one-shot's, leading
    /// with the one-shot (R90's contract). The result is a `.server` error whose
    /// status is the one-shot's, so callers keying off the original status (and
    /// the user) see the genuine new-CP failure first.
    private func publishFallbackError(
        oneShot: RoostClientError, fallback: Error
    ) -> RoostClientError {
        let oneShotMsg = oneShot.errorDescription ?? "\(oneShot)"
        let fallbackMsg = (fallback as? RoostClientError)?.errorDescription
            ?? (fallback as? LocalizedError)?.errorDescription
            ?? "\(fallback)"
        let combined = "publish failed: \(oneShotMsg)\n"
            + "  (also tried the two-step blob flow for older control planes; "
            + "that failed too: \(fallbackMsg))"
        if case .server(let status, _) = oneShot {
            return .server(status: status, message: combined)
        }
        return .server(status: 0, message: combined)
    }

    /// Body for the two-step `publishFromBlob` call. Snake-cased to match the
    /// server's JSON contract (`{name, blob_id}`).
    private struct PublishFromBlobBody: Encodable {
        let name: String
        let blob_id: String
    }

    /// Sites published on this control plane (`GET /publish`).
    public func sites() async throws -> [Site] {
        let rows: [Tolerant<Site>] = try await get("/publish")
        return rows.compactMap(\.value)
    }

    // MARK: schedules (interval jobs)

    /// Interval schedules, newest first (`GET /schedules`). Admin/scheduler-scoped.
    public func schedules() async throws -> [Schedule] {
        let rows: [Tolerant<Schedule>] = try await get("/schedules")
        return rows.compactMap(\.value)
    }

    /// Enable/disable a schedule (`PATCH /schedules/{id}`). Re-enabling restarts the
    /// clock server-side. Returns the updated schedule.
    @discardableResult
    public func setScheduleEnabled(id: String, enabled: Bool) async throws -> Schedule {
        try await send("PATCH", "/schedules/\(id)", body: SchedulePatchBody(enabled: enabled))
    }

    /// Delete a schedule (`DELETE /schedules/{id}`).
    @discardableResult
    public func deleteSchedule(id: String) async throws -> ScheduleDeleteResponse {
        try await send("DELETE", "/schedules/\(id)")
    }

    // MARK: steer a running job (R38)

    /// Queue a follow-up message for a RUNNING job (`POST /jobs/{id}/input`). The
    /// ack is always `queued`; the live delivered/dropped outcome — which depends on
    /// the job kind — arrives via `jobInputs(id:)`. A terminal job is rejected 409.
    @discardableResult
    public func sendInput(id: String, text: String) async throws -> JobInputAck {
        try await send("POST", "/jobs/\(id)/input", body: JobInputSubmit(text: text))
    }

    /// A job's queued follow-ups and their delivery state (`GET /jobs/{id}/inputs`).
    public func jobInputs(id: String) async throws -> JobInputsResponse {
        try await get("/jobs/\(id)/inputs")
    }

    /// Validates a connection for onboarding: reachable → roost → authorized.
    /// Distinguishes the three failure modes so the UI can say which it is.
    public func validate() async throws {
        let health: Healthz
        do {
            health = try await healthz()
        } catch RoostClientError.decoding {
            throw RoostClientError.notARoostServer
        }
        guard health.ok else { throw RoostClientError.notARoostServer }
        _ = try await derived(limit: 1)  // exercises auth
    }

    // MARK: SSE stream

    /// Streams a job's life via SSE. Finishes after the server's `done` event;
    /// throws on transport errors so the caller can reconnect with
    /// `since: <last seen seq>` (the API supports resume).
    public func streamJob(id: String, since: Int = 0) -> AsyncThrowingStream<JobStreamEvent, Error> {
        var request = makeRequest("GET", "/jobs/\(id)/stream",
                                  query: since > 0 ? ["since": String(since)] : [:])
        request.timeoutInterval = 3600
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        return SSEStreamTask.events(for: request, token: connection.token)
    }

    // MARK: request plumbing

    func makeRequest(_ method: String, _ path: String,
                     query: [String: String] = [:]) -> URLRequest {
        var components = URLComponents(
            url: connection.baseURL.appendingPathComponent(path),
            resolvingAgainstBaseURL: false)!
        if !query.isEmpty {
            components.queryItems = query
                .sorted { $0.key < $1.key }
                .map { URLQueryItem(name: $0.key, value: $0.value) }
        }
        var request = URLRequest(url: components.url!)
        request.httpMethod = method
        if let token = connection.token {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        return request
    }

    private func get<T: Decodable>(_ path: String, query: [String: String] = [:]) async throws -> T {
        try await perform(makeRequest("GET", path, query: query))
    }

    private func send<T: Decodable>(
        _ method: String, _ path: String,
        query: [String: String] = [:], body: (some Encodable)? = Optional<Int>.none
    ) async throws -> T {
        var request = makeRequest(method, path, query: query)
        if let body {
            request.httpBody = try JSONEncoder().encode(body)
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        return try await perform(request)
    }

    private func perform<T: Decodable>(_ request: URLRequest) async throws -> T {
        let (data, response) = try await self.data(for: request)
        switch response.statusCode {
        case 200..<300:
            do {
                return try JSONDecoder().decode(T.self, from: data)
            } catch {
                throw RoostClientError.decoding(String(describing: error))
            }
        case 401, 403:
            throw RoostClientError.unauthorized
        case 404:
            throw RoostClientError.notFound(detail(from: data) ?? request.url?.path ?? "")
        case 409:
            throw RoostClientError.conflict(detail(from: data) ?? "conflict")
        default:
            throw RoostClientError.server(
                status: response.statusCode,
                message: detail(from: data) ?? "")
        }
    }

    /// FastAPI error payloads are {"detail": "..."}.
    private func detail(from data: Data) -> String? {
        struct D: Decodable { let detail: String? }
        return (try? JSONDecoder().decode(D.self, from: data))?.detail
    }

    /// Continuation-based dataTask wrapper — works on both Darwin and
    /// swift-corelibs-foundation (Linux), unlike URLSession.data(for:delegate:).
    private func data(for request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        try await withCheckedThrowingContinuation { continuation in
            let task = session.dataTask(with: request) { data, response, error in
                if let error {
                    continuation.resume(
                        throwing: RoostClientError.transport(error.localizedDescription))
                    return
                }
                guard let http = response as? HTTPURLResponse else {
                    continuation.resume(
                        throwing: RoostClientError.transport("no HTTP response"))
                    return
                }
                continuation.resume(returning: (data ?? Data(), http))
            }
            task.resume()
        }
    }
}

// MARK: - SSE transport

/// Delegate-based SSE reader (URLSession.bytes is Darwin-only; the delegate
/// path works on Linux too, which keeps RoostKit testable here).
final class SSEStreamTask: NSObject, URLSessionDataDelegate, @unchecked Sendable {
    private var parser = SSEParser()
    private let continuation: AsyncThrowingStream<JobStreamEvent, Error>.Continuation

    private init(continuation: AsyncThrowingStream<JobStreamEvent, Error>.Continuation) {
        self.continuation = continuation
    }

    static func events(for request: URLRequest, token: String?)
        -> AsyncThrowingStream<JobStreamEvent, Error>
    {
        AsyncThrowingStream { continuation in
            let delegate = SSEStreamTask(continuation: continuation)
            let config = URLSessionConfiguration.ephemeral
            config.timeoutIntervalForRequest = 3600
            let session = URLSession(configuration: config,
                                     delegate: delegate, delegateQueue: nil)
            let task = session.dataTask(with: request)
            continuation.onTermination = { _ in
                task.cancel()
                session.invalidateAndCancel()
            }
            task.resume()
        }
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask,
                    didReceive response: URLResponse,
                    completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        if let http = response as? HTTPURLResponse, http.statusCode != 200 {
            let error: RoostClientError = (http.statusCode == 401 || http.statusCode == 403)
                ? .unauthorized
                : .server(status: http.statusCode, message: "stream rejected")
            continuation.finish(throwing: error)
            completionHandler(.cancel)
            return
        }
        completionHandler(.allow)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        for sse in parser.feed(data) {
            do {
                if let event = try JobStreamEvent.parse(sse) {
                    continuation.yield(event)
                    if case .done = event {
                        continuation.finish()
                        dataTask.cancel()
                        return
                    }
                }
            } catch {
                continuation.finish(throwing: error)
                dataTask.cancel()
                return
            }
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask,
                    didCompleteWithError error: Error?) {
        if let error, (error as NSError).code != NSURLErrorCancelled {
            continuation.finish(
                throwing: RoostClientError.transport(error.localizedDescription))
        } else {
            continuation.finish()
        }
        session.finishTasksAndInvalidate()
    }
}
