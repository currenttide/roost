#if os(macOS)
import RoostKit
import SwiftUI

struct SettingsView: View {
    @Environment(AppModel.self) private var model

    @State private var urlText = ""
    @State private var tokenText = ""
    @State private var testing = false
    @State private var testResult: (ok: Bool, message: String)?
    @State private var loaded = false

    var body: some View {
        @Bindable var settings = model.settings
        Form {
            Section("Connection") {
                TextField("Control plane URL", text: $urlText)
                SecureField("Token", text: $tokenText)
                if urlText.hasPrefix("http://"),
                   !urlText.contains("127.0.0.1"), !urlText.contains("localhost"),
                   !tokenText.isEmpty {
                    Label("Token sent over plain HTTP — fine on a trusted network, use HTTPS otherwise.",
                          systemImage: "lock.open")
                        .font(.caption)
                        .foregroundStyle(.orange)
                }
                HStack {
                    Button("Test & Save") { testAndSave() }
                        .disabled(testing || urlText.isEmpty)
                    if testing { ProgressView().controlSize(.small) }
                    if let testResult {
                        Label(testResult.message,
                              systemImage: testResult.ok
                                  ? "checkmark.circle" : "exclamationmark.triangle")
                            .font(.caption)
                            .foregroundStyle(testResult.ok ? .green : .red)
                    }
                    Spacer()
                }
            }

            Section("Notifications") {
                Toggle("Run finished (verified / failed)", isOn: $settings.notifyTerminal)
                Toggle("Fleet alert", isOn: $settings.notifyFleetAlert)
                Toggle("Run may be stuck", isOn: $settings.notifyStuck)
                Toggle("Worker went offline", isOn: $settings.notifyWorkerOffline)
            }

            Section("General") {
                Picker("Refresh while visible", selection: $settings.visibleCadence) {
                    Text("1 s").tag(1.0)
                    Text("2 s (default)").tag(2.0)
                    Text("5 s").tag(5.0)
                }
                Toggle("Global hotkey ⌥⌘R opens the goal box", isOn: $settings.hotkeyEnabled)
                Toggle("Show Dock icon", isOn: $settings.showDockIcon)
                Toggle("Launch at login", isOn: Binding(
                    get: { settings.launchAtLogin },
                    set: { settings.launchAtLogin = $0 }))
            }

            if model.updates.isConfigured {
                Section("Updates") {
                    HStack {
                        if let release = model.updates.available {
                            Button("Download \(release.version)") {
                                model.updates.openDownload()
                            }
                        } else {
                            Button("Check for Updates") {
                                Task { @MainActor in
                                    await model.updates.check(force: true)
                                }
                            }
                            .disabled(model.updates.checking)
                            if model.updates.checking {
                                ProgressView().controlSize(.small)
                            } else if model.updates.lastChecked != nil {
                                Text("Up to date")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                        }
                        Spacer()
                    }
                }
            }
        }
        .formStyle(.grouped)
        .frame(width: 440)
        .fixedSize(horizontal: false, vertical: true)
        .onAppear {
            guard !loaded else { return }
            loaded = true
            urlText = model.settings.urlString
            tokenText = model.settings.token ?? ""
        }
    }

    private func testAndSave() {
        guard let connection = RoostConnection(
            urlString: urlText, token: tokenText.isEmpty ? nil : tokenText)
        else {
            testResult = (false, "Not a valid http(s) URL")
            return
        }
        testing = true
        testResult = nil
        Task { @MainActor in
            do {
                try await RoostClient(connection: connection).validate()
                model.applyConnection(
                    urlString: urlText,
                    token: tokenText.isEmpty ? nil : tokenText)
                testResult = (true, "Connected")
            } catch let e as RoostClientError {
                testResult = (false, e.errorDescription ?? "failed")
            } catch {
                testResult = (false, error.localizedDescription)
            }
            testing = false
        }
    }
}
#endif
