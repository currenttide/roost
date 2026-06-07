import SwiftUI

/// New-session sheet (DESIGN §3.3): editable prompt + hold-to-talk mic,
/// auto/pin and agent/command toggles, dispatch → navigate into the session.
struct NewSessionView: View {
    @EnvironmentObject var app: AppState
    @StateObject private var store = NewSessionStore()
    @StateObject private var dictation = Dictation()
    @Environment(\.dismiss) private var dismiss

    /// Called with the new job id once dispatch succeeds.
    let onDispatched: (String) -> Void

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    promptField
                    if dictation.isAvailable { micButton }
                } header: { Text("Intent") }

                if !store.recentPrompts.isEmpty {
                    Section("Recent") {
                        ForEach(store.recentPrompts, id: \.self) { p in
                            Button { store.text = p } label: {
                                Text(p).lineLimit(1).foregroundStyle(.primary)
                            }
                        }
                    }
                }

                Section("Target") {
                    Picker("Placement", selection: $store.pinWorker) {
                        Text("Auto").tag(false)
                        Text("Pin a worker").tag(true)
                    }
                    .pickerStyle(.segmented)
                    if store.pinWorker {
                        Picker("Worker", selection: $store.selectedWorker) {
                            Text("—").tag(String?.none)
                            ForEach(store.workers) { w in
                                Text(w.name ?? w.id).tag(Optional(w.id))
                            }
                        }
                    }
                }

                Section("Kind") {
                    Picker("Kind", selection: $store.kind) {
                        Text("Agent").tag(NewSessionStore.Kind.agent)
                        Text("Command").tag(NewSessionStore.Kind.command)
                    }
                    .pickerStyle(.segmented)
                }

                if let error = store.error {
                    Text(error).font(.footnote).foregroundStyle(.red)
                }
            }
            .accessibilityIdentifier("new-session-form")
            .navigationTitle("New session")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("Cancel") { dismiss() }
                        .accessibilityIdentifier("new-session-cancel")
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Dispatch") {
                        Task {
                            if let id = await store.dispatch() { onDispatched(id) }
                        }
                    }
                    .accessibilityIdentifier("new-session-dispatch")
                    .disabled(store.submitting ||
                              store.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
        }
        .onAppear {
            store.bind(app)
            Task { await store.loadWorkers() }
            Task { await dictation.requestAuthorization() }
        }
        // Mirror live partial transcript into the editable field.
        .onChange(of: dictation.transcript) { _, t in
            if dictation.isRecording { store.text = t }
        }
    }

    private var promptField: some View {
        TextField("refactor the matcher to use capability sets…",
                  text: $store.text, axis: .vertical)
            .lineLimit(3...8)
            .accessibilityIdentifier("new-session-prompt")
    }

    /// Hold-to-talk: press starts dictation (seeded with current text), release
    /// stops it. The recognized text lands in the editable field.
    private var micButton: some View {
        HStack {
            Image(systemName: dictation.isRecording ? "waveform" : "mic.fill")
                .foregroundStyle(dictation.isRecording ? Color.red : Color.accentColor)
            Text(dictation.isRecording ? "Listening… release to stop" : "Hold to talk")
                .font(.callout)
                .foregroundStyle(.secondary)
            Spacer()
        }
        .contentShape(Rectangle())
        .gesture(
            DragGesture(minimumDistance: 0)
                .onChanged { _ in
                    if !dictation.isRecording { dictation.start(seed: store.text) }
                }
                .onEnded { _ in dictation.stop() }
        )
    }
}
