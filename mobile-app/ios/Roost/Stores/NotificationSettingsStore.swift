import Foundation
import SwiftUI

/// Notification-settings store (R37 / DESIGN.md §6 v1.1). Holds the ntfy topic
/// the app subscribes to for terminal-job pushes. The control plane is configured
/// with `--notify-url` and POSTs there on each terminal job; it does NOT expose
/// that topic over the API, so this is a manual SETTING (DESIGN.md §6).
///
/// The topic is NOT a secret (it's a pub/sub channel name, not a bearer token),
/// so it lives in `UserDefaults` rather than the Keychain — distinct from the
/// paired credential. The pure normalization/validation is in `NtfyTopic`
/// (Foundation-only, Linux-tested); this store is the thin persistence around it.
@MainActor
final class NotificationSettingsStore: ObservableObject {
    /// What the user typed (a bare topic or a full URL); editable in the field.
    @Published var input: String = ""
    /// Inline validation message, or nil when the input is empty/valid.
    @Published private(set) var error: String?

    private let defaults: UserDefaults
    private let key = "rs.roost.notify_topic_url"

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        // Seed the field from the last saved topic (shown as the bare name).
        if let url = defaults.string(forKey: key) {
            input = NtfyTopic.displayTopic(url) ?? url
        }
    }

    /// The canonical subscribe URL currently saved, if any.
    var savedURL: String? { defaults.string(forKey: key) }

    /// The normalized URL the current `input` would save to (live preview), or nil
    /// if it isn't a valid topic yet.
    var preview: String? { NtfyTopic.normalize(input) }

    /// Save is allowed once the input normalizes to a valid subscribe URL.
    var canSave: Bool { preview != nil }

    /// Persist the normalized topic URL. Sets `error` (and saves nothing) when the
    /// input can't be made into a valid ntfy subscribe URL.
    func save() {
        guard let url = NtfyTopic.normalize(input) else {
            error = "Enter an ntfy topic (e.g. roost-yang) or a full https://ntfy.sh/… URL."
            return
        }
        defaults.set(url, forKey: key)
        error = nil
    }

    /// Forget the configured topic (stop watching).
    func clear() {
        defaults.removeObject(forKey: key)
        input = ""
        error = nil
    }
}
