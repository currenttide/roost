// swift-tools-version:5.10
// Roost for Mac — see DESIGN.md. Two targets:
//   RoostKit  — UI-free API client + models (builds and tests on Linux too)
//   RoostMac  — the SwiftUI/AppKit menu bar app (macOS only)
import PackageDescription

let package = Package(
    name: "RoostMac",
    platforms: [.macOS(.v14)],
    products: [
        .library(name: "RoostKit", targets: ["RoostKit"]),
        .executable(name: "RoostMac", targets: ["RoostMac"]),
    ],
    dependencies: [
        // The one third-party dependency (DESIGN.md §3 carve-out): a faithful
        // terminal emulator for the Console. MIT, pure Swift, no transitive deps.
        .package(url: "https://github.com/migueldeicaza/SwiftTerm", from: "1.2.0"),
    ],
    targets: [
        .target(name: "RoostKit"),
        .executableTarget(
            name: "RoostMac",
            dependencies: [
                "RoostKit",
                .product(name: "SwiftTerm", package: "SwiftTerm",
                         condition: .when(platforms: [.macOS])),
            ]),
        .testTarget(name: "RoostKitTests", dependencies: ["RoostKit"]),
    ]
)
