#if os(macOS)
import AppKit
import Foundation
import Observation
import RoostKit
import Security
import ServiceManagement

/// User preferences. URL + toggles live in UserDefaults; the token lives in
/// the Keychain only (DESIGN.md §6).
@MainActor
@Observable
final class AppSettings {
    private let defaults = UserDefaults.standard

    var urlString: String {
        didSet { defaults.set(urlString, forKey: "connection.url") }
    }
    var hasCompletedOnboarding: Bool {
        didSet { defaults.set(hasCompletedOnboarding, forKey: "onboarding.done") }
    }
    var showDockIcon: Bool {
        didSet {
            defaults.set(showDockIcon, forKey: "general.dockIcon")
            NSApp.setActivationPolicy(showDockIcon ? .regular : .accessory)
        }
    }
    var hotkeyEnabled: Bool {
        didSet { defaults.set(hotkeyEnabled, forKey: "general.hotkey") }
    }
    var notifyTerminal: Bool {
        didSet { defaults.set(notifyTerminal, forKey: "notify.terminal") }
    }
    var notifyFleetAlert: Bool {
        didSet { defaults.set(notifyFleetAlert, forKey: "notify.alert") }
    }
    var notifyWorkerOffline: Bool {
        didSet { defaults.set(notifyWorkerOffline, forKey: "notify.workerOffline") }
    }
    var notifyStuck: Bool {
        didSet { defaults.set(notifyStuck, forKey: "notify.stuck") }
    }
    /// Poll cadence while the UI is visible (DESIGN.md M2 "cadence override").
    var visibleCadence: Double {
        didSet { defaults.set(visibleCadence, forKey: "general.visibleCadence") }
    }

    init() {
        let d = UserDefaults.standard
        urlString = d.string(forKey: "connection.url") ?? ""
        hasCompletedOnboarding = d.bool(forKey: "onboarding.done")
        showDockIcon = d.bool(forKey: "general.dockIcon")
        hotkeyEnabled = d.object(forKey: "general.hotkey") == nil
            ? true : d.bool(forKey: "general.hotkey")
        notifyTerminal = d.object(forKey: "notify.terminal") == nil
            ? true : d.bool(forKey: "notify.terminal")
        notifyFleetAlert = d.object(forKey: "notify.alert") == nil
            ? true : d.bool(forKey: "notify.alert")
        notifyWorkerOffline = d.bool(forKey: "notify.workerOffline")  // default off — noisy
        notifyStuck = d.object(forKey: "notify.stuck") == nil
            ? true : d.bool(forKey: "notify.stuck")
        let cadence = d.double(forKey: "general.visibleCadence")
        visibleCadence = cadence > 0 ? cadence : 2
    }

    // MARK: token (Keychain)

    var token: String? {
        get { KeychainStore.read() }
        set {
            if let newValue, !newValue.isEmpty {
                KeychainStore.write(newValue)
            } else {
                KeychainStore.delete()
            }
        }
    }

    var connection: RoostConnection? {
        RoostConnection(urlString: urlString, token: token)
    }

    // MARK: launch at login

    var launchAtLogin: Bool {
        get { SMAppService.mainApp.status == .enabled }
        set {
            // Best-effort; the OS may prompt the user. Errors surface in
            // System Settings > Login Items, not as app failures.
            if newValue {
                try? SMAppService.mainApp.register()
            } else {
                try? SMAppService.mainApp.unregister()
            }
        }
    }
}

/// Minimal Keychain wrapper for the one secret we hold.
enum KeychainStore {
    private static let service = "com.roost.mac"
    private static let account = "control-plane-token"

    private static var query: [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
    }

    static func read() -> String? {
        var q = query
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data
        else { return nil }
        return String(data: data, encoding: .utf8)
    }

    static func write(_ value: String) {
        let data = Data(value.utf8)
        let status = SecItemUpdate(
            query as CFDictionary,
            [kSecValueData as String: data] as CFDictionary)
        if status == errSecItemNotFound {
            var q = query
            q[kSecValueData as String] = data
            SecItemAdd(q as CFDictionary, nil)
        }
    }

    static func delete() {
        SecItemDelete(query as CFDictionary)
    }
}
#endif
