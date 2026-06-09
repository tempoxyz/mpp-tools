// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "mpp-swift-conformance-adapter",
    platforms: [
        .macOS(.v13),
    ],
    products: [
        .executable(name: "swift-adapter", targets: ["SwiftAdapter"]),
    ],
    dependencies: [
        .package(url: "https://github.com/tempoxyz/mpp-swift.git", branch: "main"),
    ],
    targets: [
        .executableTarget(
            name: "SwiftAdapter",
            dependencies: [
                .product(name: "MPP", package: "mpp-swift"),
            ]
        ),
    ]
)
