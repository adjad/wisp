import Foundation
import Darwin

// Disposable signed fixture. No Keychain or browser access. Socket path and code
// requirement are supplied by the test supervisor, never by an untrusted peer.
@main struct TransportHarness {
    static func addressFor(_ path: String) throws -> sockaddr_un {
        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        let bytes = Array(path.utf8) + [UInt8(0)]
        try BrowserBridgeWire.check(bytes.count <= MemoryLayout.size(ofValue: address.sun_path))
        withUnsafeMutableBytes(of: &address.sun_path) { $0.copyBytes(from: bytes) }
        return address
    }
    static func main() {
        do {
            let args = CommandLine.arguments
            if args[1] == "server" {
                let listener: Int32
                if let inherited = Int32(args[2]) { listener = inherited }
                else {
                    listener = socket(AF_UNIX, SOCK_STREAM, 0)
                    var address = try addressFor(args[2])
                    let status = withUnsafePointer(to: &address) {
                        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                            bind(listener, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
                        }
                    }
                    try BrowserBridgeWire.check(status == 0 && chmod(args[2], 0o600) == 0 && listen(listener, 1) == 0)
                    print("ready")
                    fflush(stdout)
                }
                defer { Darwin.close(listener) }
                let fd = accept(listener, nil, nil)
                try BrowserBridgeWire.check(fd >= 0)
                let transport = try BrowserBridgeTransport(connectedFD: fd,
                    peerRequirement: args[3], timeoutSeconds: 0.5)
                let packet = try transport.receive()
                try transport.send(args.count > 4 && args[4] == "backpressure"
                    ? Data(repeating: 0, count: BrowserBridgeWire.maxFrame) : packet)
                if Int32(args[2]) == nil {
                    // Stay alive until the mutually authenticating client has
                    // completed its post-read identity check and closed.
                    var item = pollfd(fd: fd, events: Int16(POLLIN), revents: 0)
                    _ = poll(&item, 1, 2000)
                }
                transport.close()
                print("accepted")
            } else {
                let fd = socket(AF_UNIX, SOCK_STREAM, 0)
                defer { Darwin.close(fd) }
                var address = try addressFor(args[2])
                let status = withUnsafePointer(to: &address) {
                    $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                        connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
                    }
                }
                try BrowserBridgeWire.check(status == 0)
                var one: Int32 = 1
                _ = setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, 4)
                let mode = args[3]
                if mode == "mutual" {
                    // Transport owns a duplicate; defer above owns the original.
                    let transport = try BrowserBridgeTransport(connectedFD: dup(fd), peerRequirement: args[4],
                                                               timeoutSeconds: 0.5)
                    try transport.send(Data("wisp".utf8))
                    print(try transport.receive() == Data("wisp".utf8) ? "echo" : "denied")
                    return
                }
                var packet: [UInt8] = [0, 0, 0, 4, 119, 105, 115, 112]
                if mode == "oversize" { packet = [0, 4, 0, 1] }
                if mode == "empty" { packet = [0, 0, 0, 0] }
                if mode == "truncated" { packet = [0, 0, 0, 4, 119] }
                if mode == "header_eof" { packet = [0, 0] }
                if mode == "timeout" { usleep(800_000); print("timeout"); return }
                // One byte writes exercise stream fragmentation; no message
                // boundary is assumed by the receiving implementation.
                for var byte in packet {
                    if Darwin.send(fd, &byte, 1, 0) != 1 { break }
                }
                if mode == "backpressure" { usleep(800_000); print("timeout"); return }
                if ["truncated", "header_eof"].contains(mode) { _ = shutdown(fd, SHUT_WR) }
                var received = [UInt8]()
                var byte: UInt8 = 0
                while received.count < 8 && Darwin.recv(fd, &byte, 1, 0) == 1 { received.append(byte) }
                print(received == [0, 0, 0, 4, 119, 105, 115, 112] ? "echo" : "closed")
            }
        } catch { print("denied") }
    }
}
