import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

// Blob store client (DESIGN.md §14): stage files on the control plane so the
// worker-side leg of a transfer — a normal command job — can curl them with a
// presigned URL, never a credential.

public struct Blob: Decodable, Identifiable, Equatable, Sendable {
    public let id: String
    public let name: String
    public let size: Int
    public let sha256: String?
    public let state: String          // "pending" | "ready"
    public let createdAt: Double
    public let expiresAt: Double
    public let getURL: String
    public let putURL: String?

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: AnyCodingKey.self)
        guard let bid = try? c.decode(String.self, forKey: "id") else {
            throw DecodingError.keyNotFound(
                AnyCodingKey("id"),
                .init(codingPath: decoder.codingPath, debugDescription: "blob without id"))
        }
        id = bid
        name = (try? c.decode(String.self, forKey: "name")) ?? bid
        size = (try? c.decode(Int.self, forKey: "size")) ?? 0
        sha256 = try? c.decode(String.self, forKey: "sha256")
        state = (try? c.decode(String.self, forKey: "state")) ?? "ready"
        createdAt = (try? c.decode(Double.self, forKey: "created_at")) ?? 0
        expiresAt = (try? c.decode(Double.self, forKey: "expires_at")) ?? 0
        getURL = (try? c.decode(String.self, forKey: "get_url")) ?? ""
        putURL = try? c.decode(String.self, forKey: "put_url")
    }

    public var isReady: Bool { state == "ready" }
}

extension RoostClient {

    /// Stage a local file on the control plane. `progress` (0…1) is reported
    /// on Darwin; on other platforms the callback is skipped.
    public func uploadBlob(
        fileURL: URL,
        name: String? = nil,
        ttlSec: Double? = nil,
        progress: (@Sendable (Double) -> Void)? = nil
    ) async throws -> Blob {
        var query = ["name": name ?? fileURL.lastPathComponent]
        if let ttlSec { query["ttl_sec"] = String(ttlSec) }
        let request = makeRequest("POST", "/blobs", query: query)
        let (data, response) = try await upload(
            request, fromFile: fileURL, progress: progress)
        return try decodeBlobResponse(data: data, response: response)
    }

    /// Mint a pending blob + presigned put_url (the fetch flow: a job on the
    /// worker PUTs the file, the operator downloads it here).
    public func presignBlobUpload(
        name: String, ttlSec: Double? = nil
    ) async throws -> Blob {
        var request = makeRequest("POST", "/blobs/presign")
        var body: [String: Any] = ["name": name]
        if let ttlSec { body["ttl_sec"] = ttlSec }
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let (data, response) = try await rawData(for: request)
        return try decodeBlobResponse(data: data, response: response)
    }

    /// Download a staged blob to a local file URL (atomic move into place).
    public func downloadBlob(id: String, to destination: URL) async throws {
        let request = makeRequest("GET", "/blobs/\(id)")
        let (data, response) = try await rawData(for: request)
        switch response.statusCode {
        case 200..<300:
            try? FileManager.default.removeItem(at: destination)
            try data.write(to: destination, options: .atomic)
        case 401, 403:
            throw RoostClientError.unauthorized
        case 404:
            throw RoostClientError.notFound("blob \(id)")
        case 409:
            throw RoostClientError.conflict("blob upload not finished")
        default:
            throw RoostClientError.server(status: response.statusCode, message: "")
        }
    }

    public func blob(id: String) async throws -> Blob? {
        try await listBlobs().first { $0.id == id }
    }

    public func listBlobs() async throws -> [Blob] {
        let request = makeRequest("GET", "/blobs")
        let (data, response) = try await rawData(for: request)
        guard (200..<300).contains(response.statusCode) else {
            if response.statusCode == 401 || response.statusCode == 403 {
                throw RoostClientError.unauthorized
            }
            throw RoostClientError.server(status: response.statusCode, message: "")
        }
        let rows = (try? JSONDecoder().decode([Tolerant<Blob>].self, from: data)) ?? []
        return rows.compactMap(\.value)
    }

    public func deleteBlob(id: String) async throws {
        let request = makeRequest("DELETE", "/blobs/\(id)")
        let (_, response) = try await rawData(for: request)
        guard (200..<300).contains(response.statusCode) else {
            if response.statusCode == 404 {
                throw RoostClientError.notFound("blob \(id)")
            }
            throw RoostClientError.server(status: response.statusCode, message: "")
        }
    }

    // MARK: plumbing

    private func decodeBlobResponse(
        data: Data, response: HTTPURLResponse
    ) throws -> Blob {
        switch response.statusCode {
        case 200..<300:
            do {
                return try JSONDecoder().decode(Blob.self, from: data)
            } catch {
                throw RoostClientError.decoding(String(describing: error))
            }
        case 401, 403:
            throw RoostClientError.unauthorized
        case 413:
            throw RoostClientError.conflict("file exceeds the staging size cap")
        default:
            throw RoostClientError.server(status: response.statusCode, message: "")
        }
    }

    private func upload(
        _ request: URLRequest, fromFile fileURL: URL,
        progress: (@Sendable (Double) -> Void)?
    ) async throws -> (Data, HTTPURLResponse) {
        try await withCheckedThrowingContinuation { continuation in
            let task = uploadSession.uploadTask(
                with: request, fromFile: fileURL
            ) { data, response, error in
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
            #if canImport(Darwin)
            if let progress {
                let observation = task.progress.observe(\.fractionCompleted) { p, _ in
                    progress(p.fractionCompleted)
                }
                // tie the observation's lifetime to the task
                objc_setAssociatedObject(
                    task, &Self.progressKey, observation, .OBJC_ASSOCIATION_RETAIN)
            }
            #endif
            task.resume()
        }
    }

    private static var progressKey: UInt8 = 0

    /// Long-timeout session for big staged files.
    private var uploadSession: URLSession {
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 3600
        config.timeoutIntervalForResource = 3600
        return URLSession(configuration: config)
    }

    /// Like the private data(for:) but exposed within the module for blob
    /// paths that need raw status handling.
    func rawData(for request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        try await withCheckedThrowingContinuation { continuation in
            let task = URLSession.shared.dataTask(with: request) { data, response, error in
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
