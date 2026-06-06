import XCTest
@testable import RoostKit

final class BlobTests: XCTestCase {

    // shape produced by roost/blobs.py public_dict()
    let blobFixture = """
    {
      "id": "a1b2c3d4e5f6", "name": "report.pdf", "size": 123456,
      "sha256": "deadbeef", "state": "ready",
      "created_at": 1765432100.0, "expires_at": 1765518500.0,
      "get_url": "http://hubbase:8787/blobs/a1b2c3d4e5f6?exp=1765518500&sig=abc123"
    }
    """

    func testDecodeReadyBlob() throws {
        let blob = try JSONDecoder().decode(Blob.self, from: Data(blobFixture.utf8))
        XCTAssertEqual(blob.id, "a1b2c3d4e5f6")
        XCTAssertEqual(blob.name, "report.pdf")
        XCTAssertEqual(blob.size, 123_456)
        XCTAssertTrue(blob.isReady)
        XCTAssertNil(blob.putURL)
        XCTAssertTrue(blob.getURL.contains("sig="))
    }

    func testDecodePendingBlobWithPutURL() throws {
        let pending = """
        {"id": "x1", "name": "fetched.log", "size": 0, "sha256": null,
         "state": "pending", "created_at": 1.0, "expires_at": 2.0,
         "get_url": "http://h/blobs/x1?exp=2&sig=g",
         "put_url": "http://h/blobs/x1?exp=2&sig=p"}
        """
        let blob = try JSONDecoder().decode(Blob.self, from: Data(pending.utf8))
        XCTAssertFalse(blob.isReady)
        XCTAssertNotNil(blob.putURL)
    }

    func testUploadRequestCarriesNameAndTTL() {
        let client = RoostClient(connection: RoostConnection(
            urlString: "http://hubbase:8787", token: "t")!)
        let request = client.makeRequest(
            "POST", "/blobs", query: ["name": "a b.txt", "ttl_sec": "3600"])
        let url = request.url!.absoluteString
        XCTAssertTrue(url.hasPrefix("http://hubbase:8787/blobs?"))
        XCTAssertTrue(url.contains("name=a%20b.txt"))
        XCTAssertTrue(url.contains("ttl_sec=3600"))
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer t")
    }

    func testMalformedBlobRowsTolerated() throws {
        let mixed = """
        [{"name": "no-id"}, {"id": "ok1", "name": "fine", "size": 1,
          "state": "ready", "created_at": 1, "expires_at": 2, "get_url": "u"}]
        """
        let rows = try JSONDecoder().decode([Tolerant<Blob>].self, from: Data(mixed.utf8))
        XCTAssertEqual(rows.compactMap(\.value).map(\.id), ["ok1"])
    }
}
