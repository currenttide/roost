import Foundation
import XCTest
@testable import Roost

/// Locates the golden fixtures copied into the test bundle from ../fixtures
/// (one source of truth in the repo; copied at build time by project.yml).
enum Fixtures {
    /// The fixtures folder is added as a folder reference, so files live under
    /// `Bundle/fixtures/<name>`. We resolve robustly: try the subdirectory
    /// first, then a flat layout, so the suite isn't brittle to how Xcode
    /// flattens the resource.
    static func url(_ name: String) -> URL {
        // Linux harness (SwiftPM, no Xcode bundle): point ROOST_FIXTURES at the
        // repo's mobile-app/fixtures. First-chance so it also wins under Xcode
        // if explicitly set.
        if let dir = ProcessInfo.processInfo.environment["ROOST_FIXTURES"] {
            let u = URL(fileURLWithPath: dir).appendingPathComponent(name)
            if FileManager.default.fileExists(atPath: u.path) { return u }
        }
        let bundle = Bundle(for: BundleToken.self)
        let parts = name.split(separator: ".")
        let ext = parts.count > 1 ? String(parts.last!) : ""
        let base = ext.isEmpty ? name : String(name.dropLast(ext.count + 1))
        if let u = bundle.url(forResource: base, withExtension: ext,
                              subdirectory: "fixtures") {
            return u
        }
        if let u = bundle.url(forResource: base, withExtension: ext) {
            return u
        }
        // Last resort: walk the resource path (covers odd flattening).
        if let resPath = bundle.resourcePath {
            let candidate = URL(fileURLWithPath: resPath)
                .appendingPathComponent("fixtures").appendingPathComponent(name)
            if FileManager.default.fileExists(atPath: candidate.path) { return candidate }
            let flat = URL(fileURLWithPath: resPath).appendingPathComponent(name)
            if FileManager.default.fileExists(atPath: flat.path) { return flat }
        }
        XCTFail("fixture not found: \(name)")
        return URL(fileURLWithPath: "/dev/null")
    }

    static func data(_ name: String) -> Data {
        (try? Data(contentsOf: url(name))) ?? Data()
    }

    static func string(_ name: String) -> String {
        (try? String(contentsOf: url(name), encoding: .utf8)) ?? ""
    }

    /// Every fixture file in the directory, for the "decode them all" sweep.
    static let allJSON = [
        "blob_upload_response.json", "derived.json", "error_401.json",
        "error_403_admin_endpoint.json", "error_404_job.json", "healthz.json",
        "job_cancel_response.json", "job_derived_running.json",
        "job_detail_queued.json", "job_detail_running.json",
        "job_detail_succeeded.json", "job_logs.json", "job_logs_since_2.json",
        "jobs_list.json", "job_submit_response.json", "job_tree.json",
        "pair_token_response.json", "publish_list.json",
        "publish_oneshot_response.json", "publish_response.json", "workers.json",
    ]
}

private final class BundleToken {}
