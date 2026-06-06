import UIKit

/// Thin haptics helper — a light tap on mic start/stop and confirm actions
/// (DESIGN §4 "haptic on start/stop"). UIKit feedback generators only.
enum Haptics {
    static func tap() {
        UIImpactFeedbackGenerator(style: .medium).impactOccurred()
    }

    static func success() {
        UINotificationFeedbackGenerator().notificationOccurred(.success)
    }
}
