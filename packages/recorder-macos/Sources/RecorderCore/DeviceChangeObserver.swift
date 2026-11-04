import AVFoundation
import CoreAudio
import Foundation

/// Calls `onChange` on the main queue when the default input or output device
/// changes, e.g. AirPods connecting mid-meeting. Changes arrive in bursts, so
/// they are coalesced over half a second.
public final class DeviceChangeObserver {
    private let onChange: () -> Void
    private var pending: DispatchWorkItem?
    private var listener: AudioObjectPropertyListenerBlock?
    private let selectors = [kAudioHardwarePropertyDefaultInputDevice, kAudioHardwarePropertyDefaultOutputDevice]

    public init(onChange: @escaping () -> Void) {
        self.onChange = onChange
        let block: AudioObjectPropertyListenerBlock = { [weak self] _, _ in self?.schedule() }
        listener = block
        for selector in selectors {
            var addr = address(selector)
            AudioObjectAddPropertyListenerBlock(.system, &addr, .main, block)
        }
    }

    deinit {
        guard let listener else { return }
        for selector in selectors {
            var addr = address(selector)
            AudioObjectRemovePropertyListenerBlock(.system, &addr, .main, listener)
        }
    }

    private func schedule() {
        pending?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.onChange() }
        pending = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5, execute: work)
    }
}

public enum MicrophonePermission {
    public static var isGranted: Bool { AVCaptureDevice.authorizationStatus(for: .audio) == .authorized }
    public static var isDenied: Bool {
        let status = AVCaptureDevice.authorizationStatus(for: .audio)
        return status == .denied || status == .restricted
    }

    public static func request() async -> Bool { await AVCaptureDevice.requestAccess(for: .audio) }
}
