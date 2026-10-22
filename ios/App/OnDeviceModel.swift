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
            let totalBytes = bundle.files.reduce(Int64(0)) { $0 + $1.size }
            guard free > totalBytes + 1_000_000_000 else {
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
                let request = try await gateway.fileRequest(model: bundle.id, file: file.name)
                state = .downloading(file: file.name, received: 0, total: file.size)
                let temporary = try await FileDownloader.download(request, maxBytes: file.size) { [weak self] received, _ in
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
        case oversized

        var errorDescription: String? {
            switch self {
            case .oversized: "Your Mac sent more than the file's listed size, so the download was stopped."
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

/// One file download with progress and automatic resume (multi-GB files over Tailscale
/// can drop mid-way). Returns a temporary file the caller must move or delete.
final class FileDownloader: NSObject, URLSessionDownloadDelegate, @unchecked Sendable {
    typealias Progress = @Sendable (Int64, Int64) -> Void

    private let progress: Progress
    private let maxBytes: Int64
    private let lock = NSLock()
    private var oversized = false
    private var continuation: CheckedContinuation<URL, Error>?
    private var resumeData: Data?
    private var movedFile: URL?

    private init(maxBytes: Int64, progress: @escaping Progress) {
        self.maxBytes = maxBytes
        self.progress = progress
    }

    /// Stops as soon as more than `maxBytes` arrive, so a bad listing can't fill the disk.
    static func download(_ request: URLRequest, maxBytes: Int64, attempts: Int = 4,
                         progress: @escaping Progress) async throws -> URL {
        let downloader = FileDownloader(maxBytes: maxBytes, progress: progress)
        let session = URLSession(configuration: .default, delegate: downloader, delegateQueue: nil)
        defer { session.finishTasksAndInvalidate() }
        var lastError: Error = URLError(.unknown)
        for attempt in 0..<attempts {
            try Task.checkCancellation()
            if attempt > 0 { try await Task.sleep(for: .seconds(2 * attempt)) }
            do {
                return try await withTaskCancellationHandler {
                    try await withCheckedThrowingContinuation { continuation in
                        downloader.start(session: session, request: request, continuation: continuation)
                    }
                } onCancel: {
                    session.invalidateAndCancel()
                }
            } catch let error as URLError where error.code != .cancelled && error.code != .userAuthenticationRequired {
                lastError = error
                continue
            }
        }
        throw lastError
    }

    private func start(session: URLSession, request: URLRequest, continuation: CheckedContinuation<URL, Error>) {
        lock.lock()
        self.continuation = continuation
        let resume = resumeData
        resumeData = nil
        lock.unlock()
        let task = resume.map { session.downloadTask(withResumeData: $0) } ?? session.downloadTask(with: request)
        task.resume()
    }

    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didWriteData bytesWritten: Int64,
                    totalBytesWritten: Int64, totalBytesExpectedToWrite: Int64) {
        guard totalBytesWritten <= maxBytes else {
            lock.lock(); oversized = true; lock.unlock()
            downloadTask.cancel()
            return
        }
        progress(totalBytesWritten, totalBytesExpectedToWrite)
    }

    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didFinishDownloadingTo location: URL) {
        // The system deletes `location` when this returns, so move it now.
        guard let http = downloadTask.response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else { return }
        let kept = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        if (try? FileManager.default.moveItem(at: location, to: kept)) != nil {
            lock.lock(); movedFile = kept; lock.unlock()
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        lock.lock()
        let continuation = self.continuation
        self.continuation = nil
        let file = movedFile
        movedFile = nil
        let tooBig = oversized
        if let error = error as NSError?, !tooBig,
           let data = error.userInfo[NSURLSessionDownloadTaskResumeData] as? Data {
            resumeData = data
        }
        lock.unlock()
        if tooBig {
            if let file { try? FileManager.default.removeItem(at: file) }
            continuation?.resume(throwing: OnDeviceModel.DownloadError.oversized)
        } else if let error {
            continuation?.resume(throwing: error)
        } else if let file {
            continuation?.resume(returning: file)
        } else {
            let status = (task.response as? HTTPURLResponse)?.statusCode ?? 0
            continuation?.resume(throwing: status == 401
                                 ? GatewayError.unauthorized : GatewayError.http(status, "The Mac refused the model download."))
        }
    }
}
