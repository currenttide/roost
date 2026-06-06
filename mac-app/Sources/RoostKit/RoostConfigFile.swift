import Foundation

/// Reads the CLI's `~/.config/roost/config.toml` so onboarding can offer
/// "Use this Mac's Roost config" with one click (DESIGN.md §6).
///
/// This is a *minimal* TOML reader for the flat `key = "value"` file the CLI
/// writes (url, credential, worker_id, name) — not a general TOML parser.
/// The app only ever reads this file; it never writes to ~/.config/roost/.
public struct RoostConfigFile: Equatable, Sendable {
    public let url: String?
    public let credential: String?
    public let workerID: String?
    public let name: String?
    public let path: String

    /// Resolution mirrors the CLI: $ROOST_CONFIG_DIR, else ~/.config/roost.
    public static func defaultPath(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        home: String = NSHomeDirectory()
    ) -> String {
        let dir = environment["ROOST_CONFIG_DIR"] ?? "\(home)/.config/roost"
        return "\(dir)/config.toml"
    }

    public static func load(path: String = defaultPath()) -> RoostConfigFile? {
        guard let text = try? String(contentsOfFile: path, encoding: .utf8) else {
            return nil
        }
        return parse(text, path: path)
    }

    static func parse(_ text: String, path: String = "") -> RoostConfigFile {
        var values: [String: String] = [:]
        for rawLine in text.split(separator: "\n", omittingEmptySubsequences: false) {
            let line = rawLine.trimmingCharacters(in: .whitespaces)
            if line.isEmpty || line.hasPrefix("#") || line.hasPrefix("[") { continue }
            guard let eq = line.firstIndex(of: "=") else { continue }
            let key = line[..<eq].trimmingCharacters(in: .whitespaces)
            var value = line[line.index(after: eq)...].trimmingCharacters(in: .whitespaces)
            // strip a trailing comment on unquoted values
            if !value.hasPrefix("\""), let hash = value.firstIndex(of: "#") {
                value = value[..<hash].trimmingCharacters(in: .whitespaces)
            }
            // unquote "..." (basic strings; the CLI writes nothing fancier)
            if value.hasPrefix("\""), value.hasSuffix("\""), value.count >= 2 {
                value = String(value.dropFirst().dropLast())
                    .replacingOccurrences(of: "\\\"", with: "\"")
                    .replacingOccurrences(of: "\\\\", with: "\\")
            }
            guard !key.isEmpty else { continue }
            values[key] = value
        }
        return RoostConfigFile(
            url: values["url"],
            credential: values["credential"],
            workerID: values["worker_id"],
            name: values["name"],
            path: path
        )
    }
}
