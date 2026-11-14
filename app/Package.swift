// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "WispApp",
    platforms: [.macOS(.v14)],
    dependencies: [
        // In-app updates (SoftwareUpdate.swift). Exact pin; Package.resolved and
        // build-support/toolchain.json record the same release, and SwiftPM checks
        // the binary artifact's checksum from Sparkle's own manifest.
        .package(url: "https://github.com/sparkle-project/Sparkle", exact: "2.10.1"),
    ],
    targets: [
        .executableTarget(
            name: "WispApp",
            dependencies: [.product(name: "Sparkle", package: "Sparkle")],
            path: "Sources/WispApp",
            exclude: ["Resources"],
            // sqlite3: direct read of ~/Library/Messages/chat.db (Messages.app's
            // AppleScript API can't read message content, only chat names).
            // The rpath finds Sparkle.framework where the pipeline bundles it,
            // Contents/Frameworks.
            linkerSettings: [
                .linkedLibrary("sqlite3"),
                .unsafeFlags(["-Xlinker", "-rpath", "-Xlinker", "@executable_path/../Frameworks"]),
            ]
        )
    ],
    swiftLanguageModes: [.v5]
)
