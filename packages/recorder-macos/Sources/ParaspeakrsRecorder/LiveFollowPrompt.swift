import AppKit
import SwiftUI

/// "Follow this meeting live?" in a small panel under the menu-bar icon. A panel
/// of our own: SwiftUI has no API to open the MenuBarExtra window from code. It
/// floats over full-screen meeting apps and does not take focus from them.
@MainActor
final class LiveFollowPrompt {
    private var panel: NSPanel?

    func show(open: @escaping () -> Void, dismiss: @escaping () -> Void) {
        close()
        let content = FirstClickHostingView(rootView: PromptView(open: open, dismiss: dismiss))
        let panel = PromptPanel(
            contentRect: NSRect(origin: .zero, size: content.fittingSize),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.contentView = content
        panel.level = .statusBar
        panel.isFloatingPanel = true
        panel.hidesOnDeactivate = false
        panel.becomesKeyOnlyIfNeeded = true
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.setFrameTopLeftPoint(Self.topLeft(for: content.fittingSize))
        panel.orderFrontRegardless()
        self.panel = panel
    }

    func close() {
        panel?.orderOut(nil)
        panel = nil
    }

    /// Centred under the menu-bar icon, kept on screen; top right of the screen
    /// when the icon is not found.
    private static func topLeft(for size: NSSize) -> NSPoint {
        let margin = 6.0
        if let icon = iconWindow(), let screen = icon.screen {
            let area = screen.visibleFrame
            let x = min(max(icon.frame.midX - size.width / 2, area.minX + margin), area.maxX - size.width - margin)
            return NSPoint(x: x, y: icon.frame.minY - margin)
        }
        let area = (NSScreen.main ?? NSScreen.screens[0]).visibleFrame
        return NSPoint(x: area.maxX - size.width - margin, y: area.maxY - margin)
    }

    /// The status item is one of the app's own windows: a small one at the top edge of a screen.
    private static func iconWindow() -> NSWindow? {
        NSApp.windows.first { window in
            guard window.isVisible, let screen = window.screen else { return false }
            return window.frame.maxY >= screen.frame.maxY - 1 && window.frame.height < 50 && window.frame.width < 200
        }
    }
}

/// Clicks reach the buttons without making the panel key first.
private final class PromptPanel: NSPanel {
    override var canBecomeKey: Bool { true }
}

private final class FirstClickHostingView<Content: View>: NSHostingView<Content> {
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
}

private struct PromptView: View {
    let open: () -> Void
    let dismiss: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Follow this meeting live?").font(.headline)
            Text("Opens the live meeting page in your browser.").font(.caption).foregroundStyle(.secondary)
            HStack {
                Spacer()
                Button("Dismiss", action: dismiss)
                Button("Open", action: open).buttonStyle(.borderedProminent)
            }
        }
        .padding(12)
        .frame(width: 260)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 10))
    }
}
