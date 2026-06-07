import Foundation

/// The single display state for the Transfers pane's "staged on the control plane"
/// list, derived from the load result. Putting the decision here (instead of in the
/// SwiftUI view) keeps the "404 means the endpoint is missing, not a failure" rule
/// Linux-testable and makes the view a dumb renderer.
///
/// The bug this fixes (R93): the pane did `try? await model.transfers.refreshStaged()`
/// — a load failure was swallowed silently, so against a control plane missing
/// `/blobs` (or on any transport failure) the staged section simply rendered nothing
/// with ZERO feedback. This seam gives the pane the same four mutually-exclusive
/// states the schedules pane already has.
public enum TransfersListState: Equatable, Sendable {
    /// First load in flight, nothing to show yet.
    case loading
    /// `GET /blobs` doesn't exist on this control plane (404) — older server.
    /// Mutually exclusive with `.empty`/`.error`: no contradiction, no silence.
    case unavailable
    /// Reached the endpoint, but a real error (auth, transport, server, decode).
    /// The view offers a Retry.
    case error(String)
    /// Loaded successfully, zero staged blobs — the genuine empty state.
    case empty
    /// Loaded successfully with rows. `count` lets the view drive a header total
    /// without re-counting and without the view owning the list type.
    case list(count: Int)

    /// Decide the display state from the load outcome.
    ///
    /// - Parameters:
    ///   - blobCount: rows from the last successful load (0 if none / never).
    ///   - loadError: the classified error from the last load attempt, if any.
    ///   - loading: whether a load is currently in flight.
    ///   - hasLoaded: whether at least one load attempt has completed (success or
    ///     failure). Distinguishes "first load, no data yet" from "loaded, empty".
    public static func decide(
        blobCount: Int,
        loadError: TransfersLoadError?,
        loading: Bool,
        hasLoaded: Bool
    ) -> TransfersListState {
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
        if blobCount > 0 {
            return .list(count: blobCount)
        }
        // No rows and no error. It's only genuinely empty once a load has
        // completed; before that we're still on the first fetch.
        if hasLoaded {
            return .empty
        }
        return loading ? .loading : .empty
    }
}

/// Classified load failure for the Transfers pane's staged-blob list. A 404 is
/// special-cased so the view never treats "endpoint not on this server" as a
/// contradictory error (and never swallows it into a misleading empty state).
public enum TransfersLoadError: Equatable, Sendable {
    /// `GET /blobs` returned 404 — the control plane predates the feature.
    case endpointMissing
    /// Any other failure, already turned into a user-facing message.
    case message(String)

    /// Map a `RoostClientError` from a `listBlobs()` request into the pane's
    /// classification. 404 → `.endpointMissing`; 401/403 → the token message;
    /// everything else → the error's own description.
    ///
    /// `listBlobs()` maps a 404 to `RoostClientError.server(status: 404, …)` (not
    /// `.notFound`), so both forms are treated as the missing endpoint to stay
    /// robust against that and any future reshape.
    public static func from(_ error: Error) -> TransfersLoadError {
        if let clientError = error as? RoostClientError {
            switch clientError {
            case .notFound:
                return .endpointMissing
            case .server(let status, _) where status == 404:
                return .endpointMissing
            case .unauthorized:
                return .message("A client or admin token is required to list staged files.")
            default:
                return .message(clientError.errorDescription ?? "\(clientError)")
            }
        }
        return .message((error as NSError).localizedDescription)
    }
}
