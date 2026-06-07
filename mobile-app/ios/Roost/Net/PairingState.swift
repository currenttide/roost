import Foundation

/// Pure pairing-flow state machine (R83). Lifted out of `PairingStore` so the
/// idle → contacting(host) → failed(error) / paired / cancelled transitions are
/// Linux-testable without URLSession, Keychain, or SwiftUI. The store drives it;
/// the view renders `caption` / `errorMessage` / `isBusy` off it.
///
/// `host` is the user-facing display host for the "Contacting <host>…" caption —
/// just the authority (host[:port]) of the pairing URL, never the token.
enum PairingState: Equatable {
    case idle
    case contacting(host: String)
    case failed(message: String)
    case cancelled
    case paired

    /// True while a probe is in flight — drives the spinner + Cancel affordance.
    var isBusy: Bool { if case .contacting = self { return true }; return false }

    /// Caption shown under the field while contacting; nil otherwise. The user
    /// finally sees *what* the spinner is doing and *which host* it's reaching.
    var caption: String? {
        if case .contacting(let host) = self { return "Contacting \(host)…" }
        return nil
    }

    /// The inline error to render in red, if any.
    var errorMessage: String? {
        if case .failed(let m) = self { return m }
        return nil
    }

    // MARK: Transitions (return the next state; pure)

    /// Begin a probe to `url`. Derives the display host for the caption. Always a
    /// fresh `contacting`, clearing any prior error.
    static func beginning(url: String) -> PairingState {
        .contacting(host: displayHost(url))
    }

    /// User tapped Cancel mid-probe. Clears the spinner and shows no error (a
    /// deliberate cancel is not a failure) — only meaningful while contacting.
    func cancelling() -> PairingState {
        if case .contacting = self { return .cancelled }
        return self
    }

    /// Extract a friendly host[:port] from a URL string for the caption. Falls
    /// back to the raw string when it can't be parsed — never crashes, never
    /// leaks the token (which lives only in the payload, not the URL).
    static func displayHost(_ url: String) -> String {
        guard let comps = URLComponents(string: url), let host = comps.host else {
            return url
        }
        if let port = comps.port { return "\(host):\(port)" }
        return host
    }
}
