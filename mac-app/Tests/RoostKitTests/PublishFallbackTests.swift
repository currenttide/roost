import XCTest
@testable import RoostKit
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

/// R110: `publishBundle` must degrade across control-plane versions exactly like
/// the CLI (roost/cli.py, R78/R90). The one-shot raw-tar POST is new (R7); an
/// older CP 500s/422s/404s on it. The contract:
///   - 2xx one-shot → Site, no fallback.
///   - 401/403 one-shot → unauthorized, no fallback (it would only re-fail).
///   - any other non-2xx → two-step `stageBlob` (POST /blobs?name=<name>.tar.gz,
///     raw body) → `publishFromBlob` (POST /publish JSON {name, blob_id}).
///   - if the fallback ALSO fails → surface BOTH errors, leading with the
///     one-shot's (so a genuine new-CP bug stays the headline).
///
/// These drive the real `publishBundle` end to end over a stubbed URLProtocol,
/// so the status-dispatch + body shaping are exercised, not just request building.
final class PublishFallbackTests: XCTestCase {

    override func tearDown() {
        StubURLProtocol.reset()
        super.tearDown()
    }

    private func client() -> RoostClient {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [StubURLProtocol.self]
        let session = URLSession(configuration: config)
        return RoostClient(
            connection: RoostConnection(urlString: "http://hub:8787", token: "tok")!,
            session: session)
    }

    private let bundle = Data([0x1f, 0x8b, 0x08, 0x00, 0xde, 0xad, 0xbe, 0xef])

    private let siteJSON = """
    {"slug": "demo", "url": "http://hub:8787/pub/demo/", "files": 3, "size": 4096}
    """
    private let blobJSON = """
    {"id": "blob123abc", "name": "demo.tar.gz", "size": 8, "state": "ready",
     "created_at": 1.0, "expires_at": 2.0, "get_url": "http://hub:8787/blobs/blob123abc?sig=x"}
    """

    // MARK: - one-shot success: no fallback

    func testOneShotSuccessReturnsSiteWithoutFallback() async throws {
        StubURLProtocol.responder = { request in
            // The one-shot uses a non-JSON content type and the ?name= query.
            XCTAssertEqual(request.url?.path, "/publish")
            XCTAssertTrue((request.url?.query ?? "").contains("name=demo"))
            return (200, Data(self.siteJSON.utf8))
        }
        let site = try await client().publishBundle(name: "demo", data: bundle)
        XCTAssertEqual(site.slug, "demo")
        XCTAssertEqual(site.files, 3)
        XCTAssertEqual(StubURLProtocol.requests.count, 1, "no fallback on 2xx")
    }

    // MARK: - one-shot 500 → blob path → Site (the headline case)

