import CryptoKit
import Darwin
import Foundation
import Security

private let bundleID = "com.wisp.app.summary-qa"
private let hex40 = "^[0-9a-f]{40}$"
private let hex64 = "^[0-9a-f]{64}$"
private let reviewedRuntimeInventorySHA256 = "__RUNTIME_INVENTORY_SHA256__"
private let reportKeys: Set<String> = [
    "schema_version", "manifest_sha256", "production_sha", "artifact_sha",
    "build_manifest_sha256", "source_inventory_sha256", "runtime_inventory_sha256",
    "native_sha256", "launch_nonce", "child_pid", "ipc_authenticated", "model",
    "status", "reason_codes", "call_count", "selected_ids", "predicates",
    "readiness", "elapsed_ms",
]
private let predicateKeys: Set<String> = [
    "schemas_valid", "daily_model_free", "daily_ready", "email_selected",
    "messages_selected", "source_hashes_valid",
]
private let readinessKeys: Set<String> = ["exclusive_session", "model_loaded", "server_idle"]
private let reasonCodes: Set<String> = [
    "invalid_capability", "readiness_unproven", "bounded_run_failed",
    "unknown_fixture_id", "predicate_failed", "external_exclusivity_required",
    "integrity_unproven",
]
private enum Refusal: Error { case blocked }

private func refuse(_ code: Int32 = 78) -> Never {
    fputs("Wisp Summary QA blocked; no private value was logged.\n", stderr)
    exit(code)
}

private func matches(_ value: Any?, _ expression: String) -> Bool {
    guard let text = value as? String else { return false }
    return text.range(of: expression, options: .regularExpression) != nil
}

private func exactInt(_ value: Any?) -> Int? {
    guard let number = value as? NSNumber,
          CFGetTypeID(number) != CFBooleanGetTypeID() else { return nil }
    let integer = number.intValue
    return number.doubleValue == Double(integer) ? integer : nil
}

private func exactBool(_ value: Any?) -> Bool? {
    guard let number = value as? NSNumber,
          CFGetTypeID(number) == CFBooleanGetTypeID() else { return nil }
    return number.boolValue
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

private func little32(_ data: Data, _ offset: Int) throws -> UInt32 {
    guard offset >= 0, offset + 4 <= data.count else { throw Refusal.blocked }
    return data[offset..<offset + 4].enumerated().reduce(UInt32(0)) {
        $0 | (UInt32($1.element) << UInt32($1.offset * 8))
    }
}

private func canonicalNativeDigest(_ url: URL) throws -> String {
    var data = try Data(contentsOf: url, options: [.mappedIfSafe])
    guard data.count >= 32, try little32(data, 0) == 0xfeedfacf else {
        throw Refusal.blocked
    }
    let count = Int(try little32(data, 16))
    var offset = 32
    var signature: (Int, Int)?
    for _ in 0..<count {
        let command = try little32(data, offset)
        let size = Int(try little32(data, offset + 4))
        guard size >= 8, offset + size <= data.count else { throw Refusal.blocked }
        if command == 0x1d {
            guard size >= 16, signature == nil else { throw Refusal.blocked }
            let start = Int(try little32(data, offset + 8))
            let length = Int(try little32(data, offset + 12))
            guard start >= 0, length >= 0, start + length <= data.count else {
                throw Refusal.blocked
            }
            signature = (start, length)
            data.replaceSubrange(offset + 8..<offset + 16, with: repeatElement(UInt8(0), count: 8))
        }
        offset += size
    }
    guard let (start, length) = signature else { throw Refusal.blocked }
    var hash = SHA256()
    hash.update(data: data[..<start])
    hash.update(data: data[(start + length)...])
    return hash.finalize().map { String(format: "%02x", $0) }.joined()
}

private func strictBundleValidation() throws {
    var code: SecStaticCode?
    let flags = SecCSFlags(rawValue: kSecCSStrictValidate | kSecCSCheckAllArchitectures)
    guard SecStaticCodeCreateWithPath(Bundle.main.bundleURL as CFURL, [], &code) == errSecSuccess,
          let code,
          SecStaticCodeCheckValidity(code, flags, nil) == errSecSuccess else {
        throw Refusal.blocked
    }
}

private func json(_ url: URL, limit: Int = 1_000_000) throws -> ([String: Any], Data) {
    let data = try Data(contentsOf: url)
    guard !data.isEmpty, data.count <= limit,
          let value = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
        throw Refusal.blocked
    }
    return (value, data)
}

