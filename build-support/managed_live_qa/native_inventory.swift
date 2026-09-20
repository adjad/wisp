import CryptoKit
import Darwin
import Foundation

enum NativeInventoryError: Error { case blocked }

private func hashData(_ data: Data) -> String {
    SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
}

private func safeComponents(_ relative: String) throws -> [String] {
    let values = relative.split(separator: "/", omittingEmptySubsequences: false).map(String.init)
    guard !relative.hasPrefix("/"), !values.isEmpty,
          values.allSatisfy({ !$0.isEmpty && $0 != "." && $0 != ".." }) else {
        throw NativeInventoryError.blocked
    }
    return values
}

private func openNoFollow(_ root: Int32, relative: String, directory: Bool = false) throws -> Int32 {
    let components = try safeComponents(relative)
    var current = dup(root)
    guard current >= 0 else { throw NativeInventoryError.blocked }
    for (index, component) in components.enumerated() {
        let final = index == components.count - 1
        let flags = O_RDONLY | O_CLOEXEC | O_NOFOLLOW |
            ((!final || directory) ? O_DIRECTORY : 0)
        let next = openat(current, component, flags)
        close(current)
        guard next >= 0 else { throw NativeInventoryError.blocked }
        current = next
    }
    return current
}

private func safeFileData(_ root: Int32, relative: String, limit: Int = 16_000_000) throws -> Data {
    let descriptor = try openNoFollow(root, relative: relative)
    defer { close(descriptor) }
    var info = stat()
    guard fstat(descriptor, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG,
          (info.st_uid == 0 || info.st_uid == getuid()), info.st_nlink == 1,
          info.st_mode & 0o022 == 0, info.st_size >= 0, info.st_size <= limit else {
        throw NativeInventoryError.blocked
    }
    var result = Data()
    var buffer = [UInt8](repeating: 0, count: 1_048_576)
    while true {
        let count = Darwin.read(descriptor, &buffer, buffer.count)
        if count < 0 && errno == EINTR { continue }
        guard count >= 0, result.count + count <= limit else {
            throw NativeInventoryError.blocked
        }
        if count == 0 { return result }
        result.append(buffer, count: count)
    }
}

private func treeEntries(_ directory: Int32, prefix: String,
                         result: inout Set<String>) throws {
    let streamDescriptor = dup(directory)
    guard streamDescriptor >= 0, let stream = fdopendir(streamDescriptor) else {
        if streamDescriptor >= 0 { close(streamDescriptor) }
        throw NativeInventoryError.blocked
    }
    defer { closedir(stream) }
    while let entry = readdir(stream) {
        var storage = entry.pointee.d_name
        let name = withUnsafePointer(to: &storage) {
            $0.withMemoryRebound(to: CChar.self, capacity: 1_024) { String(cString: $0) }
        }
        if name == "." || name == ".." { continue }
        var info = stat()
        guard fstatat(directory, name, &info, AT_SYMLINK_NOFOLLOW) == 0,
              (info.st_uid == 0 || info.st_uid == getuid()), info.st_mode & 0o022 == 0 else {
            throw NativeInventoryError.blocked
        }
        let relative = prefix.isEmpty ? name : "\(prefix)/\(name)"
        let kind = info.st_mode & S_IFMT
        if kind == S_IFDIR {
            let child = openat(directory, name, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_DIRECTORY)
            guard child >= 0 else { throw NativeInventoryError.blocked }
            defer { close(child) }
            try treeEntries(child, prefix: relative, result: &result)
        } else if kind == S_IFREG && info.st_nlink == 1 {
            guard name != "sitecustomize.py", name != "usercustomize.py",
                  name != "pyvenv.cfg", !name.hasSuffix(".pth") else {
                throw NativeInventoryError.blocked
            }
            result.insert(relative)
        } else {
            throw NativeInventoryError.blocked
        }
    }
}

private func inventory(_ stage: Int32, rootName: String, inventoryName: String,
                       expectedDigest: String) throws -> [String: String] {
    let raw = try safeFileData(stage, relative: inventoryName, limit: 1_000_000)
    guard let values = try JSONSerialization.jsonObject(with: raw) as? [String: String],
          !values.isEmpty,
          let canonical = try? JSONSerialization.data(withJSONObject: values, options: [.sortedKeys]),
          hashData(canonical) == expectedDigest else { throw NativeInventoryError.blocked }
    let root = try openNoFollow(stage, relative: rootName, directory: true)
    defer { close(root) }
    var actual = Set<String>()
    try treeEntries(root, prefix: "", result: &actual)
    guard actual == Set(values.keys) else { throw NativeInventoryError.blocked }
    for (relative, expected) in values {
        guard expected.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil,
              hashData(try safeFileData(root, relative: relative)) == expected else {
            throw NativeInventoryError.blocked
        }
    }
    return values
}

func verifyNativeInventories(stage: URL, expectedSourceDigest: String,
                             expectedRuntimeDigest: String) throws -> String {
    let stageDescriptor = open(stage.path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_DIRECTORY)
    guard stageDescriptor >= 0 else { throw NativeInventoryError.blocked }
    defer { close(stageDescriptor) }
    _ = try inventory(stageDescriptor, rootName: "source",
                      inventoryName: "qa-source-inventory.json",
                      expectedDigest: expectedSourceDigest)
    let runtime = try inventory(stageDescriptor, rootName: "runtime",
                                inventoryName: "qa-runtime-inventory.json",
                                expectedDigest: expectedRuntimeDigest)
    guard let python = runtime["bin/python3"] else { throw NativeInventoryError.blocked }
    return python
}

func digestNoFollow(root: URL, relative: String) throws -> String {
    let rootDescriptor = open(root.path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_DIRECTORY)
    guard rootDescriptor >= 0 else { throw NativeInventoryError.blocked }
    defer { close(rootDescriptor) }
    return hashData(try safeFileData(rootDescriptor, relative: relative))
}
