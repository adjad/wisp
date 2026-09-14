import Foundation
import Darwin

@main
struct BackendCredentialChecks {
    static func main() throws {
        let home = FileManager.default.temporaryDirectory.appendingPathComponent("wisp-credential-checks-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: home) }
        let directory = home.appendingPathComponent(".moe")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let marker = directory.appendingPathComponent(".helper-transaction.json")
        let generation = directory.appendingPathComponent(".credential-generation")
        let state = { try BackendCredentials.generation(home: home.path) }
        func denied(_ action: () throws -> Void) throws {
            do { try action(); fatalError("accepted quarantined credentials") }
            catch BackendCredentials.Failure.quarantined {}
        }
        let absent = try state()
        precondition(absent == "absent")
        for kind in 0..<3 {
            if kind == 0 { try Data().write(to: marker) }
            else if kind == 1 { try FileManager.default.createSymbolicLink(atPath: marker.path, withDestinationPath: home.appendingPathComponent("missing").path) }
            else { try FileManager.default.createDirectory(at: marker, withIntermediateDirectories: false) }
            try denied { _ = try BackendCredentials.load(reader: { _ in fatalError("read during quarantine") }, state: state) }
            try FileManager.default.removeItem(at: marker)
        }
        let epoch = String(repeating: "c", count: 64)
        try epoch.write(to: generation, atomically: true, encoding: .utf8)
        chmod(generation.path, 0o600)
        let observed = try state()
        precondition(observed == epoch)
        try denied {
            _ = try BackendCredentials.load(reader: { _ in try Data().write(to: marker); return nil }, state: state)
        }
        try FileManager.default.removeItem(at: marker)
        let fresh = try BackendCredentials.loadForBackend(reader: { _ in nil }, state: state)
        precondition(fresh.generation == epoch && fresh.credentials.isEmpty)
        var changed = false
        try denied {
            _ = try BackendCredentials.load(reader: { _ in changed = true; return nil }, state: { changed ? String(repeating: "d", count: 64) : epoch })
        }
        try denied { _ = try BackendCredentials.load(reader: { _ in fatalError("read after failed probe") }, state: { throw BackendCredentials.Failure.quarantined }) }
        try FileManager.default.removeItem(at: generation)
        try FileManager.default.createSymbolicLink(atPath: generation.path, withDestinationPath: home.appendingPathComponent("missing").path)
        try denied { _ = try state() }
        try FileManager.default.removeItem(at: generation)
        try FileManager.default.removeItem(at: directory)
        try Data().write(to: directory)
        try denied { _ = try state() }
        try FileManager.default.removeItem(at: directory)
        let trusted = Set([Data("fixture-app".utf8), Data("fixture-helper".utf8)])
        try BackendCredentials.validateReaderSets([trusted], expected: trusted)
        for invalid: [Set<Data>?] in [[], [nil], [Set()], [Set([Data("other".utf8)])], [trusted, nil], [trusted.union([Data("extra".utf8)])]] {
            do {
                try BackendCredentials.validateReaderSets(invalid, expected: trusted)
                fatalError("accepted untrusted ACL")
            } catch BackendCredentials.Failure.readerMismatch {}
        }
        let synthetic = String(repeating: "a", count: 64)
        var seen = Set<String>()
        let values = try BackendCredentials.load(reader: { account in
            seen.insert(account)
            return account == "mini-node" ? nil : (account == "local-omlx" ? synthetic : String(repeating: "b", count: 64))
        }, state: state)
        precondition(seen == Set(["local-omlx", "mini-inference", "mini-node"]))
        precondition(values.count == 2 && values["WISP_MINI_NODE_KEY"] == nil)
        let env = BackendCredentials.injecting(values.merging(["OTHER_KEY": synthetic]) { $1 },
            into: ["PATH": "/usr/bin", "WISP_MINI_NODE_KEY": "inherited", "WISP_LOCAL_OMLX_KEY": "inherited"])
        precondition(env["PATH"] == "/usr/bin" && env["OTHER_KEY"] == nil)
        precondition(env["WISP_LOCAL_OMLX_KEY"] == nil && env["WISP_MINI_NODE_KEY"] == nil)
        let bridge = Pipe()
        let pipeMetadata = try BackendCredentials.pipeMetadata(bridge)
        precondition(pipeMetadata.hasPrefix("v1:"))
        try BackendCredentials.writePipe(bridge, credentials: values, generation: "absent", role: "primary", pid: getpid())
        let frame = try bridge.fileHandleForReading.readToEnd()!
        precondition(frame.prefix(8) == Data("WISPCP1\n".utf8))
        let decoded = try JSONSerialization.jsonObject(with: frame.dropFirst(12)) as! [String: Any]
        precondition(decoded["role"] as? String == "primary" && decoded["pid"] as? Int == Int(getpid()))
        precondition(decoded["credentials"] as? [String: String] == values)
        try bridge.fileHandleForReading.close()
        for bad in ["", "short", String(repeating: "A", count: 64), synthetic + "\n"] {
            do { _ = try BackendCredentials.load(reader: { _ in bad }, state: state); fatalError("accepted malformed fixture") }
            catch BackendCredentials.Failure.malformed {}
        }
        do {
            _ = try BackendCredentials.load(reader: { _ in throw BackendCredentials.Failure.unavailable }, state: state)
            fatalError("ignored denied read")
        } catch BackendCredentials.Failure.unavailable {}
        var store: [String: String] = [:]
        var writes = 0
        for _ in 0..<2 {
            try BackendCredentials.initialize(local: synthetic, readers: ["/synthetic/reader"],
                reader: { store[$0] }, writer: { account, value, readers in
                    precondition(readers == ["/synthetic/reader"])
                    store[account] = value
                    writes += 1
                })
        }
        precondition(writes == 3 && store["local-omlx"] == synthetic)
        precondition(Set(store.values).count == 3)
        do {
            try BackendCredentials.initialize(local: String(repeating: "b", count: 64), readers: [],
                reader: { store[$0] }, writer: { _, _, _ in fatalError("wrote mismatched auth") })
            fatalError("accepted mismatched local auth")
        } catch BackendCredentials.Failure.malformed {}
        do {
            try BackendCredentials.initialize(local: synthetic, readers: [],
                reader: { _ in synthetic }, writer: { _, _, _ in fatalError("wrote duplicate key") })
            fatalError("accepted duplicate account keys")
        } catch BackendCredentials.Failure.malformed {}
        do {
            _ = try BackendCredentials.load(reader: { _ in synthetic }, state: state)
            fatalError("exported duplicate account keys")
        } catch BackendCredentials.Failure.malformed {}
        print("BackendCredentials: synthetic bridge checks passed")
    }
}
