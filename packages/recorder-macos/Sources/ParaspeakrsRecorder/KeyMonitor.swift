import AppKit
import ApplicationServices

/// Watches for a key combination without taking it: the frontmost app still
/// gets the key, so ⇧⌘M keeps muting Teams. `GlobalHotKey` takes the key
/// instead. NSEvent's global monitor sees keys only with Accessibility; the
/// local one covers keys sent to the recorder's own panel.
@MainActor
final class KeyMonitor {
    private var monitors: [Any] = []
    private var waitForTrust: Timer?

    init(_ shortcut: Shortcut, onPress: @escaping () -> Void) {
        let handle: (NSEvent) -> Void = { event in
            if !event.isARepeat, shortcut.matches(event) { onPress() }
        }
        let install = { [weak self] in
            guard let self else { return }
            if let global = NSEvent.addGlobalMonitorForEvents(matching: .keyDown, handler: handle) { monitors.append(global) }
            if let local = NSEvent.addLocalMonitorForEvents(matching: .keyDown, handler: { handle($0); return $0 }) {
                monitors.append(local)
            }
        }
        guard AXIsProcessTrusted() else {
            // A global monitor added before the grant may stay deaf after it, so install once granted.
            waitForTrust = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] timer in
                MainActor.assumeIsolated {
                    guard AXIsProcessTrusted() else { return }
                    timer.invalidate()
                    self?.waitForTrust = nil
                    install()
                }
            }
            return
        }
        install()
    }

    deinit {
        waitForTrust?.invalidate()
        monitors.forEach(NSEvent.removeMonitor)
    }
}
