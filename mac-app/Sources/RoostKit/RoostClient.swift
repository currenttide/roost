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