private struct Evidence {
    let stage: URL
    let build: [String: Any]
    let attestation: [String: Any]
    let buildDigest: String
    let attestationDigest: String
    let pythonDigest: String
}

private func evidence(_ stage: URL) throws -> Evidence {
    let buildURL = stage.appendingPathComponent("qa-build-manifest.json")
    let attestationURL = stage.appendingPathComponent("qa-attestation.json")
    let (build, _) = try json(buildURL)
    let (attestation, _) = try json(attestationURL)
    let buildKeys: Set<String> = ["schema_version", "artifact_kind", "bundle_id",
        "artifact_sha", "production_sha", "ipc_protocol", "inference_endpoint",
        "exclusive_proof_protocol", "python_relative", "manifest_sha256",
        "source_inventory_sha256", "runtime_inventory_sha256"]
    let attestKeys: Set<String> = ["schema_version", "artifact_sha", "production_sha",
        "build_manifest_sha256", "manifest_sha256", "source_inventory_sha256",
        "runtime_inventory_sha256", "native_sha256"]
    guard Set(build.keys) == buildKeys, Set(attestation.keys) == attestKeys,
          exactInt(build["schema_version"]) == 2,
          build["artifact_kind"] as? String == "wisp-managed-summary-qa-v1",
          build["bundle_id"] as? String == bundleID,
          build["ipc_protocol"] as? String == "anonymous-pipes-v1",
          build["inference_endpoint"] as? String == "http://127.0.0.1:8000",
          ["unavailable", "server-lease-v1"].contains(
            build["exclusive_proof_protocol"] as? String ?? ""),
          build["python_relative"] as? String == "runtime/bin/python3",
          matches(build["artifact_sha"], hex40), matches(build["production_sha"], hex40),
          exactInt(attestation["schema_version"]) == 1,
          attestation["artifact_sha"] as? String == build["artifact_sha"] as? String,
          attestation["production_sha"] as? String == build["production_sha"] as? String else {
        throw Refusal.blocked
    }
    let buildDigest = try digest(buildURL)
    guard attestation["build_manifest_sha256"] as? String == buildDigest else {
        throw Refusal.blocked
    }
    for key in ["manifest_sha256", "source_inventory_sha256", "runtime_inventory_sha256"] {
        guard matches(build[key], hex64), build[key] as? String == attestation[key] as? String else {
            throw Refusal.blocked
        }
    }
    guard matches(attestation["native_sha256"], hex64),
          let executable = Bundle.main.executableURL,
          try canonicalNativeDigest(executable) == attestation["native_sha256"] as? String,
          try digest(stage.appendingPathComponent("source/service/qa-manifest.json"))
            == build["manifest_sha256"] as? String,
          reviewedRuntimeInventorySHA256 == build["runtime_inventory_sha256"] as? String,
          reviewedRuntimeInventorySHA256 == attestation["runtime_inventory_sha256"] as? String,
          let sourceDigest = build["source_inventory_sha256"] as? String,
          let runtimeDigest = build["runtime_inventory_sha256"] as? String else {
        throw Refusal.blocked
    }
    let pythonDigest = try verifyNativeInventories(stage: stage,
        expectedSourceDigest: sourceDigest, expectedRuntimeDigest: runtimeDigest)
    return Evidence(stage: stage, build: build, attestation: attestation,
                    buildDigest: buildDigest, attestationDigest: try digest(attestationURL),
                    pythonDigest: pythonDigest)
}

