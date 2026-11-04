import AppKit
import RecorderCore
import SwiftUI

/// The grants the app needs, as macOS reports them now. An `ObservableObject`
/// rather than `@State`: with the macOS 27.0 SDK `@State` is a macro whose plugin
/// the Command Line Tools do not ship, so `swift build` fails without Xcode.
@MainActor
final class Permissions: ObservableObject {
    @Published private(set) var microphone = PermissionStatus.unknown
    @Published private(set) var systemAudio = PermissionStatus.unknown
    @Published private(set) var accessibility = PermissionStatus.unknown

    func refresh() {
        microphone = MicrophonePermission.isGranted ? .granted : MicrophonePermission.isDenied ? .denied : .notAsked
        systemAudio = SystemAudioPermission.status
        // macOS reports only trusted or not, so "not asked" shows as denied.
        accessibility = TextTyper.isTrusted ? .granted : .denied
    }
}

/// The panel's permissions part: each grant the app needs, whether macOS has
/// given it, and a way to change it.
struct PermissionsSection: View {
    @StateObject private var permissions = Permissions()

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Permissions").font(.caption).foregroundStyle(.secondary)
            row("Microphone", "recording and dictation", permissions.microphone, settings: "Privacy_Microphone") {
                Button("Allow") { Task { _ = await MicrophonePermission.request() } }
            }
            row("System audio", "recording", permissions.systemAudio, settings: "Privacy_ScreenCapture") {
                Text("Asked at the first recording").font(.caption).foregroundStyle(.secondary)
            }
            row("Accessibility", "dictation and meeting shortcut", permissions.accessibility, settings: "Privacy_Accessibility") { EmptyView() }
        }
        // Grants change in System Settings; check again while the panel is open.
        .task {
            while !Task.isCancelled {
                permissions.refresh()
                try? await Task.sleep(for: .seconds(1))
            }
        }
    }

    private func row(_ name: String, _ usedFor: String, _ status: PermissionStatus, settings anchor: String,
                     @ViewBuilder notAsked: () -> some View) -> some View {
        HStack(alignment: .firstTextBaseline) {
            Image(systemName: Self.symbol(status)).foregroundStyle(Self.colour(status))
            VStack(alignment: .leading, spacing: 0) {
                Text(name)
                Text(usedFor).font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            switch status {
            case .granted:
                Text("Allowed").foregroundStyle(.secondary)
            case .notAsked:
                notAsked()
            case .denied, .unknown:
                // Unknown: the private TCC call is missing, so only Settings can tell.
                Button(status == .denied ? "Open Settings" : "Check in Settings") {
                    NSWorkspace.shared.open(URL(string:
                        "x-apple.systempreferences:com.apple.preference.security?\(anchor)")!)
                }
            }
        }
    }

    private static func symbol(_ status: PermissionStatus) -> String {
        switch status {
        case .granted: "checkmark.circle.fill"
        case .denied: "xmark.circle.fill"
        case .notAsked: "circle.dashed"
        case .unknown: "questionmark.circle"
        }
    }

    private static func colour(_ status: PermissionStatus) -> Color {
        switch status {
        case .granted: .green
        case .denied: .red
        case .notAsked, .unknown: .secondary
        }
    }
}
