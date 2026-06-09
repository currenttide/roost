#if os(macOS)
import AppKit
import Foundation
import Observation
import RoostKit
import SwiftTerm
import SwiftUI

/// The Console's brain (DESIGN.md §13): discovers binaries, generates the
/// fleet wiring (env, MCP config, workspace CLAUDE.md), and OWNS the terminal
/// view + PTY for the app's lifetime.
///
/// Redesign: the `LocalProcessTerminalView` is retained here and hosted as the
/// raw contentView of the Console window — it is never wrapped in
/// `NSViewRepresentable`, so navigation and window hide/show can't tear down
/// the live Claude session. Restart is explicit and imperative.
@MainActor
@Observable
final class ConsoleSession {
    enum State: Equatable {
        case idle                       // never started
        case running(kind: Kind)
        case ended(exitCode: Int32?)
    }

    enum Kind: Equatable {
        case claude                     // claude, fleet-wired
        case shell                      // fallback: plain zsh + install hint
    }

    private unowned let model: AppModel

    private(set) var state: State = .idle
    /// Deep-linked prompt to type (not submit) once the session is up.
    private(set) var queuedPrompt: String?

    /// Filled when the terminal is (re)built so the session can type into the PTY.
    @ObservationIgnored private var sendText: ((String) -> Void)?

    @ObservationIgnored private var _container: ConsoleContainerView?
    @ObservationIgnored private var terminalView: LocalProcessTerminalView?
    @ObservationIgnored private let processDelegate = ConsoleProcessDelegate()

    init(model: AppModel) {
        self.model = model
        processDelegate.session = self
    }

    /// The Console window's contentView: a container holding the SwiftUI header
    /// strip over the retained terminal. Built (and the PTY started) on first
    /// access; reused forever after.
    var contentView: NSView {
        if let _container { return _container }
        let container = ConsoleContainerView(
            headerHost: NSHostingView(rootView: ConsoleHeader().environment(model)))
        _container = container
        startTerminal()
        return container
    }

    // MARK: lifecycle

    private func startTerminal() {
        let term = LocalProcessTerminalView(frame: .zero)
        term.processDelegate = processDelegate
        term.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)

        let launch = makeLaunch()
        sendText = { [weak term] text in term?.send(txt: text) }
        term.startProcess(
            executable: "/bin/zsh",
            args: ["-lc", launch.script],
            environment: launch.environment)
        terminalView = term
        _container?.setTerminal(term)
        markRunning(kind: launch.kind)
    }

    func markRunning(kind: Kind) {
        state = .running(kind: kind)
        if let prompt = queuedPrompt {
            queuedPrompt = nil
            // Let claude's TUI finish drawing before typing. Type, don't
            // submit — the user reviews and hits ⏎ (DESIGN.md §13).
            Task { @MainActor in
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                self.sendText?(prompt)
            }
        }
    }

    func markEnded(exitCode: Int32?) {
        state = .ended(exitCode: exitCode)
    }

    /// Explicit restart: SIGTERM the old child deterministically, then build a
    /// fresh terminal + PTY in place (the window is untouched).
    func restart() {
        terminalView?.terminate()
        terminalView = nil
        state = .idle
        startTerminal()
    }

    /// App quit: kill the child so no orphaned agent is left behind.
    func shutdown() {
        terminalView?.terminate()
        terminalView = nil
    }

    func queue(prompt: String) {
        queuedPrompt = prompt
        if case .running = state {
            queuedPrompt = nil
            sendText?(prompt)
        }
    }

    var isConfigured: Bool { model.store.isConfigured }

    func openWorkspaceFolder() {
        NSWorkspace.shared.open(
            FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent("RoostConsole"))
    }

    // MARK: what to launch

    struct Launch {
        let script: String              // run under /bin/zsh -lc
        let environment: [String]       // KEY=value
        let kind: Kind
    }

    func makeLaunch() -> Launch {
        let workspace = prepareWorkspace()
        let claude = findBinary("claude", extraCandidates: [
            "~/.claude/local/claude",
        ])
        let kind: Kind = claude != nil ? .claude : .shell

        var script = "cd \(shQuote(workspace.path))"
        if let claude {
            if let mcpConfig = writeMCPConfig() {
                script += " && exec \(shQuote(claude)) --mcp-config \(shQuote(mcpConfig.path))"
            } else {
                script += " && exec \(shQuote(claude))"
            }
        } else {
            script += """
             ; echo ''; echo '  Claude Code is not installed on this Mac.'; \
            echo '  Install it:  npm install -g @anthropic-ai/claude-code'; \
            echo '  (plain zsh below — the roost CLI works here too)'; echo ''; exec zsh -i
            """
        }
        return Launch(script: script, environment: makeEnvironment(), kind: kind)
    }

    // MARK: wiring pieces (all generated; the user's ~/.claude is never touched)

    private func makeEnvironment() -> [String] {
        var env = ProcessInfo.processInfo.environment
        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"
        if env["LANG"] == nil { env["LANG"] = "en_US.UTF-8" }
        if let connection = model.store.client?.connection {
            env["ROOST_URL"] = connection.baseURL.absoluteString
            if let token = connection.token {
                // Stays on this machine, in this child process — opening the
                // Console is the consent (DESIGN.md §13).
                env["ROOST_TOKEN"] = token
            }
        }
        return env.map { "\($0.key)=\($0.value)" }
    }

    /// `~/RoostConsole/` with a fresh CLAUDE.md so Claude has fleet context
    /// from message one.
    private func prepareWorkspace() -> URL {
        let home = FileManager.default.homeDirectoryForCurrentUser
        let dir = home.appendingPathComponent("RoostConsole")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)

        let workers = model.store.workers
            .map { "- \($0.name) (\($0.os ?? "?")/\($0.arch ?? "?"), \($0.statusRaw))" }
            .joined(separator: "\n")
        let url = model.store.client?.connection.baseURL.absoluteString ?? "(not connected)"
        let context = """
        # Roost Console

        You are connected to a Roost fleet — a pull-based orchestrator for agent
        jobs across heterogeneous machines. This terminal was opened from the
        Roost menu bar app.

        - Control plane: \(url) (`ROOST_URL` and `ROOST_TOKEN` are set in this
          environment; the `roost` CLI uses them automatically).
        - The `roost` MCP server is attached: prefer its tools to inspect the
          fleet, submit goals, and watch runs.
        - Useful CLI: `roost workers`, `roost do "<goal>"`, `roost status <id>`,
          `roost logs <id> --follow`, `roost history`.

        ## Current fleet
        \(workers.isEmpty ? "(no snapshot yet — ask via the MCP tools)" : workers)
        """
        try? context.write(
            to: dir.appendingPathComponent("CLAUDE.md"),
            atomically: true, encoding: .utf8)
        return dir
    }

    /// Generated MCP config passed via --mcp-config; nil when the roost CLI
    /// isn't installed (the Console still works, just without MCP tools).
    private func writeMCPConfig() -> URL? {
        guard let roost = findBinary("roost") else { return nil }
        guard let support = FileManager.default.urls(
            for: .applicationSupportDirectory, in: .userDomainMask).first
        else { return nil }
        let dir = support.appendingPathComponent("Roost/console")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)

        var serverEnv: [String: String] = [:]
        if let connection = model.store.client?.connection {
            serverEnv["ROOST_URL"] = connection.baseURL.absoluteString
            if let token = connection.token { serverEnv["ROOST_TOKEN"] = token }
        }
        let config: [String: Any] = [
            "mcpServers": [
                "roost": [
                    "command": roost,
                    "args": ["mcp"],
                    "env": serverEnv,
                ],
            ],
        ]
        let url = dir.appendingPathComponent("mcp.json")
        guard let data = try? JSONSerialization.data(
            withJSONObject: config, options: [.prettyPrinted, .sortedKeys])
        else { return nil }
        try? data.write(to: url)
        // contains the token — keep it owner-only
        try? FileManager.default.setAttributes(
            [.posixPermissions: 0o600], ofItemAtPath: url.path)
        return url
    }

    // MARK: binary discovery

    private func findBinary(_ name: String, extraCandidates: [String] = []) -> String? {
        let home = NSHomeDirectory()
        var candidates = extraCandidates.map {
            $0.replacingOccurrences(of: "~", with: home)
        }
        candidates += [
            "/opt/homebrew/bin/\(name)",
            "/usr/local/bin/\(name)",
            "\(home)/.local/bin/\(name)",
            "\(home)/bin/\(name)",
        ]
        for path in candidates where FileManager.default.isExecutableFile(atPath: path) {
            return path
        }
        // last resort: the user's login-shell PATH
        let probe = Process()
        probe.executableURL = URL(fileURLWithPath: "/bin/zsh")
        probe.arguments = ["-lc", "command -v \(name)"]
        let pipe = Pipe()
        probe.standardOutput = pipe
        probe.standardError = Pipe()
        guard (try? probe.run()) != nil else { return nil }
        probe.waitUntilExit()
        guard probe.terminationStatus == 0 else { return nil }
        let path = String(
            decoding: pipe.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return path.isEmpty ? nil : path
    }

    private func shQuote(_ s: String) -> String {
        "'" + s.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }
}

