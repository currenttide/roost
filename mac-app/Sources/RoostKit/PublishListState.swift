import Foundation

/// The single display state for the Publish pane's "sites on this control plane"
/// list, derived from the load result. Putting the decision here (instead of in
/// the SwiftUI view) keeps the "404 means the endpoint is missing, not a failure"
/// rule Linux-testable and makes the view a dumb renderer.
///
/// The bug this fixes (R93): the pane did `sites = (try? await client.sites()) ?? sites`
/// — a load failure was swallowed silently, so against a control plane missing
/// `/publish` (or on any transport failure) the list simply showed "No sites yet."
/// with ZERO feedback. That is the inverse of R81's double-stack: instead of two
/// contradictory states, there was no signal at all. This seam gives the pane the
/// same four mutually-exclusive states the schedules pane already has.
public enum PublishListState: Equatable, Sendable {
    /// First load in flight, nothing to show yet.
    case loading
    /// `GET /publish` doesn't exist on this control plane (404) — older server.
    /// Mutually exclusive with `.empty`/`.error`: no contradiction, no silence.
    case unavailable
    /// Reached the endpoint, but a real error (auth, transport, server, decode).
    /// The view offers a Retry.
    case error(String)
    /// Loaded successfully, zero sites — the genuine empty state.
    case empty
    /// Loaded successfully with rows. `count` lets the view show a header total
    /// without re-counting and without the view owning the list type.
    case list(count: Int)

    /// Decide the display state from the load outcome.
    ///
    /// - Parameters:
    ///   - siteCount: rows from the last successful load (0 if none / never).
    ///   - loadError: the classified error from the last load attempt, if any.
    ///   - loading: whether a load is currently in flight.
    ///   - hasLoaded: whether at least one load attempt has completed (success or
    ///     failure). Distinguishes "first load, no data yet" from "loaded, empty".
    public static func decide(
        siteCount: Int,
        loadError: PublishLoadError?,
        loading: Bool,
        hasLoaded: Bool
    ) -> PublishListState {
        // A real error always wins over stale rows — but a 404 is not an error,
        // it's an absent endpoint, so it gets its own state.
        if let loadError {
            switch loadError {
            case .endpointMissing:
                return .unavailable
            case .message(let text):
                return .error(text)
            }
        }
        if siteCount > 0 {
            return .list(count: siteCount)
        }
        // No rows and no error. It's only genuinely empty once a load has
        // completed; before that we're still on the first fetch.
        if hasLoaded {
            return .empty
        }
        return loading ? .loading : .empty
    }
}

/// Classified load failure for the Publish pane's site list. A 404 is special-cased
/// so the view never treats "endpoint not on this server" as a contradictory error
/// (and, crucially, never swallows it into a misleading empty state).
public enum PublishLoadError: Equatable, Sendable {
    /// `GET /publish` returned 404 — the control plane predates the feature.
    case endpointMissing
    /// Any other failure, already turned into a user-facing message.
    case message(String)

    /// Map a `RoostClientError` from a `sites()` request into the pane's
    /// classification. 404 → `.endpointMissing`; 401/403 → the token message;
    /// everything else → the error's own description.
    ///
    /// `sites()` routes 404 through `RoostClientError.notFound`, but a future
    /// reshape (or a path that maps 404 to `.server(status: 404, …)`) must classify
    /// the same way, so both are treated as the missing endpoint.
    public static func from(_ error: Error) -> PublishLoadError {
        if let clientError = error as? RoostClientError {
            switch clientError {
            case .notFound:
                return .endpointMissing
            case .server(let status, _) where status == 404:
                return .endpointMissing
            case .unauthorized:
                return .message("A client or admin token is required to list published sites.")
            default:
                return .message(clientError.errorDescription ?? "\(clientError)")
            }
        }
        return .message((error as NSError).localizedDescription)
    }
}
