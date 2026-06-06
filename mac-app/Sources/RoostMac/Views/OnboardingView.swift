#if os(macOS)
import RoostKit
import SwiftUI

/// First-run connect flow (DESIGN.md §6): use the CLI's config in one click,
/// enter URL + token manually, or get pointed at `roost up`. The app never
/// installs software.
struct OnboardingView: View {
    private enum Path: Hashable {
        case detected, manual, noFleet
    }

    @Environment(AppModel.self) private var model

    @State private var detected: RoostConfigFile?
    @State private var path: Path = .manual
    @State private var urlText = ""
    @State private var tokenText = ""
    @State private var validating = false
    @State private var error: String?
    @State private var httpWarning = false

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            VStack(spacing: 4) {
                Image(systemName: "bird.fill").font(.system(size: 30))
                Text("Welcome to Roost").font(.title2.weight(.semibold))
                Text("Your fleet in the menu bar.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity)

            if let detected {
                option(.detected,
                       title: "Use this Mac's Roost config",
                       subtitle: "\(detected.path)\n→ \(detected.url ?? "?")"
                           + (detected.name.map { " · worker “\($0)”" } ?? ""))
            }
            option(.manual,
                   title: "Connect to a control plane",
                   subtitle: "Enter the URL and token of a running fleet.")
            if path == .manual {
                manualFields.padding(.leading, 24)
            }
            option(.noFleet,
                   title: "I don't have a fleet yet",
                   subtitle: "Two commands stand one up on this Mac.")
            if path == .noFleet {
                noFleetHelp.padding(.leading, 24)
            }

            if let error {
                Label(error, systemImage: "exclamationmark.triangle")
                    .font(.caption)
                    .foregroundStyle(.red)
            }
            if httpWarning {
                Label("Token will be sent over plain HTTP — fine on a trusted network, use HTTPS otherwise.",
                      systemImage: "lock.open")
                    .font(.caption)
                    .foregroundStyle(.orange)
            }

            HStack {
                Spacer()
                if validating { ProgressView().controlSize(.small) }
                Button("Connect") { connect() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(validating || path == .noFleet
                              || (path == .manual && urlText.isEmpty))
            }
        }
        .padding(24)
        .frame(width: 460)
        .onAppear(perform: detect)
    }

    // MARK: pieces

    private func option(_ value: Path, title: String, subtitle: String) -> some View {
        Button {
            path = value
            error = nil
            updateHTTPWarning()
        } label: {
            HStack(alignment: .top, spacing: 8) {
                Image(systemName: path == value ? "largecircle.fill.circle" : "circle")
                    .foregroundStyle(path == value ? Color.accentColor : .secondary)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).font(.body.weight(.medium))
                    Text(subtitle)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.leading)
                }
                Spacer()
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private var manualFields: some View {
        VStack(alignment: .leading, spacing: 6) {
            TextField("http://hubbase:8787", text: $urlText)
                .textFieldStyle(.roundedBorder)
                .onChange(of: urlText) { updateHTTPWarning() }
            SecureField("Token (leave empty for a no-auth loopback plane)",
                        text: $tokenText)
                .textFieldStyle(.roundedBorder)
        }
    }

    private var noFleetHelp: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("In a terminal:")
                .font(.caption)
                .foregroundStyle(.secondary)
            HStack {
                Text(Self.quickstart)
                    .font(.system(size: 11, design: .monospaced))
                    .textSelection(.enabled)
                Spacer()
                Button {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(Self.quickstart, forType: .string)
                } label: {
                    Image(systemName: "doc.on.doc")
                }
                .buttonStyle(.borderless)
                .help("Copy")
            }
            .padding(8)
            .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 6))
            Text("`roost up` starts a control plane and enrolls this Mac as the first worker. Then come back and press Recheck.")
                .font(.caption)
                .foregroundStyle(.secondary)
            Button("Recheck") { recheck() }
                .controlSize(.small)
        }
    }

    private static let quickstart = """
    uv tool install --python 3.12 roost-fleet
    roost up
    """

    // MARK: behavior

    private func detect() {
        detected = RoostConfigFile.load()
        if let detected, detected.url != nil {
            path = .detected
        }
        updateHTTPWarning()
    }

    private func updateHTTPWarning() {
        let candidate = path == .detected ? (detected?.url ?? "") : urlText
        httpWarning = candidate.hasPrefix("http://")
            && !candidate.contains("127.0.0.1") && !candidate.contains("localhost")
            && !tokenIsEmptyForCurrentPath
    }

    private var tokenIsEmptyForCurrentPath: Bool {
        path == .detected ? (detected?.credential ?? "").isEmpty : tokenText.isEmpty
    }

    private func recheck() {
        detect()
        if detected == nil {
            // a freshly-run `roost up` defaults to loopback
            urlText = "http://127.0.0.1:8787"
            path = .manual
            connect()
        } else {
            connect()
        }
    }

    private func connect() {
        let url: String
        let token: String?
        switch path {
        case .detected:
            url = detected?.url ?? ""
            token = detected?.credential
        case .manual, .noFleet:
            url = urlText
            token = tokenText.isEmpty ? nil : tokenText
        }
        guard let connection = RoostConnection(urlString: url, token: token) else {
            error = "That doesn't look like a valid http(s) URL."
            return
        }

        validating = true
        error = nil
        Task { @MainActor in
            do {
                // reachable → roost → authorized, with distinct errors (§6)
                try await RoostClient(connection: connection).validate()
                model.applyConnection(urlString: url, token: token)
                validating = false
                closeWindow()
            } catch let e as RoostClientError {
                validating = false
                error = e.errorDescription
            } catch {
                validating = false
                self.error = error.localizedDescription
            }
        }
    }

    private func closeWindow() {
        NSApp.keyWindow?.close()
    }
}
#endif
