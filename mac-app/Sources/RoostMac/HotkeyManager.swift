#if os(macOS)
import AppKit
import Carbon.HIToolbox

/// Global hotkey (default ⌥⌘R, DESIGN.md §4) via Carbon RegisterEventHotKey —
/// the one supported zero-dependency path for system-wide hotkeys that does
/// not require Accessibility permission.
@MainActor
final class HotkeyManager {
    private var hotKeyRef: EventHotKeyRef?
    private var handlerRef: EventHandlerRef?
    private let onPress: () -> Void

    init(onPress: @escaping () -> Void) {
        self.onPress = onPress
    }

    func enable() {
        guard hotKeyRef == nil else { return }

        var eventType = EventTypeSpec(
            eventClass: OSType(kEventClassKeyboard),
            eventKind: UInt32(kEventHotKeyPressed))
        let selfPtr = Unmanaged.passUnretained(self).toOpaque()
        InstallEventHandler(
            GetApplicationEventTarget(),
            { _, _, userData in
                guard let userData else { return noErr }
                let manager = Unmanaged<HotkeyManager>
                    .fromOpaque(userData).takeUnretainedValue()
                Task { @MainActor in manager.onPress() }
                return noErr
            },
            1, &eventType, selfPtr, &handlerRef)

        // ⌥⌘R — kVK_ANSI_R with option+command
        let hotKeyID = EventHotKeyID(signature: OSType(0x5253_544B) /* 'RSTK' */, id: 1)
        RegisterEventHotKey(
            UInt32(kVK_ANSI_R),
            UInt32(optionKey | cmdKey),
            hotKeyID,
            GetApplicationEventTarget(),
            0,
            &hotKeyRef)
    }

    func disable() {
        if let hotKeyRef {
            UnregisterEventHotKey(hotKeyRef)
            self.hotKeyRef = nil
        }
        if let handlerRef {
            RemoveEventHandler(handlerRef)
            self.handlerRef = nil
        }
    }
}
#endif
