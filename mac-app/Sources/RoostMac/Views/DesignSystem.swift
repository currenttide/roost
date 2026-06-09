#if os(macOS)
import SwiftUI

/// The shared visual language (redesign §Visual language). A small, calm set of
/// tokens and components so every surface feels like one app: three type sizes,
/// status conveyed by a single dot/pill (never by filling rows), system
/// materials, generous spacing.
enum Theme {
    static let cardCorner: CGFloat = 10
    static let cardPadding: CGFloat = 12
    static let rowSpacing: CGFloat = 10
    static let sectionSpacing: CGFloat = 18
}

/// A calm section header: a quiet uppercase label with breathing room.
struct SectionLabel: View {
    let title: String
    var trailing: String?

    init(_ title: String, trailing: String? = nil) {
        self.title = title
        self.trailing = trailing
    }

    var body: some View {
        HStack {
            Text(title.uppercased())
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
                .tracking(0.5)
            Spacer()
            if let trailing {
                Text(trailing).font(.caption).foregroundStyle(.tertiary)
            }
        }
    }
}

/// A small capsule used only for the one attention callout a row may carry —
/// never to fill the UI.
struct StatusPill: View {
    let text: String
    var color: Color = .orange

    var body: some View {
        Text(text)
            .font(.caption2.weight(.medium))
            .padding(.horizontal, 7)
            .padding(.vertical, 2)
            .background(color.opacity(0.15), in: Capsule())
            .foregroundStyle(color)
    }
}

/// A rounded, materially-backed card with a hairline border — the standard
/// surface for runs, outcomes, and grouped content.
struct Card<Content: View>: View {
    var selected = false
    @ViewBuilder var content: Content

    var body: some View {
        content
            .padding(Theme.cardPadding)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                selected ? AnyShapeStyle(.tint.opacity(0.12))
                         : AnyShapeStyle(.quaternary.opacity(0.4)),
                in: RoundedRectangle(cornerRadius: Theme.cardCorner))
            .overlay(
                RoundedRectangle(cornerRadius: Theme.cardCorner)
                    .strokeBorder(selected ? AnyShapeStyle(.tint.opacity(0.5))
                                            : AnyShapeStyle(.quaternary),
                                  lineWidth: 1))
    }
}
#endif
