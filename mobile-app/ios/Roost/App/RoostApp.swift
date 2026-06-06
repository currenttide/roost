import SwiftUI

@main
struct RoostApp: App {
    @StateObject private var app = AppState()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(app)
                // Pairing deep link: the user scans the QR with the system
                // Camera app, which opens roost://pair?d=… into us.
                .onOpenURL { url in
                    app.pendingPairURL = url
                }
        }
    }
}

/// Routes between the pairing screen and the paired app. A pending pairing URL
/// is held on AppState so it survives the unpaired→pairing transition.
struct RootView: View {
    @EnvironmentObject var app: AppState

    var body: some View {
        Group {
            if !app.ready {
                ProgressView().controlSize(.large)
            } else if app.isPaired {
                DashboardView()
            } else {
                PairingView()
            }
        }
        .animation(.default, value: app.isPaired)
    }
}
