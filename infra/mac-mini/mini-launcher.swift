import Foundation
import Darwin

/// Installed executable referenced by secret-free launchd plists.
@main
struct MiniLauncher {
    static func main() {
        if CommandLine.arguments.dropFirst().first == "protocol-version" {
            print("wisp-mini-helper-v2")
            return
        }
        do {
            let args = Array(CommandLine.arguments.dropFirst())
            guard args.count == 3, ["gateway", "node"].contains(args[0]),
                  args[1].hasPrefix("/"), args[2].hasPrefix("n") else {
                throw BackendCredentials.Failure.unavailable
            }
            let root = URL(fileURLWithPath: args[1])
            let names = args[0] == "gateway" ? ["local-omlx", "mini-inference"] : ["mini-node"]
            var env = ["PATH": "/usr/bin:/bin", "HOME": NSHomeDirectory(), "PYTHONUNBUFFERED": "1",
                       "PYTHONDONTWRITEBYTECODE": "1"]
            var credentials: [String: String] = [:]
            for name in names {
                guard let value = try BackendCredentials.read(name) else {
                    throw BackendCredentials.Failure.unavailable
                }
                credentials[BackendCredentials.accounts[name]!] = value
            }
            let executable = root.appendingPathComponent("venv/bin/python3").path
            var arguments = [executable, "-m", "mini", args[0]]
            if args[0] == "gateway" {
                // Filled only by independently authorized arrival qualification.
                // Missing/stale files refuse startup; no unqualified fallback.
                let qualification = root.deletingLastPathComponent().appendingPathComponent("state/qualification")
                arguments += ["--resource-contract", qualification.appendingPathComponent("resource-contract.json").path,
                              "--resource-telemetry", qualification.appendingPathComponent("resource-telemetry.json").path]
            }
            if args[0] == "node" {
                arguments += ["--state-dir", root.appendingPathComponent("state").path, "--node-id", args[2]]
            }
            guard chdir(root.appendingPathComponent("runtime").path) == 0 else {
                throw BackendCredentials.Failure.unavailable
            }
            let credentialPipe = Pipe()
            env["WISP_CREDENTIAL_PIPE"] = try BackendCredentials.pipeMetadata(credentialPipe)
            try BackendCredentials.writePipe(credentialPipe, credentials: credentials,
                generation: "absent", role: args[0], pid: getpid())
            credentials.removeAll()
            guard dup2(credentialPipe.fileHandleForReading.fileDescriptor, STDIN_FILENO) == STDIN_FILENO,
                  fcntl(STDIN_FILENO, F_SETFD, 0) == 0 else { throw BackendCredentials.Failure.unavailable }
            if credentialPipe.fileHandleForReading.fileDescriptor != STDIN_FILENO {
                try credentialPipe.fileHandleForReading.close()
            }
            // Replace the launcher so launchd controls the actual runtime PID.
            // No orphaned Python child can survive a wrapper-only termination.
            let null = open("/dev/null", O_WRONLY)
            guard null >= 0 else { throw BackendCredentials.Failure.unavailable }
            dup2(null, STDOUT_FILENO)
            dup2(null, STDERR_FILENO)
            close(null)
            let argv = arguments.map { strdup($0) } + [nil]
            let envp = env.sorted(by: { $0.key < $1.key }).map { strdup($0.key + "=" + $0.value) } + [nil]
            defer {
                argv.forEach { free($0) }
                envp.forEach { free($0) }
            }
            argv.withUnsafeBufferPointer { av in
                envp.withUnsafeBufferPointer { ev in
                    _ = execve(executable, av.baseAddress!, ev.baseAddress!)
                }
            }
            throw BackendCredentials.Failure.unavailable
        } catch {
            // No credentials, system errors, or process details in diagnostics.
            exit(1)
        }
    }
}
