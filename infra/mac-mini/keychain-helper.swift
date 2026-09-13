import Foundation
import Security

@main
struct KeychainHelper {
    static func main() {
        do {
            let command = CommandLine.arguments.dropFirst().first
            switch command {
            case "init":
                let input = FileHandle.standardInput.readDataToEndOfFile()
                guard input.count < 1024, let local = String(data: input, encoding: .utf8),
                      CommandLine.arguments.count == 3 else { throw BackendCredentials.Failure.malformed }
                try BackendCredentials.initialize(local: local,
                    readers: [CommandLine.arguments[0], CommandLine.arguments[2]])
                print("{\"credentials\":\"ready\"}")
            case "status":
                let values = try BackendCredentials.load()
                print(values.count == 3 ? "{\"credentials\":\"ready\"}" : "{\"credentials\":\"missing\"}")
            case "init-mini-local":
                let path = URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent(".omlx/settings.json")
                let settings = try JSONSerialization.jsonObject(with: Data(contentsOf: path)) as? [String: Any]
                guard let auth = settings?["auth"] as? [String: Any], let value = auth["api_key"] as? String,
                      BackendCredentials.valid(value) else { throw BackendCredentials.Failure.malformed }
                for account in ["mini-inference", "mini-node"] {
                    guard let remote = try BackendCredentials.read(account), remote != value else {
                        throw BackendCredentials.Failure.malformed
                    }
                }
                if let existing = try BackendCredentials.read("local-omlx") {
                    guard existing == value else { throw BackendCredentials.Failure.malformed }
                } else {
                    let launcher = URL(fileURLWithPath: CommandLine.arguments[0])
                        .deletingLastPathComponent().appendingPathComponent("mini-launcher").path
                    try BackendCredentials.add("local-omlx", value: value,
                        readers: [CommandLine.arguments[0], launcher])
                }
                print("{\"credentials\":\"ready\"}")
            case "import-mini":
                let input = FileHandle.standardInput.readDataToEndOfFile()
                guard input.count < 1024,
                      let values = try JSONSerialization.jsonObject(with: input) as? [String: String],
                      Set(values.keys) == Set(["mini-inference", "mini-node"]),
                      Set(values.values).count == 2,
                      values.values.allSatisfy(BackendCredentials.valid) else {
                    throw BackendCredentials.Failure.malformed
                }
                // Check every entry before any write; never rotate an existing key.
                for (account, value) in values {
                    if let existing = try BackendCredentials.read(account), existing != value {
                        throw BackendCredentials.Failure.storage
                    }
                    if let local = try BackendCredentials.read("local-omlx"), local == value {
                        throw BackendCredentials.Failure.malformed
                    }
                }
                for (account, value) in values {
                    if try BackendCredentials.read(account) == nil {
                        let launcher = URL(fileURLWithPath: CommandLine.arguments[0])
                            .deletingLastPathComponent().appendingPathComponent("mini-launcher").path
                        try BackendCredentials.add(account, value: value,
                            readers: [CommandLine.arguments[0], launcher])
                    }
                }
                print("{\"credentials\":\"ready\"}")
            case "export-mini":
                // Machine-only pipe. Refuse a terminal to prevent accidental display.
                var info = stat()
                guard fstat(STDOUT_FILENO, &info) == 0, (info.st_mode & S_IFMT) == S_IFIFO else { throw BackendCredentials.Failure.unavailable }
                let all = try BackendCredentials.load()
                guard all.count == 3 else { throw BackendCredentials.Failure.unavailable }
                var values: [String: String] = [:]
                for account in ["mini-inference", "mini-node"] {
                    guard let value = all[BackendCredentials.accounts[account]!] else {
                        throw BackendCredentials.Failure.unavailable
                    }
                    values[account] = value
                }
                let data = try JSONSerialization.data(withJSONObject: values, options: [.sortedKeys])
                FileHandle.standardOutput.write(data)
            default: throw BackendCredentials.Failure.unavailable
            }
        } catch {
            FileHandle.standardError.write(Data("Keychain operation unavailable\n".utf8))
            exit(1)
        }
    }
}
