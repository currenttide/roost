#if os(macOS)
import AppKit
import RoostKit
import SwiftUI

/// Owns the NSStatusItem and its popover. The icon IS the smallest unit of
/// UI (DESIGN.md §2.1): one bird glyph, three modifiers.
@MainActor
final class StatusItemController: NSObject, NSPopoverDelegate {
    enum IconState: Equatable {
        case idle          // template bird
        case working       // bird + activity dot
        case alert         // bird + orange badge
        case unreachable   // dimmed bird
    }

    private let model: AppModel
    private let statusItem: NSStatusItem
    private let popover: NSPopover
    private var lastIconState: IconState?

    /// Called when popover visibility changes, so the delegate can recompute
    /// store.uiVisible across popover + main window.
    var onVisibilityChange: (() -> Void)?

    var isPopoverShown: Bool { popover.isShown }

    init(model: AppModel) {
        self.model = model

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)

        popover = NSPopover()
        popover.behavior = .transient
        popover.animates = false

        super.init()
        popover.delegate = self

        let host = NSHostingController(
            rootView: PopoverRootView().environment(model))
        host.sizingOptions = [.preferredContentSize]
        popover.contentViewController = host

        if let button = statusItem.button {
            button.action = #selector(togglePopoverAction)
            button.target = self
            button.toolTip = "Roost"
        }

        // Redraw the icon whenever the verdict / activity / reachability change.
        continuouslyTrack { [weak self] in
            self?.refreshIcon()
        }
    }

    // MARK: icon

    private var iconState: IconState {
        let store = model.store
        switch store.reachability {
        case .unreachable, .never:
            return store.isConfigured ? .unreachable : .idle
        case .unauthorized:
            return .alert
        case .ok:
            if store.verdict?.level == .alert { return .alert }
            if store.hasActivity { return .working }
            return .idle
        }
    }

    private func refreshIcon() {
        let state = iconState
        statusItem.button?.toolTip = model.store.verdict?.summary ?? "Roost"
        guard state != lastIconState else { return }
        lastIconState = state
        statusItem.button?.image = Self.icon(for: state)
    }

    /// Drawn with a handler so NSColor.labelColor resolves against the menu
    /// bar's current appearance at draw time (light & dark both correct).
    static func icon(for state: IconState) -> NSImage {
        let size = NSSize(width: 18, height: 18)
        let image = NSImage(size: size, flipped: false) { rect in
            guard let bird = NSImage(
                systemSymbolName: "bird.fill", accessibilityDescription: "Roost")?
                .withSymbolConfiguration(.init(pointSize: 13, weight: .regular))
            else { return false }

            let birdRect = NSRect(
                x: (rect.width - bird.size.width) / 2,
                y: (rect.height - bird.size.height) / 2,
                width: bird.size.width, height: bird.size.height)
            bird.draw(in: birdRect)
            // tint the template drawing with the appearance-correct label color
            NSColor.labelColor
                .withAlphaComponent(state == .unreachable ? 0.4 : 1.0)
                .set()
            rect.fill(using: .sourceAtop)

            // badge dot
            let badgeColor: NSColor? = switch state {
            case .working: .systemBlue
            case .alert: .systemOrange
            case .idle, .unreachable: nil
            }
            if let badgeColor {
                let r: CGFloat = 3.5
                let dot = NSRect(x: rect.maxX - 2 * r, y: rect.minY,
                                 width: 2 * r, height: 2 * r)
                badgeColor.setFill()
                NSBezierPath(ovalIn: dot).fill()
            }
            return true
        }
        image.isTemplate = false
        image.accessibilityDescription = switch state {
        case .idle: "Roost: fleet OK"
        case .working: "Roost: fleet working"
        case .alert: "Roost: fleet needs attention"
        case .unreachable: "Roost: control plane unreachable"
        }
        return image
    }

    // MARK: popover

    @objc private func togglePopoverAction() {
        togglePopover()
    }

    func togglePopover() {
        if popover.isShown {
            popover.performClose(nil)
        } else {
            showPopover()
        }
    }

    func showPopover() {
        guard let button = statusItem.button else { return }
        NSApp.activate()
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        popover.contentViewController?.view.window?.makeKey()
        onVisibilityChange?()
    }

    nonisolated func popoverDidClose(_ notification: Notification) {
        Task { @MainActor in self.onVisibilityChange?() }
    }

    nonisolated func popoverDidShow(_ notification: Notification) {
        Task { @MainActor in self.onVisibilityChange?() }
    }
}
#endif