private func randomHex() throws -> String {
    var bytes = [UInt8](repeating: 0, count: 32)
    guard SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) == errSecSuccess else {
        throw Refusal.blocked
    }
    return bytes.map { String(format: "%02x", $0) }.joined()
}

private func dataKey(_ hex: String) throws -> Data {
    guard hex.range(of: hex64, options: .regularExpression) != nil else {
        throw Refusal.blocked
    }
    var bytes = Data()
    var index = hex.startIndex
    for _ in 0..<32 {
        let next = hex.index(index, offsetBy: 2)
        guard let byte = UInt8(hex[index..<next], radix: 16) else { throw Refusal.blocked }
        bytes.append(byte)
        index = next
    }
    return bytes
}

private func sessionKey(_ credential: String, nonce: String, evidence: Evidence,
                        parent: pid_t, child: pid_t) throws -> SymmetricKey {
    let object: [String: Any] = ["protocol": "anonymous-pipes-v1", "nonce": nonce,
        "artifact_sha": evidence.build["artifact_sha"]!,
        "production_sha": evidence.build["production_sha"]!,
        "parent_pid": parent, "child_pid": child,
        "attestation_sha256": evidence.attestationDigest]
    let body = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
    return SymmetricKey(data: Data(HMAC<SHA256>.authenticationCode(
        for: body, using: SymmetricKey(data: try dataKey(credential)))))
}

private func readBounded(_ fd: Int32, timeout: TimeInterval, limit: Int) throws -> Data {
    let deadline = Date().addingTimeInterval(timeout)
    var result = Data()
    var buffer = [UInt8](repeating: 0, count: 4096)
    while true {
        let remaining = deadline.timeIntervalSinceNow
        guard remaining > 0 else { throw Refusal.blocked }
        var item = pollfd(fd: fd, events: Int16(POLLIN | POLLHUP), revents: 0)
        let waited = poll(&item, 1, Int32(min(remaining * 1000, 1000)))
        if waited < 0 && errno == EINTR { continue }
        guard waited >= 0 else { throw Refusal.blocked }
        if waited == 0 { continue }
        let count = Darwin.read(fd, &buffer, buffer.count)
        if count == 0 { return result }
        guard count > 0, result.count + count <= limit else { throw Refusal.blocked }
        result.append(buffer, count: count)
    }
}

private func authenticatedReport(_ frame: Data, key: SymmetricKey) throws -> [String: Any] {
    let magic = Data("WQARPT1\n".utf8)
    guard frame.count >= 44, frame.count <= 32_812, frame.prefix(8) == magic else {
        throw Refusal.blocked
    }
    let size = Int(frame[8..<12].reduce(UInt32(0)) { ($0 << 8) | UInt32($1) })
    guard size <= 32_768, frame.count == 12 + size + 32 else { throw Refusal.blocked }
    let body = frame[12..<12 + size]
    let supplied = frame[(12 + size)...]
    guard HMAC<SHA256>.isValidAuthenticationCode(supplied, authenticating: body, using: key),
          let value = try JSONSerialization.jsonObject(with: body) as? [String: Any] else {
        throw Refusal.blocked
    }
    return value
}

private func booleans(_ value: Any?, keys: Set<String>) -> [String: Bool]? {
    guard let object = value as? [String: Any], Set(object.keys) == keys else { return nil }
    var result: [String: Bool] = [:]
    for key in keys {
        guard let value = exactBool(object[key]) else { return nil }
        result[key] = value
    }
    return result
}

