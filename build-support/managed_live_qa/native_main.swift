import CryptoKit
import Darwin
import Foundation

private let qaPort = 18765
private let bundleID = "com.wisp.app.summary-qa"
private let reportKeys: Set<String> = [
    "schema_version", "manifest_sha256", "candidate_sha", "model", "status",
    "reason_codes", "call_count", "selected_ids", "predicates", "elapsed_ms",
]
private enum Refusal: Error { case blocked }

private func refuse(_ code: Int32 = 78) -> Never {
    fputs("Wisp Summary QA blocked; no private value was logged.\n", stderr)
    exit(code)
}

private func digest(_ url: URL) throws -> String {
    let handle = try FileHandle(forReadingFrom: url)
    defer { try? handle.close() }
    var hash = SHA256()
    while true {
        let data = try handle.read(upToCount: 1_048_576) ?? Data()
        if data.isEmpty { break }
        hash.update(data: data)
    }
    return hash.finalize().map { String(format: "%02x", $0) }.joined()
}

private func keyData(_ value: String) throws -> Data {
    guard value.utf8.count == 64 else { throw Refusal.blocked }
    var result = Data()
    var index = value.startIndex
    for _ in 0..<32 {
        let next = value.index(index, offsetBy: 2)
        guard let byte = UInt8(value[index..<next], radix: 16) else { throw Refusal.blocked }
        result.append(byte)
        index = next
    }
    return result
}

private func capability(_ key: String, _ candidate: String, _ label: String) throws -> String {
    let secret = SymmetricKey(data: try keyData(key))
    let data = Data("wisp-summary-qa:\(label):\(candidate)".utf8)
    return HMAC<SHA256>.authenticationCode(for: data, using: secret)
        .map { String(format: "%02x", $0) }.joined()
}

private func buildManifest(_ stage: URL) throws -> [String: Any] {
    let data = try Data(contentsOf: stage.appendingPathComponent("qa-build-manifest.json"))
    guard data.count <= 1_000_000,
          let value = try JSONSerialization.jsonObject(with: data) as? [String: Any],
          value["artifact_kind"] as? String == "wisp-managed-summary-qa-v1",
          value["bundle_id"] as? String == bundleID,
          value["port"] as? Int == qaPort,
          let candidate = value["candidate_sha"] as? String,
          candidate.range(of: "^[0-9a-f]{40}$", options: .regularExpression) != nil,
          let python = value["python_executable"] as? String,
          python.hasPrefix("/"),
          let expected = value["python_sha256"] as? String,
          try digest(URL(fileURLWithPath: python)) == expected else { throw Refusal.blocked }
    return value
}

private func tool(_ path: String, _ arguments: [String]) throws -> (Int32, Data) {
    let process = Process()
    let pipe = Pipe()
    process.executableURL = URL(fileURLWithPath: path)
    process.arguments = arguments
    process.environment = ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"]
    process.standardOutput = pipe
    process.standardError = FileHandle.nullDevice
    try process.run()
    process.waitUntilExit()
    let data = try pipe.fileHandleForReading.readToEnd() ?? Data()
    guard data.count <= 65_536 else { throw Refusal.blocked }
    return (process.terminationStatus, data)
}

private func listeners() throws -> Set<pid_t> {
    let result = try tool("/usr/sbin/lsof",
        ["-nP", "-a", "-iTCP:\(qaPort)", "-sTCP:LISTEN", "-t"])
    if result.0 == 1 && result.1.isEmpty { return [] }
    guard result.0 == 0, let text = String(data: result.1, encoding: .utf8) else {
        throw Refusal.blocked
    }
    let rows = text.split(whereSeparator: \.isNewline)
    let values = rows.compactMap { Int32(String($0)) }
    guard values.count == rows.count else { throw Refusal.blocked }
    return Set(values)
}

private func awaitListener(_ process: Process) throws {
    let deadline = Date().addingTimeInterval(20)
    while Date() < deadline {
        guard process.isRunning else { throw Refusal.blocked }
        if try listeners() == Set([process.processIdentifier]) { return }
        usleep(100_000)
    }
    throw Refusal.blocked
}

private func request(_ method: String, _ path: String, _ headers: [String: String],
                     _ body: Data? = nil) throws -> [String: Any] {
    guard let url = URL(string: "http://127.0.0.1:\(qaPort)\(path)") else {
        throw Refusal.blocked
    }
    var value = URLRequest(url: url)
    value.httpMethod = method
    value.timeoutInterval = 45
    value.httpBody = body
    for (name, header) in headers { value.setValue(header, forHTTPHeaderField: name) }
    let semaphore = DispatchSemaphore(value: 0)
    var data: Data?
    var code: Int?
    URLSession.shared.dataTask(with: value) { result, response, _ in
        data = result
        code = (response as? HTTPURLResponse)?.statusCode
        semaphore.signal()
    }.resume()
    guard semaphore.wait(timeout: .now() + 50) == .success,
          code == 200,
          let data,
          data.count <= 32_768,
          let object = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
        throw Refusal.blocked
    }
    return object
}

