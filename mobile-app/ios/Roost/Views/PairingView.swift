import SwiftUI

/// Pairing screen (API.md §1). Two paths: a `roost://pair` deep link from the
/// system Camera scanning the QR, or a manual paste of the code. No in-app
/// scanner (DESIGN: thin client, OS does the camera).
struct PairingView: View {
    @EnvironmentObject var app: AppState
    @StateObject private var store = PairingStore()

    var body: some View {
        NavigationStack {
            VStack(spacing: 24) {
                Spacer()
                Image(systemName: "qrcode.viewfinder")
                    .font(.system(size: 64))
                    .foregroundStyle(.tint)
                Text("Pair with your fleet")
                    .font(.title2.bold())
                Text("Run `roost pair` on the control-plane host, then scan the QR with your Camera app — or paste the code below.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal)

                VStack(spacing: 12) {
                    TextField("roost://pair?d=… or the raw code", text: $store.pasteText,
                              axis: .vertical)
                        .textFieldStyle(.roundedBorder)
                        .autocorrectionDisabled()
                        .textInputAutocapitalization(.never)
                        .lineLimit(1...4)
                    Button {
                        Task { await store.pairFromPaste(into: app) }
                    } label: {
                        if store.busy {
                            ProgressView()
                        } else {
                            Text("Pair").frame(maxWidth: .infinity)
                        }
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(store.busy || store.pasteText.isEmpty)
                }
                .padding(.horizontal)

                if let error = store.error {
                    Text(error)
                        .font(.footnote)
                        .foregroundStyle(.red)
                        .multilineTextAlignment(.center)
                        .padding(.horizontal)
                }
                Spacer()
            }
            .navigationTitle("Roost")
            .navigationBarTitleDisplayMode(.inline)
        }
        // Consume a deep link captured before this view appeared.
        // ORDER MATTERS: this task is keyed on pendingPairURL, so clearing it
        // BEFORE the await would change our own id and SwiftUI would cancel
        // the in-flight pairing probe ("transport(cancelled)"). Clear it only
        // after the attempt finishes; the id-change restart then no-ops.
        .task(id: app.pendingPairURL) {
            guard let url = app.pendingPairURL else { return }
            await store.pair(url: url, into: app)
            app.pendingPairURL = nil
        }
    }
}