/// Container for the Console window: a fixed-height SwiftUI header over the
/// terminal, laid out with Auto Layout. The terminal is swapped on Restart;
/// the header host is permanent.
final class ConsoleContainerView: NSView {
    private let headerHost: NSView
    private var terminal: NSView?

    init(headerHost: NSView) {
        self.headerHost = headerHost
        super.init(frame: NSRect(x: 0, y: 0, width: 820, height: 560))
        headerHost.translatesAutoresizingMaskIntoConstraints = false
        addSubview(headerHost)
        NSLayoutConstraint.activate([
            headerHost.topAnchor.constraint(equalTo: topAnchor),
            headerHost.leadingAnchor.constraint(equalTo: leadingAnchor),
            headerHost.trailingAnchor.constraint(equalTo: trailingAnchor),
            headerHost.heightAnchor.constraint(equalToConstant: 32),
        ])
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("not used") }

    func setTerminal(_ view: NSView) {
        terminal?.removeFromSuperview()
        terminal = view
        view.translatesAutoresizingMaskIntoConstraints = false
        addSubview(view)
        NSLayoutConstraint.activate([
            view.topAnchor.constraint(equalTo: headerHost.bottomAnchor),
            view.leadingAnchor.constraint(equalTo: leadingAnchor),
            view.trailingAnchor.constraint(equalTo: trailingAnchor),
            view.bottomAnchor.constraint(equalTo: bottomAnchor),
        ])
    }
}

/// Bridges SwiftTerm's process-termination callback back to the session.
@MainActor
final class ConsoleProcessDelegate: NSObject, LocalProcessTerminalViewDelegate {
    weak var session: ConsoleSession?

    nonisolated func sizeChanged(
        source: LocalProcessTerminalView, newCols: Int, newRows: Int) {}
    nonisolated func setTerminalTitle(
        source: LocalProcessTerminalView, title: String) {}
    nonisolated func hostCurrentDirectoryUpdate(
        source: TerminalView, directory: String?) {}
    nonisolated func processTerminated(source: TerminalView, exitCode: Int32?) {
        Task { @MainActor in self.session?.markEnded(exitCode: exitCode) }
    }
}
#endif
