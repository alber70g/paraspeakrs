import AppKit
import RecorderCore
import SwiftUI

/// The panel's permissions part: each grant the app needs, whether macOS has
/// given it, and a way to change it.
struct PermissionsSection: View {
    @State private var microphone = PermissionStatus.unknown
    @State private var systemAudio = PermissionStatus.unknown
    @State private var accessibility = PermissionStatus.unknown

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Permissions").font(.caption).foregroundStyle(.secondary)
            row("Microphone", "recording and dictation", microphone, settings: "Privacy_Microphone") {
                Button("Allow") { Task { _ = await MicrophonePermission.request() } }
            }
            row("System audio", "recording", systemAudio, settings: "Privacy_ScreenCapture") {
                Text("Asked at the first recording").font(.caption).foregroundStyle(.secondary)
            }
            row("Accessibility", "dictation", accessibility, settings: "Privacy_Accessibility") { EmptyView() }
        }
        // Grants change in System Settings; check again while the panel is open.
        .task {
            while !Task.isCancelled {
                refresh()
                try? await Task.sleep(for: .seconds(1))
            }
        }
    }

    private func refresh() {
        microphone = MicrophonePermission.isGranted ? .granted : MicrophonePermission.isDenied ? .denied : .notAsked
        systemAudio = SystemAudioPermission.status
        // macOS reports only trusted or not, so "not asked" shows as denied.
        accessibility = TextTyper.isTrusted ? .granted : .denied
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
