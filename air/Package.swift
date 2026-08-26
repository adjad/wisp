// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "WispAirReader",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "WispAirReader",
            path: "Sources/WispAirReader",
            // sqlite3: direct read of ~/Library/Messages/chat.db (Messages.app's
            // AppleScript API can't read message content, only chat names).
            linkerSettings: [.linkedLibrary("sqlite3")]
        )
    ],
    swiftLanguageModes: [.v5]
)
