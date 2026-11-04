// Milestone 0 spike: record N seconds of mic + system audio, then merge them
// into a stereo .m4a (L = mic, R = system).
//
//   recorder-spike --seconds 10 out/meeting      # writes out/meeting.m4a
//   recorder-spike --merge mic.caf system.caf out.m4a
//
// `--merge` also recovers a recording the app could not finish: it leaves
// `<name>.mic.caf` and `<name>.system.caf` behind.
import Foundation
import RecorderCore

if CommandLine.arguments.count == 5, CommandLine.arguments[1] == "--merge" {
    let paths = CommandLine.arguments[2...].map { URL(fileURLWithPath: $0) }
    try StereoMerge.merge(left: paths[0], right: paths[1], to: paths[2])
    print("wrote \(paths[2].path)")
    exit(0)
}

var seconds = 10.0
var rebuildAt: Double?
var output: String?
var arguments = CommandLine.arguments.dropFirst()
while let argument = arguments.popFirst() {
    if argument == "--seconds", let value = arguments.popFirst().flatMap(Double.init) {
        seconds = value
    } else if argument == "--rebuild-at", let value = arguments.popFirst().flatMap(Double.init) {
        rebuildAt = value
    } else {
        output = argument
    }
}
guard let output else {
    FileHandle.standardError.write(Data("usage: recorder-spike [--seconds N] [--rebuild-at S] out/base\n".utf8))
    exit(2)
}

let granted = await MicrophonePermission.request()
print("microphone permission: \(granted ? "granted" : "denied")")

let base = URL(fileURLWithPath: output)
let capture = MeetingCapture(base: base)
do {
    try capture.start()
} catch {
    FileHandle.standardError.write(Data("start failed: \(error)\n".utf8))
    exit(1)
}
print("mic \(capture.micRate) Hz, system \(capture.systemRate) Hz")

var micMax: Float = 0
var systemMax: Float = 0
let started = Date()
while Date().timeIntervalSince(started) < seconds {
    try await Task.sleep(for: .milliseconds(100))
    if let at = rebuildAt, Date().timeIntervalSince(started) >= at {
        rebuildAt = nil
        try capture.rebuild()
        print(String(format: "rebuilt both devices at %.2f s", Date().timeIntervalSince(started)))
    }
    micMax = max(micMax, capture.levels.mic)
    systemMax = max(systemMax, capture.levels.system)
}
try capture.stop()
print(String(format: "peak mic %.4f, peak system %.4f, dropped buffers %d", micMax, systemMax, capture.droppedBuffers))

let m4a = base.appendingPathExtension("m4a")
try StereoMerge.merge(left: capture.micURL, right: capture.systemURL, to: m4a)
print("wrote \(m4a.path)")