private func writeAll(_ fd: Int32, _ data: Data) throws {
    try data.withUnsafeBytes { raw in
        guard let base = raw.baseAddress else { return }
        var offset = 0
        while offset < data.count {
            let count = Darwin.write(fd, base.advanced(by: offset), data.count - offset)
            guard count > 0 else { throw Refusal.blocked }
            offset += count
        }
    }
}

private func save(_ report: [String: Any], _ candidate: String) throws -> URL {
    guard Set(report.keys) == reportKeys,
          report["candidate_sha"] as? String == candidate,
          let status = report["status"] as? String,
          ["PASS", "FAIL", "BLOCK"].contains(status) else { throw Refusal.blocked }
    let data = try JSONSerialization.data(withJSONObject: report, options: [.sortedKeys])
    guard data.count <= 32_768 else { throw Refusal.blocked }
    let directory = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/Wisp Summary QA/reports")
    try FileManager.default.createDirectory(
        at: directory, withIntermediateDirectories: true,
        attributes: [.posixPermissions: 0o700])
    var info = stat()
    guard lstat(directory.path, &info) == 0,
          (info.st_mode & S_IFMT) == S_IFDIR,
          info.st_uid == getuid(),
          info.st_mode & 0o077 == 0 else { throw Refusal.blocked }
    let final = directory.appendingPathComponent(
        "\(candidate)-\(Int(Date().timeIntervalSince1970)).json")
    let temporary = directory.appendingPathComponent(".report-\(UUID().uuidString)")
    let fd = open(temporary.path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
    guard fd >= 0 else { throw Refusal.blocked }
    var committed = false
    defer {
        close(fd)
        if !committed { unlink(temporary.path) }
    }
    try writeAll(fd, data)
    guard fsync(fd) == 0, rename(temporary.path, final.path) == 0 else {
        throw Refusal.blocked
    }
    committed = true
    return final
}

@main
private enum SummaryQAMain {
static func main() {
guard CommandLine.arguments.count == 1 else { refuse(64) }
do {
    guard Bundle.main.bundleIdentifier == bundleID,
          let resources = Bundle.main.resourceURL else { throw Refusal.blocked }
    let stage = resources.appendingPathComponent("qa")
    let manifest = try buildManifest(stage)
    guard let candidate = manifest["candidate_sha"] as? String,
          let python = manifest["python_executable"] as? String,
          try listeners().isEmpty else { throw Refusal.blocked }
    let snapshot = try BackendCredentials.loadForBackend()
    guard let key = snapshot.credentials["WISP_LOCAL_OMLX_KEY"],
          snapshot.credentials.count == 1 else { throw Refusal.blocked }
    let runKey = try capability(key, candidate, "run")
    let readyKey = try capability(key, candidate, "ready")
    let pipe = Pipe()
    let process = Process()
    process.executableURL = URL(fileURLWithPath: python)
    process.arguments = ["-B", "-m", "service.main"]
    process.currentDirectoryURL = stage
    var environment = [
        "HOME": FileManager.default.homeDirectoryForCurrentUser.path,
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": stage.path,
        "WISP_QA_CANDIDATE_SHA": candidate,
    ]
    environment["WISP_CREDENTIAL_PIPE"] = try BackendCredentials.pipeMetadata(pipe)
    process.environment = environment
    process.standardInput = pipe
    process.standardOutput = FileHandle.nullDevice
    process.standardError = FileHandle.nullDevice
    try process.run()
    try pipe.fileHandleForReading.close()
    do {
        try BackendCredentials.writePipe(
            pipe, credentials: snapshot.credentials, generation: snapshot.generation,
            role: "primary", pid: process.processIdentifier)
    } catch {
        if process.isRunning { process.terminate() }
        throw Refusal.blocked
    }
    defer {
        if process.isRunning {
            process.terminate()
            for _ in 0..<30 where process.isRunning { usleep(100_000) }
            if process.isRunning { kill(process.processIdentifier, SIGKILL) }
        }
        process.waitUntilExit()
    }
    try awaitListener(process)
    _ = try request("GET", "/qa/ready", ["X-Wisp-QA-Challenge": readyKey])
    let body = try JSONSerialization.data(withJSONObject: [
        "manifest_id": "pr50-summary-selection-v1", "capability": runKey])
    let report = try request(
        "POST", "/qa/run", ["Content-Type": "application/json"], body)
    print("Sanitized QA report written to \(try save(report, candidate).path)")
} catch {
    refuse()
}
}
}
