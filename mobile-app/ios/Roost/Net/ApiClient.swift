import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking  // URLSession on Linux (the repo's Linux test harness)
#endif

/// Typed errors surfaced to the UI. `unauthorized` (401) drops to pairing;
/// `forbidden` (403) is a scope bug — show but stay paired (API.md §1).
enum ApiError: Error, Equatable {
    case unauthorized               // 401 — drop to pairing screen
    case forbidden(String)          // 403 — show, stay paired
    case notFound(String)           // 404
    case http(Int, String)          // other non-2xx with server detail
    case transport(String)          // URLSession / decode failure
}

/// Small async/await HTTP client against one control plane. Holds the base URL
/// and bearer token; nothing global. The stores own an instance and recreate it
/// on re-pair. Bytes streaming for SSE lives in `LogStream`, which borrows the
/// same `session` + auth.
struct ApiClient {
    let baseURL: URL
    let token: String
    let session: URLSession

    init(baseURL: URL, token: String, session: URLSession = .shared) {
        self.baseURL = baseURL
        self.token = token
        self.session = session
    }

    // MARK: Request building

    func request(_ path: String, query: [URLQueryItem] = [],
                 method: String = "GET", body: Data? = nil) -> URLRequest {
        var comps = URLComponents(url: baseURL.appendingPathComponent(path),
                                  resolvingAgainstBaseURL: false)!
        if !query.isEmpty { comps.queryItems = query }
        var req = URLRequest(url: comps.url!)
        req.httpMethod = method
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        if let body {
            req.httpBody = body
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        return req
    }

    /// Map an HTTP response to a typed error, decoding the `{"detail": …}`
    /// envelope when present (API.md §1).
    static func mapError(status: Int, data: Data) -> ApiError {
        let detail = (try? JSONDecoder().decode(ErrorEnvelope.self, from: data))?.detail
            ?? String(data: data, encoding: .utf8) ?? "HTTP \(status)"
        switch status {
        case 401: return .unauthorized
        case 403: return .forbidden(detail)
        case 404: return .notFound(detail)
        default: return .http(status, detail)
        }
    }

    private func sendData(_ req: URLRequest) async throws -> Data {
        let data: Data
        let resp: URLResponse
        do {
            (data, resp) = try await session.data(for: req)
        } catch {
            throw ApiError.transport(error.localizedDescription)
        }
        guard let http = resp as? HTTPURLResponse else {
            throw ApiError.transport("non-HTTP response")
        }
        guard (200...299).contains(http.statusCode) else {
            throw Self.mapError(status: http.statusCode, data: data)
        }
        return data
    }

    private func send<T: Decodable>(_ req: URLRequest, as type: T.Type) async throws -> T {
        let data = try await sendData(req)
        do {
            return try JSONDecoder().decode(T.self, from: data)
        } catch {
            throw ApiError.transport("decode: \(error)")
        }
    }

    // MARK: Endpoints (only those in API.md)

    /// Unauthenticated reachability probe.
    static func healthz(baseURL: URL, session: URLSession = .shared) async throws -> Healthz {
        var req = URLRequest(url: baseURL.appendingPathComponent("healthz"))
        req.timeoutInterval = 8
        let data: Data, resp: URLResponse
        do { (data, resp) = try await session.data(for: req) }
        catch { throw ApiError.transport(error.localizedDescription) }
        guard let http = resp as? HTTPURLResponse, (200...299).contains(http.statusCode)
        else { throw ApiError.transport("healthz unreachable") }
        do { return try JSONDecoder().decode(Healthz.self, from: data) }
        catch { throw ApiError.transport("healthz decode") }
    }

    func derived(limit: Int = 40) async throws -> Derived {
        try await send(request("derived", query: [.init(name: "limit", value: String(limit))]),
                       as: Derived.self)
    }

    /// Raw body so the caller can also feed the offline cache (one fetch, one decode).
    func derivedRaw(limit: Int = 40) async throws -> Data {
        try await sendData(request("derived", query: [.init(name: "limit", value: String(limit))]))
    }

    func job(_ id: String) async throws -> Job {
        try await send(request("jobs/\(id)"), as: Job.self)
    }

    func jobDerived(_ id: String) async throws -> Run {
        try await send(request("jobs/\(id)/derived"), as: Run.self)
    }

    func logs(_ id: String, since: Int, limit: Int = 1000) async throws -> LogPage {
        try await send(request("jobs/\(id)/logs", query: [
            .init(name: "since", value: String(since)),
            .init(name: "limit", value: String(limit)),
        ]), as: LogPage.self)
    }

    func tree(_ id: String) async throws -> [Job] {
        try await send(request("jobs/\(id)/tree"), as: [Job].self)
    }

    func submit(_ submit: JobSubmit) async throws -> Job {
        let body = try JSONEncoder().encode(submit)
        return try await send(request("jobs", method: "POST", body: body), as: Job.self)
    }

    @discardableResult
    func cancel(_ id: String, tree: Bool = false) async throws -> CancelResponse {
        let query = tree ? [URLQueryItem(name: "tree", value: "true")] : []
        return try await send(request("jobs/\(id)", query: query, method: "DELETE"),
                              as: CancelResponse.self)
    }

    // MARK: Publish (API.md §6)

    /// Stage a site bundle (raw tar.gz body) — publish step 1.
    func uploadBlob(name: String, data: Data) async throws -> BlobUploadResponse {
        var req = request("blobs", query: [.init(name: "name", value: name)],
                          method: "POST")
        req.httpBody = data
        req.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
        return try await send(req, as: BlobUploadResponse.self)
    }

    /// Publish a staged bundle — step 2. `name` optional (defaults server-side
    /// to the blob name minus its tar suffix, then slugified).
    func publish(blobId: String, name: String? = nil) async throws -> Site {
        var payload = ["blob_id": blobId]
        if let name { payload["name"] = name }
        let body = try JSONEncoder().encode(payload)
        return try await send(request("publish", method: "POST", body: body),
                              as: Site.self)
    }

    func sites() async throws -> [Site] {
        try await send(request("publish"), as: [Site].self)
    }
}
