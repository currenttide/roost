import XCTest
@testable import RoostKit

/// R93: the Publish pane swallowed its site-list load failure entirely
/// (`sites = (try? await client.sites()) ?? sites`). Against a control plane
/// missing `/publish`, or on any transport failure, the list showed "No sites yet."
/// with ZERO feedback — the inverse of R81's double-stack. These tests pin the
/// decision (now in RoostKit, so the SwiftUI view is a dumb renderer) so a load
/// failure can never again masquerade as an empty list.
final class PublishListStateTests: XCTestCase {

    // MARK: error classification

    func testNotFoundClassifiesAsEndpointMissing() {
        // The missing-endpoint failure: GET /publish → 404 (sites() → .notFound).
        let err = PublishLoadError.from(RoostClientError.notFound("Not Found"))
        XCTAssertEqual(err, .endpointMissing)
    }

    func testServer404AlsoClassifiesAsEndpointMissing() {
        // Robustness: a 404 surfaced as .server(status: 404, …) is still missing.
        let err = PublishLoadError.from(RoostClientError.server(status: 404, message: ""))
        XCTAssertEqual(err, .endpointMissing)
    }

    func testUnauthorizedClassifiesAsTokenMessage() {
        let err = PublishLoadError.from(RoostClientError.unauthorized)
        XCTAssertEqual(
            err, .message("A client or admin token is required to list published sites."))
    }

    func testTransportErrorClassifiesAsItsDescription() {
        let err = PublishLoadError.from(RoostClientError.transport("connection refused"))
        guard case .message(let text) = err else {
            return XCTFail("expected .message, got \(err)")
        }
        XCTAssertTrue(text.contains("connection refused"), "kept the underlying reason: \(text)")
        XCTAssertNotEqual(err, .endpointMissing, "only a 404 is endpointMissing")
    }

    func testNon404ServerErrorClassifiesAsMessage() {
        let err = PublishLoadError.from(RoostClientError.server(status: 500, message: "boom"))
        guard case .message = err else { return XCTFail("expected .message, got \(err)") }
        XCTAssertNotEqual(err, .endpointMissing)
    }

    // MARK: the bug — a load failure must NOT vanish into the empty state

    /// The crux of R93: a 404 collapses to a single `.unavailable` state, NOT the
    /// `.empty` state (which would silently look like "no sites published").
    func test404YieldsUnavailable_notEmpty() {
        let state = PublishListState.decide(
            siteCount: 0, loadError: .endpointMissing, loading: false, hasLoaded: true)
        XCTAssertEqual(state, .unavailable)
        XCTAssertNotEqual(state, .empty, "a missing endpoint must not look like an empty list")
        if case .error = state { XCTFail("404 must not surface as a generic error") }
    }

    /// A transport failure (the live-CP render-proof case: pointing at a dead port)
    /// must surface as a retryable `.error`, NOT a silent empty list.
    func testTransportFailureYieldsError_notEmpty() {
        let classified = PublishLoadError.from(RoostClientError.transport("Could not connect to the server."))
        let state = PublishListState.decide(
            siteCount: 0, loadError: classified, loading: false, hasLoaded: true)
        guard case .error(let text) = state else {
            return XCTFail("a transport failure must surface as .error, got \(state)")
        }
        XCTAssertTrue(text.contains("Could not connect"), "kept the reason: \(text)")
        XCTAssertNotEqual(state, .empty)
    }

    func testReal404EndToEndYieldsUnavailable() {
        let classified = PublishLoadError.from(RoostClientError.notFound("Not Found"))
        let state = PublishListState.decide(
            siteCount: 0, loadError: classified, loading: false, hasLoaded: true)
        XCTAssertEqual(state, .unavailable)
    }

    // MARK: the other states stay correct

    func testGenuineErrorSurfacesAsError() {
        let state = PublishListState.decide(
            siteCount: 0,
            loadError: .message("Unauthorized — check the token"),
            loading: false, hasLoaded: true)
        XCTAssertEqual(state, .error("Unauthorized — check the token"))
    }

    func testLoadedWithZeroSitesIsEmpty() {
        let state = PublishListState.decide(
            siteCount: 0, loadError: nil, loading: false, hasLoaded: true)
        XCTAssertEqual(state, .empty)
    }

    func testLoadedWithRowsIsList() {
        let state = PublishListState.decide(
            siteCount: 2, loadError: nil, loading: false, hasLoaded: true)
        XCTAssertEqual(state, .list(count: 2))
    }

    func testFirstLoadInFlightIsLoading() {
        let state = PublishListState.decide(
            siteCount: 0, loadError: nil, loading: true, hasLoaded: false)
        XCTAssertEqual(state, .loading)
    }

    func testBeforeAnyLoadAttemptIsNotErroneous() {
        let state = PublishListState.decide(
            siteCount: 0, loadError: nil, loading: false, hasLoaded: false)
        XCTAssertEqual(state, .empty,
                       "with no in-flight load and nothing loaded, settling on empty is fine")
    }

    // MARK: precedence — an error always wins over stale rows

    func testErrorWinsOverStaleRows() {
        // A refresh that fails after a prior success: show the error, not stale rows.
        let state = PublishListState.decide(
            siteCount: 3,
            loadError: .message("Can't reach the control plane: timed out"),
            loading: false, hasLoaded: true)
        XCTAssertEqual(state, .error("Can't reach the control plane: timed out"))
    }

    func test404WinsOverStaleRows() {
        let state = PublishListState.decide(
            siteCount: 3, loadError: .endpointMissing, loading: false, hasLoaded: true)
        XCTAssertEqual(state, .unavailable)
    }

    func testReloadingKeepsShowingListNotLoadingFlash() {
        // A refresh over an existing list keeps the list visible (header spinner)
        // rather than blanking the pane to a spinner on every refresh.
        let state = PublishListState.decide(
            siteCount: 2, loadError: nil, loading: true, hasLoaded: true)
        XCTAssertEqual(state, .list(count: 2))
    }
}
