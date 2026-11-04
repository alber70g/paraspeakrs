// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "recorder-macos",
    platforms: [.macOS(.v15)],
    targets: [
        .target(name: "RecorderCore"),
        // Run from a terminal, macOS attributes the system-audio request to the
        // terminal app, so the right channel stays silent unless that app has
        // the permission; the menu-bar app asks for it under its own name.
        .executableTarget(name: "recorder-spike", dependencies: ["RecorderCore"]),
        .executableTarget(
            name: "ParaspeakrsRecorder",
            dependencies: ["RecorderCore"],
            // Both go into the app bundle; `paraspeakrs install-recorder` copies them.
            exclude: ["Info.plist", "Sounds"]
        ),
    ],
    swiftLanguageModes: [.v5]
)
