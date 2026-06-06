#if os(macOS)
import RoostKit
import SwiftUI

/// The single most important interaction (DESIGN.md §4): type a goal, ⏎.
/// Options are progressive disclosure and reset after every submission —
/// the defaults are the contract.
struct GoalBoxView: View {
    @Environment(AppModel.self) private var model

    @State private var text = ""
    @State private var showOptions = false
    @State private var captain = false
    @State private var verify = true
    @State private var preferWorker: String?
    @State private var modelName = ""
    @State private var maxTokensText = ""
    @State private var submitting = false
    @State private var error: String?
    @FocusState private var focused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                TextField("Tell your fleet what to do…", text: $text)
                    .textFieldStyle(.roundedBorder)
                    .focused($focused)
                    .onSubmit(submit)
                    .disabled(disabledReason != nil || submitting)
                Button {
                    withAnimation(.easeOut(duration: 0.15)) { showOptions.toggle() }
                } label: {
                    Image(systemName: showOptions ? "chevron.up" : "chevron.down")
                }
                .buttonStyle(.borderless)
                .help("Options")
            }

            if let disabledReason {
                Text(disabledReason)
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
            }

            if let error {
                Text(error)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .lineLimit(2)
            }

            if showOptions {
                options
            }
        }
        .onAppear { focused = true }
    }

    private var disabledReason: String? {
        switch model.store.reachability {
        case .unreachable: return "Goal box disabled — control plane unreachable"
        case .unauthorized: return "Goal box disabled — unauthorized"
        case .ok, .never: return nil
        }
    }

    private var options: some View {
        VStack(alignment: .leading, spacing: 8) {
            Toggle("Multi-step plan (captain)", isOn: $captain)
            Toggle("Verify result", isOn: $verify)

            Picker("Prefer worker", selection: $preferWorker) {
                Text("(auto)").tag(String?.none)
                ForEach(model.store.workers
                    .filter { $0.status == .idle || $0.status == .busy }) { worker in
                    Text(worker.name).tag(String?.some(worker.id))
                }
            }

            TextField("Model (fleet default)", text: $modelName)
                .textFieldStyle(.roundedBorder)
            TextField("Token budget (none)", text: $maxTokensText)
                .textFieldStyle(.roundedBorder)
        }
        .font(.caption)
        .controlSize(.small)
        .padding(8)
        .background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 6))
    }

    private func submit() {
        let goal = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !goal.isEmpty, !submitting else { return }
        submitting = true
        error = nil
        let opts = (captain: captain, verify: verify, prefer: preferWorker,
                    model: modelName.isEmpty ? nil : modelName,
                    budget: Int(maxTokensText))
        Task { @MainActor in
            do {
                try await model.store.submitGoal(
                    goal, captain: opts.captain, verify: opts.verify,
                    preferWorker: opts.prefer, model: opts.model,
                    maxTokens: opts.budget)
                text = ""
                resetOptions()
            } catch {
                self.error = error.localizedDescription
            }
            submitting = false
        }
    }

    /// Options remember nothing between submissions (§4).
    private func resetOptions() {
        showOptions = false
        captain = false
        verify = true
        preferWorker = nil
        modelName = ""
        maxTokensText = ""
    }
}
#endif
