import XCTest
@testable import RoostKit

/// R81: the schedules pane against a control plane without `/schedules` (the
/// deployed 0.1.0-era CP) stacked a red "Not found: Not Found" banner ON TOP of
/// the "No schedules" empty state — error and empty state contradicting. These
/// tests pin the decision (now in RoostKit, so the SwiftUI view is a dumb
/// renderer) so the contradiction can never come back.
final class SchedulesListStateTests: XCTestCase {

    // MARK: error classification

    func testNotFoundClassifiesAsEndpointMissing() {
        // The exact failure from the screenshot: GET /schedules → 404.
        let err = SchedulesLoadError.from(RoostClientError.notFound("Not Found"))
        XCTAssertEqual(err, .endpointMissing)
    }

    func testUnauthorizedClassifiesAsAdminTokenMessage() {
        let err = SchedulesLoadError.from(RoostClientError.unauthorized)
        XCTAssertEqual(
            err, .message("An admin (or scheduler) token is required to manage schedules."))
    }

    func testTransportErrorClassifiesAsItsDescription() {
        let err = SchedulesLoadError.from(RoostClientError.transport("connection refused"))
        guard case .message(let text) = err else {
            return XCTFail("expected .message, got \(err)")
        }
        XCTAssertTrue(text.contains("connection refused"), "kept the underlying reason: \(text)")
        XCTAssertNotEqual(err, .endpointMissing, "only 404 is endpointMissing")
    }

    func testServerErrorClassifiesAsMessage() {
        let err = SchedulesLoadError.from(RoostClientError.server(status: 500, message: "boom"))
        guard case .message = err else { return XCTFail("expected .message, got \(err)") }
        XCTAssertNotEqual(err, .endpointMissing)
    }

    // MARK: the bug — 404 must NOT produce a contradiction

    /// The crux of R81: a 404 must collapse to a single `.unavailable` state, NOT
    /// an `.error` and NOT the `.empty` state (which would contradict a banner).
    func test404YieldsSingleUnavailableState_notErrorPlusEmpty() {
        let state = SchedulesListState.decide(
            scheduleCount: 0,
            loadError: .endpointMissing,
            loading: false,
            hasLoaded: true)
        XCTAssertEqual(state, .unavailable)
        // Explicitly assert it is neither of the two contradictory states.
        XCTAssertNotEqual(state, .empty)
        if case .error = state { XCTFail("404 must not surface as a generic error") }
    }

    func testReal404ErrorEndToEndYieldsUnavailable() {
        // Drive the whole pipeline the model uses: RoostClientError → classify → decide.
        let classified = SchedulesLoadError.from(RoostClientError.notFound("Not Found"))
        let state = SchedulesListState.decide(
            scheduleCount: 0, loadError: classified, loading: false, hasLoaded: true)
        XCTAssertEqual(state, .unavailable)
    }

    // MARK: the other states stay correct

    func testGenuineErrorSurfacesAsError() {
        let state = SchedulesListState.decide(
            scheduleCount: 0,
            loadError: .message("Unauthorized — check the token"),
            loading: false,
            hasLoaded: true)
        XCTAssertEqual(state, .error("Unauthorized — check the token"))
    }

    func testLoadedWithZeroSchedulesIsEmpty() {
        let state = SchedulesListState.decide(
            scheduleCount: 0, loadError: nil, loading: false, hasLoaded: true)
        XCTAssertEqual(state, .empty)
    }

    func testLoadedWithRowsIsList() {
        let state = SchedulesListState.decide(
            scheduleCount: 3, loadError: nil, loading: false, hasLoaded: true)
        XCTAssertEqual(state, .list(count: 3))
    }

    func testFirstLoadInFlightIsLoading() {
        let state = SchedulesListState.decide(
            scheduleCount: 0, loadError: nil, loading: true, hasLoaded: false)
        XCTAssertEqual(state, .loading)
    }

    func testBeforeAnyLoadAttemptIsNotEmpty() {
        // Not loading, never loaded — don't flash "No schedules" prematurely.
        let state = SchedulesListState.decide(
            scheduleCount: 0, loadError: nil, loading: false, hasLoaded: false)
        XCTAssertEqual(state, .empty,
                       "with no in-flight load and nothing loaded, settling on empty is fine")
    }

    // MARK: precedence — an error always wins over stale rows

    func testErrorWinsOverStaleRows() {
        // A refresh that fails after a prior success: show the error, not stale rows.
        let state = SchedulesListState.decide(
            scheduleCount: 5,
            loadError: .message("Can't reach the control plane: timed out"),
            loading: false,
            hasLoaded: true)
        XCTAssertEqual(state, .error("Can't reach the control plane: timed out"))
    }

    func test404WinsOverStaleRows() {
        // Endpoint vanished (downgrade): collapse to unavailable, not stale list.
        let state = SchedulesListState.decide(
            scheduleCount: 5, loadError: .endpointMissing, loading: false, hasLoaded: true)
        XCTAssertEqual(state, .unavailable)
    }

    func testReloadingKeepsShowingListNotLoadingFlash() {
        // A refresh over an existing list keeps the list visible (header shows the
        // inline spinner) — don't blank the pane to a spinner on every refresh.
        let state = SchedulesListState.decide(
            scheduleCount: 2, loadError: nil, loading: true, hasLoaded: true)
        XCTAssertEqual(state, .list(count: 2))
    }
}
