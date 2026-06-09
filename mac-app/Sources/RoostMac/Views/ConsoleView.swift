#if os(macOS)
import RoostKit
import SwiftUI

/// The thin header strip above the Console terminal (DESIGN.md §13). Hosted via
/// NSHostingView inside the Console window's container — the terminal itself is
/// a raw, app-owned NSView (see ConsoleSession), so it never restarts on its
/// own. This view only reflects state and offers Restart / Open folder.
struct ConsoleHeader: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        HStack(spacing: 10) {
            status
            Text("~/RoostConsole").foregroundStyle(.tertiary)
            fleetBadge
            Spacer()
            Button("Open folder") { model.console.openWorkspaceFolder() }
                .controlSize(.small)
            Button(restartTitle) { model.console.restart() }
                .controlSize(.small)
                .keyboardShortcut("r", modifiers: [.command, .shift])
        }
        .font(.caption)
        .padding(.horizontal, 12)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(.bar)
    }

    @ViewBuilder
    private var status: some View {
        switch model.console.state {
        case .running(kind: .claude):
            Label("claude", systemImage: "sparkle").foregroundStyle(.primary)
        case .running(kind: .shell):
            Label("zsh — claude not installed", systemImage: "terminal")
                .foregroundStyle(.orange)
        case .ended(let code):
            Label("session ended\(code.map { " · exit \($0)" } ?? "")",
                  systemImage: "stop.circle")
                .foregroundStyle(.secondary)
        case .idle:
            Label("starting…", systemImage: "terminal").foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private var fleetBadge: some View {
        if model.console.isConfigured {
            Label("fleet ✓", systemImage: "checkmark.circle")
                .foregroundStyle(.green)
                .help("ROOST_URL and ROOST_TOKEN are set; the roost MCP server is attached when available.")
        } else {
            Label("not connected", systemImage: "exclamationmark.circle")
                .foregroundStyle(.orange)
        }
    }

    private var restartTitle: String {
        if case .ended = model.console.state { return "Restart  ⇧⌘R" }
        return "Restart"
    }
}
#endif
