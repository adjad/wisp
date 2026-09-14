import Foundation
import Darwin
import CryptoKit

/// Disposable native launch fixture. Inputs stay in pipes; outputs are fixed phases.
@main
struct NativeCredentialPipeFixture {
    static func main() {
        do {
            let args=CommandLine.arguments
            guard args.count==4 else { throw BackendCredentials.Failure.unavailable }
            let input=FileHandle.standardInput.readDataToEndOfFile()
            guard input.count<=4096, let values=try JSONSerialization.jsonObject(with: input) as? [String:String] else { throw BackendCredentials.Failure.malformed }
            let root=URL(fileURLWithPath:args[3])
            let process=Process(), bridge=Pipe(), output=Pipe()
            process.executableURL=URL(fileURLWithPath:args[1])
            process.arguments=["-I","-B",args[2],root.path]
            process.environment=["PATH":"/usr/bin:/bin","HOME":root.path,
                "WISP_CREDENTIAL_PIPE":try BackendCredentials.pipeMetadata(bridge)]
            process.standardInput=bridge;process.standardOutput=output;process.standardError=FileHandle.nullDevice
            try process.run()
            defer { if process.isRunning { process.terminate() }; try? bridge.fileHandleForWriting.close() }
            try bridge.fileHandleForReading.close()
            let ready=output.fileHandleForReading.availableData
            guard ready==Data("READY\n".utf8) else { throw BackendCredentials.Failure.unavailable }
            print("BEFORE \(process.processIdentifier)");fflush(stdout)
            func wait(_ name:String) throws {
                let deadline=Date().addingTimeInterval(15)
                while !FileManager.default.fileExists(atPath:root.appendingPathComponent(name).path) {
                    guard Date()<deadline,process.isRunning else { throw BackendCredentials.Failure.unavailable }
                    Thread.sleep(forTimeInterval:0.01)
                }
            }
            try wait("before")
            try BackendCredentials.writePipe(bridge,credentials:values,generation:"absent",role:"primary",pid:process.processIdentifier)
            let response=output.fileHandleForReading.availableData
            let canonical=values.sorted(by:{$0.key<$1.key}).map{$0.key+":"+$0.value}.joined(separator:"\n")
            let hash=SHA256.hash(data:Data(canonical.utf8)).map{String(format:"%02x",$0)}.joined()
            guard response==Data(("CONSUMED "+hash+"\n").utf8) else { throw BackendCredentials.Failure.unavailable }
            print("AFTER \(process.processIdentifier)");fflush(stdout)
            try wait("after")
            process.waitUntilExit()
            guard process.terminationStatus==0 else { throw BackendCredentials.Failure.unavailable }
            print("PIPE_PASS")
        } catch { fputs("PIPE_UNAVAILABLE\n",stderr);exit(1) }
    }
}
