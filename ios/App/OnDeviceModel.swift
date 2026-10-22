import CryptoKit
import Foundation
#if canImport(WispLing)
import WispLing
#endif

/// The Ling weights on this iPhone: where they live, whether they're complete and verified,
/// and copying them over from the paired Mac.
@MainActor @Observable
final class OnDeviceModel {
    enum State: Equatable {
        case notInstalled
        case downloading(file: String, received: Int64, total: Int64)
        case verifying(file: String)
        case installed(id: String, bytes: Int64)
        case failed(String)
    }

    private(set) var state: State = .notInstalled
    private var downloadTask: Task<Void, Never>?

    /// MLX needs a real GPU; the iOS Simulator can't run Ling.
    nonisolated static var isSupportedHere: Bool {
        #if targetEnvironment(simulator)
        false
        #else
        canRunLing
        #endif
    }

    nonisolated static var canRunLing: Bool {
        #if canImport(WispLing)
        true
        #else
        false
        #endif
    }

    static let root: URL = {
        let base = (try? FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask,
                                                 appropriateFor: nil, create: true))
            ?? FileManager.default.temporaryDirectory
        return base.appendingPathComponent("Models", isDirectory: true)
    }()

    private static let receiptName = "wisp-verified.json"

    struct Receipt: Codable {
        var id: String
        var files: [String: String]
        var sizes: [String: Int64]
        var totalBytes: Int64
    }

    init() { refresh() }

    /// The installed model's directory, if a verified one exists.
    var installedDirectory: URL? {
        guard case .installed(let id, _) = state else { return nil }
        return Self.root.appendingPathComponent(id, isDirectory: true)
    }

    func refresh() {
        guard let entries = try? FileManager.default.contentsOfDirectory(at: Self.root, includingPropertiesForKeys: nil) else {
            state = .notInstalled
            return
        }
        for directory in entries {
            let receiptURL = directory.appendingPathComponent(Self.receiptName)
            // Every listed file must still be there at the size that was verified, so a re-download
            // that stopped half-way never passes for an installed model.
            guard let data = try? Data(contentsOf: receiptURL),
                  let receipt = try? JSONDecoder().decode(Receipt.self, from: data),
                  receipt.id == directory.lastPathComponent, ModelBundle.isSafeID(receipt.id),
                  receipt.files.keys.allSatisfy({ name in
                      ModelBundle.isSafeFileName(name)
                          && (try? directory.appendingPathComponent(name).resourceValues(forKeys: [.fileSizeKey]).fileSize)
                              .map(Int64.init) == receipt.sizes[name]
                  })
            else { continue }
            state = .installed(id: receipt.id, bytes: receipt.totalBytes)
            return
        }
        state = .notInstalled
    }

    func delete() {
        downloadTask?.cancel()
        try? FileManager.default.removeItem(at: Self.root)
        state = .notInstalled
    }

    func cancelDownload() {
        downloadTask?.cancel()
    }

    var isBusy: Bool {
        switch state {
        case .downloading, .verifying: true
        default: false
        }
    }

    /// Copy the Mac's Ling bundle to the phone, verifying every file's SHA-256 before it counts.
    func download(from gateway: GatewayClient) {
        guard !isBusy else { return }
        downloadTask = Task { [weak self] in
            await self?.runDownload(gateway: gateway)
        }
    }

    private func runDownload(gateway: GatewayClient) async {
        do {
            let bundles = try await gateway.models()
            let offered = bundles.filter(\.isSafe)
            guard let bundle = offered.first(where: { $0.modelType == "bailing_hybrid" }) ?? offered.first else {
                state = .failed(bundles.isEmpty
                    ? "Your Mac isn't offering a model. Start the gateway with --model <Ling folder>."
                    : "Your Mac offered a model with unsafe file names, so nothing was downloaded.")
                return
            }
            let free = (try? Self.root.deletingLastPathComponent()
                .resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey])
                .volumeAvailableCapacityForImportantUsage) ?? Int64.max
            // Sum the files rather than trusting the listing's total.
            // Saturating, so absurd listed sizes fail the space check instead of trapping.
            let totalBytes = bundle.files.reduce(Int64(0)) { sum, file in
                let (next, overflow) = sum.addingReportingOverflow(file.size)
                return overflow ? .max : next
            }
            guard totalBytes < .max - 1_000_000_000, free > totalBytes + 1_000_000_000 else {
                state = .failed("Not enough free space: Ling needs \(Self.format(totalBytes)) plus 1 GB.")
                return
            }
            try FileManager.default.createDirectory(at: Self.root, withIntermediateDirectories: true)
            let destination = Self.root.appendingPathComponent(bundle.id, isDirectory: true)
            guard ModelBundle.isDirectChild(destination, of: Self.root) else { throw DownloadError.unsafePath(bundle.id) }
            try FileManager.default.createDirectory(at: destination, withIntermediateDirectories: true)
            // Files are about to be replaced one by one; until all are verified this isn't an installed model.
            try? FileManager.default.removeItem(at: destination.appendingPathComponent(Self.receiptName))
            if case .installed(let id, _) = state, id == bundle.id { state = .notInstalled }
            var root = Self.root
            var values = URLResourceValues()
            values.isExcludedFromBackup = true
            try? root.setResourceValues(values)

            var hashes: [String: String] = [:]
            for file in bundle.files {
                try Task.checkCancellation()
                let target = destination.appendingPathComponent(file.name)
                guard ModelBundle.isDirectChild(target, of: destination) else { throw DownloadError.unsafePath(file.name) }
                if FileManager.default.fileExists(atPath: target.path),
                   (try? target.resourceValues(forKeys: [.fileSizeKey]).fileSize).map(Int64.init) == file.size {
                    state = .verifying(file: file.name)
                    if try await Self.sha256(of: target) == file.sha256 { hashes[file.name] = file.sha256; continue }
                }
                state = .downloading(file: file.name, received: 0, total: file.size)
                // Proves the Mac again before every attempt, so a retry never sends the token unproved.
                let temporary = try await VerifiedFileDownloader.download(
                    gateway: gateway, model: bundle.id, file: file.name, maxBytes: file.size
                ) { [weak self] received, _ in
                    Task { @MainActor in
                        self?.state = .downloading(file: file.name, received: received, total: file.size)
                    }
                }
                state = .verifying(file: file.name)
                let digest = try await Self.sha256(of: temporary)
                guard digest == file.sha256 else {
                    try? FileManager.default.removeItem(at: temporary)
                    throw DownloadError.checksum(file.name)
                }
                try? FileManager.default.removeItem(at: target)
                try FileManager.default.moveItem(at: temporary, to: target)
                hashes[file.name] = digest
            }
            #if canImport(WispLing)
            let missing = LingRuntime.requiredFiles(in: destination).filter { hashes[$0] == nil }
            guard missing.isEmpty else { throw DownloadError.incomplete(missing) }
            #endif
            let sizes = Dictionary(uniqueKeysWithValues: bundle.files.map { ($0.name, $0.size) })
            let receipt = Receipt(id: bundle.id, files: hashes, sizes: sizes, totalBytes: totalBytes)
            try JSONEncoder().encode(receipt).write(to: destination.appendingPathComponent(Self.receiptName), options: .atomic)
            state = .installed(id: bundle.id, bytes: totalBytes)
        } catch is CancellationError {
            refresh()
        } catch {
            state = .failed(error.localizedDescription)
        }
    }

    enum DownloadError: Error, LocalizedError {
        case checksum(String)
        case incomplete([String])
        case unsafePath(String)

        var errorDescription: String? {
            switch self {
            case .checksum(let name): "\(name) didn't match your Mac's copy. Try the download again."
            case .incomplete(let names): "Your Mac's model folder is missing \(names.joined(separator: ", "))."
            case .unsafePath(let name): "Refused to save \(name): it would land outside Wisp's model folder."
            }
        }
    }

    static func sha256(of url: URL) async throws -> String {
        try await Task.detached(priority: .utility) {
            let handle = try FileHandle(forReadingFrom: url)
            defer { try? handle.close() }
            var hasher = SHA256()
            while let chunk = try handle.read(upToCount: 8 << 20), !chunk.isEmpty {
                try Task.checkCancellation()
                hasher.update(data: chunk)
            }
            return hasher.finalize().map { String(format: "%02x", $0) }.joined()
        }.value
    }

    static func format(_ bytes: Int64) -> String {
        ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file)
    }
}
