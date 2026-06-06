import XCTest
@testable import RoostKit
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

final class ClientAndConfigTests: XCTestCase {

    // MARK: connection parsing

    func testConnectionURLNormalization() {
        XCTAssertEqual(
            RoostConnection(urlString: "http://hubbase:8787/")?.baseURL.absoluteString,
            "http://hubbase:8787")
        XCTAssertEqual(
            RoostConnection(urlString: "  https://roost.example.com  ", token: "t")?.isHTTPS,
            true)
        XCTAssertNil(RoostConnection(urlString: ""))
        XCTAssertNil(RoostConnection(urlString: "not a url"))
        XCTAssertNil(RoostConnection(urlString: "ftp://x"))
        // empty token normalizes to nil so we don't send a bogus header
        XCTAssertNil(RoostConnection(urlString: "http://x:1", token: "")?.token)
    }

    // MARK: request building

    func testRequestCarriesAuthAndQuery() {
        let client = RoostClient(connection: RoostConnection(
            urlString: "http://hubbase:8787", token: "secret-token")!)
        let request = client.makeRequest("GET", "/derived", query: ["limit": "40"])
        XCTAssertEqual(request.url?.absoluteString, "http://hubbase:8787/derived?limit=40")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"),
                       "Bearer secret-token")
    }

    func testRequestWithoutToken() {
        let client = RoostClient(connection: RoostConnection(
            urlString: "http://127.0.0.1:8787")!)
        let request = client.makeRequest("DELETE", "/jobs/abc", query: ["tree": "true"])
        XCTAssertNil(request.value(forHTTPHeaderField: "Authorization"))
        XCTAssertEqual(request.httpMethod, "DELETE")
        XCTAssertEqual(request.url?.absoluteString,
                       "http://127.0.0.1:8787/jobs/abc?tree=true")
    }

    // MARK: submission encoding

    func testGoalSubmissionEncodesOnlySetFields() throws {
        let sub = JobSubmission.goal("lint the repo")
        let json = try jsonObject(sub)
        XCTAssertEqual(json["task"] as? String, "lint the repo")
        XCTAssertEqual(json["intent"] as? String, "lint the repo")
        XCTAssertEqual(json["kind"] as? String, "auto")
        XCTAssertEqual(json["verify"] as? Bool, true)
        XCTAssertNil(json["prefer"], "unset fields must be omitted, not null")
        XCTAssertNil(json["model"])
        XCTAssertNil(json["budget"])
        XCTAssertNil(json["hierarchy"])
    }

    func testCaptainSubmission() throws {
        let sub = JobSubmission.goal(
            "migrate the docs site", captain: true,
            preferWorker: "w1", model: "opus", maxTokens: 500_000)
        let json = try jsonObject(sub)
        XCTAssertEqual(json["kind"] as? String, "captain")
        XCTAssertEqual((json["hierarchy"] as? [String: Bool])?["can_dispatch"], true)
        XCTAssertEqual((json["prefer"] as? [String: String])?["worker"], "w1")
        XCTAssertEqual((json["budget"] as? [String: Int])?["max_tokens"], 500_000)
        XCTAssertEqual(json["model"] as? String, "opus")
    }

    private func jsonObject(_ value: some Encodable) throws -> [String: Any] {
        let data = try JSONEncoder().encode(value)
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    // MARK: config.toml

    func testParseCLIConfig() {
        let toml = """
        # roost config
        url = "http://hubbase:8787"
        credential = "rst-wkr-abc123"
        worker_id = "deadbeef1234"
        name = "macmini"
        """
        let config = RoostConfigFile.parse(toml)
        XCTAssertEqual(config.url, "http://hubbase:8787")
        XCTAssertEqual(config.credential, "rst-wkr-abc123")
        XCTAssertEqual(config.workerID, "deadbeef1234")
        XCTAssertEqual(config.name, "macmini")
    }

    func testParseTolerance() {
        let toml = """
        url = "http://x:1"
        # comment
        weird_line_without_equals
        [some_section]
        port = 8787  # trailing comment on unquoted value
        escaped = "say \\"hi\\""
        """
        let config = RoostConfigFile.parse(toml)
        XCTAssertEqual(config.url, "http://x:1")
        XCTAssertNil(config.credential)
        // section headers and junk lines are skipped, not fatal
    }

    func testDefaultPathHonorsConfigDirOverride() {
        let path = RoostConfigFile.defaultPath(
            environment: ["ROOST_CONFIG_DIR": "/tmp/roost-conf"], home: "/Users/x")
        XCTAssertEqual(path, "/tmp/roost-conf/config.toml")
        let fallback = RoostConfigFile.defaultPath(environment: [:], home: "/Users/x")
        XCTAssertEqual(fallback, "/Users/x/.config/roost/config.toml")
    }

    // MARK: JSONValue

    func testJSONValueAccessors() throws {
        let raw = #"{"a": 1, "b": "s", "c": [1, "x"], "d": {"e": true}, "f": null}"#
        let v = try JSONDecoder().decode(JSONValue.self, from: Data(raw.utf8))
        XCTAssertEqual(v["a"]?.intValue, 1)
        XCTAssertEqual(v["b"]?.stringValue, "s")
        XCTAssertEqual(v["c"]?.arrayValue?.count, 2)
        XCTAssertEqual(v["d"]?["e"]?.boolValue, true)
        XCTAssertEqual(v["f"], .null)
        XCTAssertEqual(v["d"]?["e"]?.displayText, "true")
        XCTAssertEqual(v["a"]?.displayText, "1")
    }
}
