// swift-tools-version: 6.0
import PackageDescription
let package = Package(name: "WispPhoneCore", platforms: [.iOS(.v17), .macOS(.v14)], products: [
    .library(name: "WispCore", targets: ["WispCore"]),
    .executable(name: "workflow20-ios", targets: ["WorkflowContractRunner"])
], targets: [
    .target(name: "WispCore"),
    .executableTarget(name: "WorkflowContractRunner", dependencies: ["WispCore"]),
    .testTarget(name: "WispCoreTests", dependencies: ["WispCore"])
])
