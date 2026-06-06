import Foundation

/// New-session sheet store: composes a `POST /jobs` from the text + toggles,
/// keeps the last 10 prompts locally for one-tap reuse (DESIGN §3.3).
@MainActor
final class NewSessionStore: ObservableObject {
    @Published var text: String = ""
    @Published var kind: Kind = .agent
    @Published var pinWorker: Bool = false
    @Published var selectedWorker: String?
    @Published var workers: [Worker] = []
    @Published var error: String?
    @Published var submitting = false

    enum Kind: String { case agent, command }

    private weak var app: AppState?
    private let recentKey = "roost.recentPrompts"

    func bind(_ app: AppState) { self.app = app }

    var recentPrompts: [String] {
        UserDefaults.standard.stringArray(forKey: recentKey) ?? []
    }

    /// Load the worker list to feed the pin-a-worker picker (from /derived).
    func loadWorkers() async {
        guard let api = app?.api else { return }
        if let d = try? await api.derived() {
            workers = d.workers.filter(\.isLive)
        }
    }

    /// Dispatch the job and return its id for navigation. nil on failure.
    func dispatch() async -> String? {
        guard let api = app?.api else { return nil }
        let prompt = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !prompt.isEmpty else { error = "Say or type something first."; return nil }

        let requires: [String: JSONValue]
        if pinWorker, let w = selectedWorker {
            requires = ["worker": .string(w)]
        } else {
            requires = [:]   // auto-place
        }

        let submit: JobSubmit
        switch kind {
        case .agent:
            submit = JobSubmit(intent: prompt, kind: "claude",
                               requires: requires, command: nil)
        case .command:
            submit = JobSubmit(intent: nil, kind: "command",
                               requires: requires, command: prompt)
        }

        submitting = true
        defer { submitting = false }
        do {
            let job = try await api.submit(submit)
            remember(prompt)
            return job.id
        } catch ApiError.unauthorized {
            app?.handleUnauthorized(); return nil
        } catch let ApiError.forbidden(detail) {
            error = "Not allowed: \(detail)"; return nil
        } catch {
            // `self.` is load-bearing: bare `error` is the immutable catch binding.
            self.error = "Dispatch failed."; return nil
        }
    }

    /// Push to the front of the recent list, dedupe, cap at 10.
    private func remember(_ prompt: String) {
        var list = recentPrompts.filter { $0 != prompt }
        list.insert(prompt, at: 0)
        if list.count > 10 { list = Array(list.prefix(10)) }
        UserDefaults.standard.set(list, forKey: recentKey)
    }
}