private func validateReport(_ report: [String: Any], evidence: Evidence,
                            nonce: String, child: pid_t, authenticated: Bool) throws {
    guard Set(report.keys) == reportKeys, exactInt(report["schema_version"]) == 2,
          report["manifest_sha256"] as? String == evidence.build["manifest_sha256"] as? String,
          report["production_sha"] as? String == evidence.build["production_sha"] as? String,
          report["artifact_sha"] as? String == evidence.build["artifact_sha"] as? String,
          report["build_manifest_sha256"] as? String == evidence.buildDigest,
          report["source_inventory_sha256"] as? String
            == evidence.build["source_inventory_sha256"] as? String,
          report["runtime_inventory_sha256"] as? String
            == evidence.build["runtime_inventory_sha256"] as? String,
          report["native_sha256"] as? String == evidence.attestation["native_sha256"] as? String,
          report["launch_nonce"] as? String == nonce,
          exactInt(report["child_pid"]) == Int(child),
          exactBool(report["ipc_authenticated"]) == authenticated,
          report["model"] as? String == "Ling-3.0-tiny-oQ4e",
          let status = report["status"] as? String,
          ["PASS", "FAIL", "BLOCK"].contains(status),
          let reasons = report["reason_codes"] as? [String],
          reasons.count == Set(reasons).count, Set(reasons).isSubset(of: reasonCodes),
          let calls = exactInt(report["call_count"]), (0...2).contains(calls),
          let selected = report["selected_ids"] as? [String],
          selected.count == Set(selected).count,
          Set(selected).isSubset(of: ["email-urgent", "email-routine",
                                      "message-family", "message-routine", "calendar-today"]),
          let predicates = booleans(report["predicates"], keys: predicateKeys),
          let readiness = booleans(report["readiness"], keys: readinessKeys),
          let elapsed = exactInt(report["elapsed_ms"]), (0...60_000).contains(elapsed) else {
        throw Refusal.blocked
    }
    if status == "PASS" {
        guard reasons.isEmpty, calls == 2,
              selected == ["email-urgent", "message-family"],
              predicates.values.allSatisfy({ $0 }), readiness.values.allSatisfy({ $0 }),
              authenticated, child > 0 else { throw Refusal.blocked }
    } else if status == "BLOCK" {
        guard calls == 0, selected.isEmpty, !reasons.isEmpty else { throw Refusal.blocked }
    }
}

private func writeAll(_ fd: Int32, _ data: Data) throws {
    try data.withUnsafeBytes { raw in
        guard let base = raw.baseAddress else { return }
        var offset = 0
        while offset < data.count {
            let count = Darwin.write(fd, base.advanced(by: offset), data.count - offset)
            if count < 0 && errno == EINTR { continue }
            guard count > 0 else { throw Refusal.blocked }
            offset += count
        }
    }
}

