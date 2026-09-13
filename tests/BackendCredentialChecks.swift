import Foundation

@main
struct BackendCredentialChecks {
    static func main() throws {
        let synthetic = String(repeating: "a", count: 64)
        var seen = Set<String>()
        let values = try BackendCredentials.load { account in
            seen.insert(account)
            return account == "mini-node" ? nil : (account == "local-omlx" ? synthetic : String(repeating: "b", count: 64))
        }
        precondition(seen == Set(["local-omlx", "mini-inference", "mini-node"]))
        precondition(values.count == 2 && values["WISP_MINI_NODE_KEY"] == nil)
        let env = BackendCredentials.injecting(values.merging(["OTHER_KEY": synthetic]) { $1 },
            into: ["PATH": "/usr/bin", "WISP_MINI_NODE_KEY": "inherited", "WISP_LOCAL_OMLX_KEY": "inherited"])
        precondition(env["PATH"] == "/usr/bin" && env["OTHER_KEY"] == nil)
        precondition(env["WISP_LOCAL_OMLX_KEY"] == synthetic && env["WISP_MINI_NODE_KEY"] == nil)
        for bad in ["", "short", String(repeating: "A", count: 64), synthetic + "\n"] {
            do { _ = try BackendCredentials.load { _ in bad }; fatalError("accepted malformed fixture") }
            catch BackendCredentials.Failure.malformed {}
        }
        do {
            _ = try BackendCredentials.load { _ in throw BackendCredentials.Failure.unavailable }
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
            _ = try BackendCredentials.load { _ in synthetic }
            fatalError("exported duplicate account keys")
        } catch BackendCredentials.Failure.malformed {}
        print("BackendCredentials: synthetic bridge checks passed")
    }
}
