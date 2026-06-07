import SwiftUI

/// Notification-settings sheet (R37 / DESIGN.md §6 v1.1). The user enters the
/// ntfy topic their control plane is configured to POST to (`roost serve
/// --notify-url https://ntfy.sh/<topic>`); the app stores the canonical subscribe
/// URL. Subscribing for real is the device-only half (see `PushService`) — on iOS
/// the honest v1.1 path is subscribing to that topic in the ntfy app; this screen
/// makes the topic an explicit, validated setting.
struct NotificationSettingsView: View {
    @StateObject private var store = NotificationSettingsStore()
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("ntfy topic or URL", text: $store.input)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                        .accessibilityIdentifier("notifications-topic-field")
                    if let preview = store.preview {
                        LabeledContent("Subscribes to", value: preview)
                            .font(.footnote)
                    }
                } header: {
                    Text("Job notifications")
                } footer: {
                    Text("Enter the ntfy topic your control plane posts to "
                         + "(`roost serve --notify-url …`). Then subscribe to the "
                         + "same topic in the ntfy app to get a push when a job "
                         + "finishes. A bare name uses ntfy.sh; paste a full URL "
                         + "for a self-hosted server.")
                }

                if let saved = store.savedURL {
                    Section("Current") {
                        LabeledContent("Watching", value: saved)
                            .font(.footnote)
                        Button("Stop watching", role: .destructive) { store.clear() }
                    }
                }

                if let error = store.error {
                    Text(error).font(.footnote).foregroundStyle(.red)
                }
            }
            .accessibilityIdentifier("notifications-form")
            .navigationTitle("Notifications")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("Done") { dismiss() }
                        .accessibilityIdentifier("notifications-done")
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Save") { store.save() }
                        .disabled(!store.canSave)
                }
            }
        }
    }
}
