import XCTest
@testable import RoostKit

/// R93: the Transfers pane swallowed its staged-blob load failure entirely
/// (`try? await model.transfers.refreshStaged()`). Against a control plane missing
/// `/blobs`, or on any transport failure, the staged section rendered nothing with
/// ZERO feedback. These tests pin the decision (now in RoostKit, so the SwiftUI view
/// is a dumb renderer) so a load failure can never again masquerade as an empty list.
final class TransfersListStateTests: XCTestCase {

    // MARK: error classification

    func testServer404ClassifiesAsEndpointMissing() {
        // listBlobs() maps a 404 to .server(status: 404, …) — that's the real shape.
        let err = TransfersLoadError.from(RoostClientError.server(status: 404, message: ""))
        XCTAssertEqual(err, .endpointMissing)
    }

    func testNotFoundAlsoClassifiesAsEndpointMissing() {
        // Robustness against a future reshape that uses .notFound for 404.
        let err = TransfersLoadError.from(RoostClientError.notFound("Not Found"))
        XCTAssertEqual(err, .endpointMissing)
    }

    func testUnauthorizedClassifiesAsTokenMessage() {
        let err = TransfersLoadError.from(RoostClientError.unauthorized)
        XCTAssertEqual(
            err, .message("A client or admin token is required to list staged files."))
    }

    func testTransportErrorClassifiesAsItsDescription() {
        let err = TransfersLoadError.from(RoostClientError.transport("connection refused"))
        guard case .message(let text) = err else {
            return XCTFail("expected .message, got \(err)")
        }
        XCTAssertTrue(text.contains("connection refused"), "kept the underlying reason: \(text)")
        XCTAssertNotEqual(err, .endpointMissing, "only a 404 is endpointMissing")
    }

    func testNon404ServerErrorClassifiesAsMessage() {
        let err = TransfersLoadError.from(RoostClientError.server(status: 500, message: "boom"))
        guard case .message = err else { return XCTFail("expected .message, got \(err)") }
        XCTAssertNotEqual(err, .endpointMissing)
    }

    // MARK: the bug — a load failure must NOT vanish into the empty state

    /// The crux of R93: a 404 collapses to a single `.unavailable` state, NOT the
    /// `.empty` state (which would silently look like "nothing staged").
    func test404YieldsUnavailable_notEmpty() {
        let state = TransfersListState.decide(
            blobCount: 0, loadError: .endpointMissing, loading: false, hasLoaded: true)
        XCTAssertEqual(state, .unavailable)
        XCTAssertNotEqual(state, .empty, "a missing endpoint must not look like an empty list")
        if case .error = state { XCTFail("404 must not surface as a generic error") }
    }

    /// A transport failure (the live-CP render-proof case: pointing at a dead port)
    /// must surface as a retryable `.error`, NOT a silent empty list.
    func testTransportFailureYieldsError_notEmpty() {
        let classified = TransfersLoadError.from(RoostClientError.transport("Could not connect to the server."))
        let state = TransfersListState.decide(
            blobCount: 0, loadError: classified, loading: false, hasLoaded: true)
        guard case .error(let text) = state else {
            return XCTFail("a transport failure must surface as .error, got \(state)")
        }
        XCTAssertTrue(text.contains("Could not connect"), "kept the reason: \(text)")
        XCTAssertNotEqual(state, .empty)
    }

    func testReal404EndToEndYieldsUnavailable() {
        let classified = TransfersLoadError.from(RoostClientError.server(status: 404, message: ""))
        let state = TransfersListState.decide(
            blobCount: 0, loadError: classified, loading: false, hasLoaded: true)
        XCTAssertEqual(state, .unavailable)
    }

    // MARK: the other states stay correct

    func testGenuineErrorSurfacesAsError() {
        let state = TransfersListState.decide(
            blobCount: 0,
            loadError: .message("Unauthorized — check the token"),
            loading: false, hasLoaded: true)
        XCTAssertEqual(state, .error("Unauthorized — check the token"))
    }

    func testLoadedWithZeroBlobsIsEmpty() {
        let state = TransfersListState.decide(
            blobCount: 0, loadError: nil, loading: false, hasLoaded: true)
        XCTAssertEqual(state, .empty)
    }

    func testLoadedWithRowsIsList() {
        let state = TransfersListState.decide(
            blobCount: 4, loadError: nil, loading: false, hasLoaded: true)
        XCTAssertEqual(state, .list(count: 4))
    }

    func testFirstLoadInFlightIsLoading() {
        let state = TransfersListState.decide(
            blobCount: 0, loadError: nil, loading: true, hasLoaded: false)
        XCTAssertEqual(state, .loading)
    }

    func testBeforeAnyLoadAttemptIsNotErroneous() {
        let state = TransfersListState.decide(
            blobCount: 0, loadError: nil, loading: false, hasLoaded: false)
        XCTAssertEqual(state, .empty,
                       "with no in-flight load and nothing loaded, settling on empty is fine")
    }

    // MARK: precedence — an error always wins over stale rows

    func testErrorWinsOverStaleRows() {
        let state = TransfersListState.decide(
            blobCount: 5,
            loadError: .message("Can't reach the control plane: timed out"),
            loading: false, hasLoaded: true)
        XCTAssertEqual(state, .error("Can't reach the control plane: timed out"))
    }

    func test404WinsOverStaleRows() {
        let state = TransfersListState.decide(
            blobCount: 5, loadError: .endpointMissing, loading: false, hasLoaded: true)
        XCTAssertEqual(state, .unavailable)
    }

    func testReloadingKeepsShowingListNotLoadingFlash() {
        let state = TransfersListState.decide(
            blobCount: 2, loadError: nil, loading: true, hasLoaded: true)
        XCTAssertEqual(state, .list(count: 2))
    }
}