    func testOneShot500FallsBackThroughBlobToSite() async throws {
        StubURLProtocol.responder = { request in
            let path = request.url?.path ?? ""
            let ctype = request.value(forHTTPHeaderField: "Content-Type") ?? ""
            if path == "/publish" && ctype != "application/json" {
                // Old CP json.loads()es the gzip and 500s on the one-shot.
                return (500, Data(#"{"detail":"Internal Server Error"}"#.utf8))
            }
            if path == "/blobs" {
                // The blob is staged as <name>.tar.gz with the raw body.
                XCTAssertTrue((request.url?.query ?? "").contains("name=demo.tar.gz"),
                              "blob name must be <name>.tar.gz, got \(request.url?.query ?? "")")
                return (200, Data(self.blobJSON.utf8))
            }
            if path == "/publish" && ctype == "application/json" {
                // The second /publish references the staged blob by id.
                let posted = StubURLProtocol.bodies.last ?? Data()
                let obj = (try? JSONSerialization.jsonObject(with: posted)) as? [String: Any]
                XCTAssertEqual(obj?["name"] as? String, "demo")
                XCTAssertEqual(obj?["blob_id"] as? String, "blob123abc")
                return (200, Data(self.siteJSON.utf8))
            }
            XCTFail("unexpected request: \(path) ctype=\(ctype)")
            return (404, Data())
        }
        let site = try await client().publishBundle(name: "demo", data: bundle)
        XCTAssertEqual(site.slug, "demo")
        // Three calls: one-shot 500, stageBlob, publishFromBlob.
        XCTAssertEqual(StubURLProtocol.requests.count, 3)
        let paths = StubURLProtocol.requests.map { $0.url?.path ?? "" }
        XCTAssertEqual(paths, ["/publish", "/blobs", "/publish"])
    }

    // 422 (validation) and 404 (no such shape) also trigger the fallback.
    func testOneShot422AlsoFallsBack() async throws {
        StubURLProtocol.responder = { request in
            let path = request.url?.path ?? ""
            let ctype = request.value(forHTTPHeaderField: "Content-Type") ?? ""
            if path == "/publish" && ctype != "application/json" { return (422, Data()) }
            if path == "/blobs" { return (200, Data(self.blobJSON.utf8)) }
            return (200, Data(self.siteJSON.utf8))
        }
        let site = try await client().publishBundle(name: "demo", data: bundle)
        XCTAssertEqual(site.slug, "demo")
        XCTAssertEqual(StubURLProtocol.requests.count, 3)
    }

    // MARK: - one-shot 500 → blob ALSO fails → both errors, leading with one-shot

    func testOneShot500ThenBlobStageFailsSurfacesBothErrors() async throws {
        StubURLProtocol.responder = { request in
            let path = request.url?.path ?? ""
            let ctype = request.value(forHTTPHeaderField: "Content-Type") ?? ""
            if path == "/publish" && ctype != "application/json" {
                return (500, Data(#"{"detail":"one-shot boom"}"#.utf8))
            }
            if path == "/blobs" { return (502, Data(#"{"detail":"blob boom"}"#.utf8)) }
            XCTFail("publishFromBlob should not be reached if stageBlob fails")
            return (404, Data())
        }
        do {
            _ = try await client().publishBundle(name: "demo", data: bundle)
            XCTFail("expected a publish failure")
        } catch let RoostClientError.server(status, message) {
            // Leads with the one-shot status (R90: the genuine new-CP bug first).
            XCTAssertEqual(status, 500, "status must be the one-shot's, not the fallback's")
            XCTAssertTrue(message.contains("one-shot boom"),
                          "one-shot error must lead: \(message)")
            // and still names the fallback failure underneath.
            XCTAssertTrue(message.contains("two-step blob flow"), message)
            XCTAssertTrue(message.contains("502") || message.contains("blob boom"), message)
            // Order: the one-shot text precedes the fallback's.
            let oneShotIdx = message.range(of: "one-shot boom")!.lowerBound
            let fallbackIdx = message.range(of: "two-step blob flow")!.lowerBound
            XCTAssertTrue(oneShotIdx < fallbackIdx, "one-shot must come first")
        }
        XCTAssertEqual(StubURLProtocol.requests.count, 2, "stops after stageBlob fails")
    }

    func testOneShot500ThenPublishFromBlobFailsSurfacesBothErrors() async throws {
        StubURLProtocol.responder = { request in
            let path = request.url?.path ?? ""
            let ctype = request.value(forHTTPHeaderField: "Content-Type") ?? ""
            if path == "/publish" && ctype != "application/json" {
                return (500, Data(#"{"detail":"one-shot boom"}"#.utf8))
            }
            if path == "/blobs" { return (200, Data(self.blobJSON.utf8)) }
            // The second /publish (JSON) fails on the old CP too.
            return (500, Data(#"{"detail":"publish-from-blob boom"}"#.utf8))
        }
        do {
            _ = try await client().publishBundle(name: "demo", data: bundle)
            XCTFail("expected a publish failure")
        } catch let RoostClientError.server(status, message) {
            XCTAssertEqual(status, 500)
            XCTAssertTrue(message.contains("one-shot boom"), message)
            XCTAssertTrue(message.contains("two-step blob flow"), message)
            XCTAssertTrue(message.contains("publish-from-blob boom"), message)
        }
        XCTAssertEqual(StubURLProtocol.requests.count, 3)
    }

    // MARK: - 401/403 short-circuits: no fallback (it would only re-fail)

    func testOneShotUnauthorizedDoesNotFallBack() async throws {
        StubURLProtocol.responder = { _ in (403, Data(#"{"detail":"admin token required"}"#.utf8)) }
        do {
            _ = try await client().publishBundle(name: "demo", data: bundle)
            XCTFail("expected unauthorized")
        } catch RoostClientError.unauthorized {
            // expected
        }
        XCTAssertEqual(StubURLProtocol.requests.count, 1, "401/403 must not retry via blob")
    }

    // MARK: - new methods stand alone

    func testStageBlobPostsRawBodyWithName() async throws {
        StubURLProtocol.responder = { request in
            XCTAssertEqual(request.url?.path, "/blobs")
            XCTAssertTrue((request.url?.query ?? "").contains("name=demo.tar.gz"))
            return (200, Data(self.blobJSON.utf8))
        }
        let blob = try await client().stageBlob(name: "demo.tar.gz", data: bundle)
        XCTAssertEqual(blob.id, "blob123abc")
    }

    func testPublishFromBlobPostsJSONBody() async throws {
        StubURLProtocol.responder = { request in
            XCTAssertEqual(request.url?.path, "/publish")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "application/json")
            let obj = (try? JSONSerialization.jsonObject(
                with: StubURLProtocol.bodies.last ?? Data())) as? [String: Any]
            XCTAssertEqual(obj?["name"] as? String, "demo")
            XCTAssertEqual(obj?["blob_id"] as? String, "blob123abc")
            return (200, Data(self.siteJSON.utf8))
        }
        let site = try await client().publishFromBlob(name: "demo", blobID: "blob123abc")
        XCTAssertEqual(site.slug, "demo")
    }
}

// MARK: - URLProtocol stub (Linux + Darwin)

/// A sequenced HTTP stub: the test sets a `responder` closure mapping a request
/// to `(status, body)`. Records every request (and its body) so tests can assert
/// the call sequence and posted payloads. URLProtocol works on both
/// swift-corelibs-foundation (Linux) and Darwin.
final class StubURLProtocol: URLProtocol, @unchecked Sendable {
    // URLProtocol is instantiated by the loading system per-request, so the
    // stub state is static (single-threaded test driver, serialized requests).
    nonisolated(unsafe) static var responder: ((URLRequest) -> (Int, Data))?
    nonisolated(unsafe) static var requests: [URLRequest] = []
    nonisolated(unsafe) static var bodies: [Data] = []

    static func reset() {
        responder = nil
        requests = []
        bodies = []
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        Self.requests.append(request)
        // URLProtocol strips httpBody into httpBodyStream for upload-ish requests
        // on some platforms; capture whichever is present so body assertions work.
        Self.bodies.append(Self.body(of: request))
        guard let responder = Self.responder else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
            return
        }
        let (status, data) = responder(request)
        let response = HTTPURLResponse(
            url: request.url!, statusCode: status,
            httpVersion: "HTTP/1.1", headerFields: nil)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}

    private static func body(of request: URLRequest) -> Data {
        if let body = request.httpBody { return body }
        guard let stream = request.httpBodyStream else { return Data() }
        stream.open()
        defer { stream.close() }
        var data = Data()
        let bufSize = 4096
        let buffer = UnsafeMutablePointer<UInt8>.allocate(capacity: bufSize)
        defer { buffer.deallocate() }
        while stream.hasBytesAvailable {
            let read = stream.read(buffer, maxLength: bufSize)
            if read <= 0 { break }
            data.append(buffer, count: read)
        }
        return data
    }
}
