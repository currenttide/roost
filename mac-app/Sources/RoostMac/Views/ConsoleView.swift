#if os(macOS)
import AppKit
import RoostKit
import SwiftTerm
import SwiftUI

/// The Console pane (DESIGN.md §13): a thin header strip + the terminal.
struct ConsolePane: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            ZStack {
                ConsoleHostView(session: model.console)
                    .id(model.console.generation)  // restart = fresh PTY
                if case .ended(let code) = model.console.state {
                    endedOverlay(exitCode: code)
                }
            }
        }
        .background(Color(nsColor: .textBackgroundColor))
    }

    private var header: some View {
        HStack(spacing: 10) {
            switch model.console.state {
            case .running(kind: .claude):
                Label("claude", systemImage: "sparkle")
                    .foregroundStyle(.primary)
            case .running(kind: .shell):
                Label("zsh — claude not installed", systemImage: "terminal")
                    .foregroundStyle(.orange)
            case .ended:
                Label("session ended", systemImage: "terminal")
                    .foregroundStyle(.secondary)
            case .idle:
                Label("starting…", systemImage: "terminal")
                    .foregroundStyle(.secondary)
            }

            Text("~/RoostConsole")
                .foregroundStyle(.tertiary)

            if model.store.isConfigured {
                Label("fleet env ✓", systemImage: "checkmark.circle")
                    .foregroundStyle(.green)
                    .help("ROOST_URL and ROOST_TOKEN are set; the roost MCP server is attached when available.")
            } else {
                Label("not connected", systemImage: "exclamationmark.circle")
                    .foregroundStyle(.orange)
            }

            Spacer()

            Button("Open folder") {
                NSWorkspace.shared.open(
                    FileManager.default.homeDirectoryForCurrentUser
                        .appendingPathComponent("RoostConsole"))
            }
            .controlSize(.small)

            Button("Restart") { model.console.restart() }
                .controlSize(.small)
                .keyboardShortcut("r", modifiers: [.command, .shift])
        }
        .font(.caption)
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
    }

    private func endedOverlay(exitCode: Int32?) -> some View {
        VStack(spacing: 10) {
            Text("Session ended\(exitCode.map { " (exit \($0))" } ?? "")")
                .font(.headline)
            Button("Restart  ⇧⌘R") { model.console.restart() }
                .keyboardShortcut(.defaultAction)
        }
        .padding(24)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 10))
    }
}

/// Hosts SwiftTerm's LocalProcessTerminalView and runs the session script.
struct ConsoleHostView: NSViewRepresentable {
    let session: ConsoleSession

    func makeCoordinator() -> Coordinator {
        Coordinator(session: session)
    }

    func makeNSView(context: Context) -> LocalProcessTerminalView {
        let view = LocalProcessTerminalView(frame: .zero)
        view.processDelegate = context.coordinator
        view.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)

        let launch = session.makeLaunch()
        session.sendText = { [weak view] text in
            view?.send(txt: text)
        }
        view.startProcess(
            executable: "/bin/zsh",
            args: ["-lc", launch.script],
            environment: launch.environment)
        Task { @MainActor in
            session.markRunning(kind: launch.kind)
        }
        return view
    }

    func updateNSView(_ nsView: LocalProcessTerminalView, context: Context) {}

    @MainActor
    final class Coordinator: NSObject, LocalProcessTerminalViewDelegate {
        private let session: ConsoleSession

        init(session: ConsoleSession) {
            self.session = session
        }

        nonisolated func sizeChanged(
            source: LocalProcessTerminalView, newCols: Int, newRows: Int) {}

        nonisolated func setTerminalTitle(
            source: LocalProcessTerminalView, title: String) {}

        nonisolated func hostCurrentDirectoryUpdate(
            source: TerminalView, directory: String?) {}

        nonisolated func processTerminated(
            source: TerminalView, exitCode: Int32?) {
            Task { @MainActor in
                self.session.markEnded(exitCode: exitCode)
            }
        }
    }
}
#endif
