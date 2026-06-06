import Foundation
import Security

/// The paired credential: control-plane URL + mobile-scoped bearer token.
struct Credential: Equatable {
    let url: String
    let token: String
    let name: String?
}

/// Keychain-backed credential store (the *only* singleton besides the URLSession
/// owner, per the brief). We persist the credential as a single JSON blob under
/// a generic-password item so url+token live together and rotate atomically.
///
/// WHY Keychain and not UserDefaults: the token is a bearer credential to the
/// fleet (DESIGN §6 — "never in plain files").
final class Keychain {
    static let shared = Keychain()
    private init() {}

    private let service = "rs.roost.mobile"
    private let account = "credential"

    private struct Stored: Codable {
        let url: String
        let token: String
        let name: String?
    }

    func save(_ cred: Credential) {
        let blob = (try? JSONEncoder().encode(
            Stored(url: cred.url, token: cred.token, name: cred.name))) ?? Data()
        // Upsert: delete any existing item then add fresh.
        let base: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        SecItemDelete(base as CFDictionary)
        var add = base
        add[kSecValueData as String] = blob
        // Available after first unlock; survives until explicit unpair. Not
        // synced to iCloud (no kSecAttrSynchronizable) — token is device-local.
        add[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
        SecItemAdd(add as CFDictionary, nil)
    }

    func load() -> Credential? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data,
              let s = try? JSONDecoder().decode(Stored.self, from: data)
        else { return nil }
        return Credential(url: s.url, token: s.token, name: s.name)
    }

    func clear() {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        SecItemDelete(query as CFDictionary)
    }
}
