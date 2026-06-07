import Foundation

/// The single display state for a schedules-list pane, derived from the load
/// result. Putting the decision here (instead of in the SwiftUI view) keeps the
/// "404 means the endpoint is missing, not an error to stack on the empty state"
/// rule Linux-testable and makes the view a dumb renderer.
///
/// The bug this fixes (R81): a control plane predating `/schedules` (the deployed
/// 0.1.0-era server) returns 404. The old code routed that through the generic
/// `error` field *and* still showed the "No schedules" empty state — a red
/// "Not found: Not Found" banner contradicting an empty-but-fine empty state.
/// Mapping 404 to `.unavailable` collapses that into one honest message.
public enum SchedulesListState: Equatable, Sendable {
    /// First load in flight, nothing to show yet.
    case loading
    /// `/schedules` doesn't exist on this control plane (404) — older server.
    /// Mutually exclusive with `.empty`/`.error`: no contradiction.
    case unavailable
    /// Reached the endpoint, but a real error (auth, transport, server, decode).
    case error(String)
    /// Loaded successfully, zero schedules — the genuine empty state.
    case empty
    /// Loaded successfully with rows. `count` lets the view show the header total
    /// without re-counting and without the view owning the list type.
    case list(count: Int)

    /// Decide the display state from the load outcome.
    ///
    /// - Parameters:
    ///   - schedules: rows from the last successful load (empty if none / never).
    ///   - loadError: the error from the last load attempt, if any.
    ///   - loading: whether a load is currently in flight.
    ///   - hasLoaded: whether at least one load attempt has completed (success or
    ///     failure). Distinguishes "first load, no data yet" from "loaded, empty".
    public static func decide(
        scheduleCount: Int,
        loadError: SchedulesLoadError?,
        loading: Bool,
        hasLoaded: Bool
    ) -> SchedulesListState {
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
        if scheduleCount > 0 {
            return .list(count: scheduleCount)
        }
        // No rows and no error. It's only genuinely empty once a load has
        // completed; before that we're still on the first fetch.
        if hasLoaded {
            return .empty
        }
        return loading ? .loading : .empty
    }
}

/// Classified load failure for the schedules pane. A 404 is special-cased so the
/// view never treats "endpoint not on this server" as a contradictory error.
public enum SchedulesLoadError: Equatable, Sendable {
    /// `GET /schedules` returned 404 — the control plane predates the feature.
    case endpointMissing
    /// Any other failure, already turned into a user-facing message.
    case message(String)

    /// Map a `RoostClientError` from a schedules request into the pane's
    /// classification. 404 → `.endpointMissing`; 401/403 → the admin-token
    /// message; everything else → the error's own description.
    public static func from(_ error: Error) -> SchedulesLoadError {
        if let clientError = error as? RoostClientError {
            switch clientError {
            case .notFound:
                return .endpointMissing
            case .unauthorized:
                return .message("An admin (or scheduler) token is required to manage schedules.")
            default:
                return .message(clientError.errorDescription ?? "\(clientError)")
            }
        }
        return .message((error as NSError).localizedDescription)
    }
}