private func privateDirectory() throws -> (Int32, URL) {
    let home = FileManager.default.homeDirectoryForCurrentUser
    let homeFD = open(home.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
    guard homeFD >= 0 else { throw Refusal.blocked }
    defer { close(homeFD) }
    if mkdirat(homeFD, ".wisp-summary-qa", 0o700) != 0 && errno != EEXIST {
        throw Refusal.blocked
    }
    let rootFD = openat(homeFD, ".wisp-summary-qa", O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
    guard rootFD >= 0 else { throw Refusal.blocked }
    var info = stat()
    guard fstat(rootFD, &info) == 0, (info.st_mode & S_IFMT) == S_IFDIR,
          info.st_uid == getuid(), info.st_mode & 0o077 == 0 else {
        close(rootFD); throw Refusal.blocked
    }
    if mkdirat(rootFD, "reports", 0o700) != 0 && errno != EEXIST {
        close(rootFD); throw Refusal.blocked
    }
    let reportsFD = openat(rootFD, "reports", O_RDONLY | O_DIRECTORY | O_NOFOLLOW)
    close(rootFD)
    guard reportsFD >= 0, fstat(reportsFD, &info) == 0,
          (info.st_mode & S_IFMT) == S_IFDIR, info.st_uid == getuid(),
          info.st_mode & 0o077 == 0 else {
        if reportsFD >= 0 { close(reportsFD) }
        throw Refusal.blocked
    }
    return (reportsFD, home.appendingPathComponent(".wisp-summary-qa/reports"))
}

private func save(_ report: [String: Any]) throws -> URL {
    let data = try JSONSerialization.data(withJSONObject: report, options: [.sortedKeys])
    guard data.count <= 32_768 else { throw Refusal.blocked }
    let (directory, url) = try privateDirectory()
    defer { close(directory) }
    let token = UUID().uuidString.lowercased()
    let temporary = ".report-\(token)"
    let final = "report-\(token).json"
    let fd = openat(directory, temporary, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
    guard fd >= 0 else { throw Refusal.blocked }
    var published = false
    defer {
        close(fd)
        if !published { unlinkat(directory, temporary, 0) }
    }
    try writeAll(fd, data)
    guard fsync(fd) == 0,
          renameatx_np(directory, temporary, directory, final, UInt32(RENAME_EXCL)) == 0,
          fsync(directory) == 0 else { throw Refusal.blocked }
    published = true
    return url.appendingPathComponent(final)
}

private func blockedReport(_ evidence: Evidence, nonce: String) -> [String: Any] {
    let falsePredicates = Dictionary(uniqueKeysWithValues: predicateKeys.map { ($0, false) })
    let falseReadiness = Dictionary(uniqueKeysWithValues: readinessKeys.map { ($0, false) })
    return ["schema_version": 2,
        "manifest_sha256": evidence.build["manifest_sha256"]!,
        "production_sha": evidence.build["production_sha"]!,
        "artifact_sha": evidence.build["artifact_sha"]!,
        "build_manifest_sha256": evidence.buildDigest,
        "source_inventory_sha256": evidence.build["source_inventory_sha256"]!,
        "runtime_inventory_sha256": evidence.build["runtime_inventory_sha256"]!,
        "native_sha256": evidence.attestation["native_sha256"]!,
        "launch_nonce": nonce, "child_pid": 0, "ipc_authenticated": false,
        "model": "Ling-3.0-tiny-oQ4e", "status": "BLOCK",
        "reason_codes": ["external_exclusivity_required"], "call_count": 0,
        "selected_ids": [], "predicates": falsePredicates,
        "readiness": falseReadiness, "elapsed_ms": 0]
}

private func withCStringArray<T>(_ values: [String],
                                 _ body: (UnsafeMutablePointer<UnsafeMutablePointer<CChar>?>) throws -> T) rethrows -> T {
    var pointers = values.map { strdup($0) }
    pointers.append(nil)
    defer { for pointer in pointers where pointer != nil { free(pointer) } }
    return try pointers.withUnsafeMutableBufferPointer { buffer in
        try body(buffer.baseAddress!)
    }
}

private func launch(_ evidence: Evidence, nonce: String) throws -> (pid_t, Pipe, Int32) {
    guard let relative = evidence.build["python_relative"] as? String else {
        throw Refusal.blocked
    }
    let runtime = evidence.stage.appendingPathComponent("runtime")
    guard try digestNoFollow(root: runtime, relative: "bin/python3")
            == evidence.pythonDigest else { throw Refusal.blocked }
    let python = evidence.stage.appendingPathComponent(relative).path
    let script = evidence.stage.appendingPathComponent("source/service/main.py").path
    let credential = Pipe()
    var resultFDs = [Int32](repeating: -1, count: 2)
    guard Darwin.pipe(&resultFDs) == 0 else { throw Refusal.blocked }
    var actions: posix_spawn_file_actions_t?
    var attributes: posix_spawnattr_t?
    guard posix_spawn_file_actions_init(&actions) == 0,
          posix_spawnattr_init(&attributes) == 0 else {
        close(resultFDs[0]); close(resultFDs[1]); throw Refusal.blocked
    }
    defer {
        posix_spawn_file_actions_destroy(&actions)
        posix_spawnattr_destroy(&attributes)
    }
    let input = credential.fileHandleForReading.fileDescriptor
    let credentialOutput = credential.fileHandleForWriting.fileDescriptor
    guard posix_spawn_file_actions_adddup2(&actions, input, STDIN_FILENO) == 0,
          posix_spawn_file_actions_adddup2(&actions, resultFDs[1], STDOUT_FILENO) == 0,
          posix_spawn_file_actions_addopen(&actions, STDERR_FILENO, "/dev/null", O_WRONLY, 0) == 0,
          posix_spawn_file_actions_addclose(&actions, credentialOutput) == 0,
          posix_spawn_file_actions_addclose(&actions, resultFDs[0]) == 0 else {
        close(resultFDs[0]); close(resultFDs[1]); throw Refusal.blocked
    }
    let flags = Int16(POSIX_SPAWN_SETPGROUP | POSIX_SPAWN_CLOEXEC_DEFAULT)
    guard posix_spawnattr_setflags(&attributes, flags) == 0,
          posix_spawnattr_setpgroup(&attributes, 0) == 0 else {
        close(resultFDs[0]); close(resultFDs[1]); throw Refusal.blocked
    }
    let environment = ["HOME=\(FileManager.default.homeDirectoryForCurrentUser.path)",
        "PATH=/usr/bin:/bin:/usr/sbin:/sbin", "WISP_QA_LAUNCH_NONCE=\(nonce)",
        "WISP_QA_BUILD_MANIFEST_SHA256=\(evidence.buildDigest)",
        "WISP_QA_ATTESTATION_SHA256=\(evidence.attestationDigest)",
        "WISP_CREDENTIAL_PIPE=\(try BackendCredentials.pipeMetadata(credential))"]
    let arguments = [python, "-I", "-S", "-B", script]
    var pid: pid_t = 0
    let result = withCStringArray(arguments) { argv in
        withCStringArray(environment) { envp in
            posix_spawn(&pid, python, &actions, &attributes, argv, envp)
        }
    }
    guard result == 0, pid > 0 else {
        close(resultFDs[0]); close(resultFDs[1]); throw Refusal.blocked
    }
    close(resultFDs[1])
    return (pid, credential, resultFDs[0])
}

@main
private enum SummaryQAMain {
static func main() {
    guard CommandLine.arguments.count == 1 else { refuse(64) }
    do {
        guard Bundle.main.bundleIdentifier == bundleID,
              let resources = Bundle.main.resourceURL else { throw Refusal.blocked }
        try strictBundleValidation()
        let evidence = try evidence(resources.appendingPathComponent("qa"))
        let nonce = try randomHex()
        if evidence.build["exclusive_proof_protocol"] as? String != "server-lease-v1" {
            let report = blockedReport(evidence, nonce: nonce)
            try validateReport(report, evidence: evidence, nonce: nonce,
                               child: 0, authenticated: false)
            print("Sanitized QA report written to \(try save(report).path)")
            return
        }
        // This branch cannot be assembled by the current builder.  It is kept
        // mechanically complete for a future reviewed server-lease verifier.
        let snapshot = try BackendCredentials.loadForBackend()
        guard let secret = snapshot.credentials["WISP_LOCAL_OMLX_KEY"],
              snapshot.credentials.count == 1 else { throw Refusal.blocked }
        let (pid, credentialPipe, reportFD) = try launch(evidence, nonce: nonce)
        defer {
            if !cleanupProcessGroup(pid) { _ = kill(-pid, SIGKILL) }
            close(reportFD)
        }
        try credentialPipe.fileHandleForReading.close()
        try BackendCredentials.writePipe(credentialPipe, credentials: snapshot.credentials,
            generation: snapshot.generation, role: "primary", pid: pid)
        try credentialPipe.fileHandleForWriting.close()
        let key = try sessionKey(secret, nonce: nonce, evidence: evidence,
                                 parent: getpid(), child: pid)
        let report = try authenticatedReport(
            readBounded(reportFD, timeout: 45, limit: 32_812), key: key)
        try validateReport(report, evidence: evidence, nonce: nonce,
                           child: pid, authenticated: true)
        print("Sanitized QA report written to \(try save(report).path)")
    } catch {
        refuse()
    }
}
}
