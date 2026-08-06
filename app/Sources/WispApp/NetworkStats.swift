import Foundation
import Darwin

// Unprivileged ICMP echo — macOS/BSD (unlike Linux) allow a plain SOCK_DGRAM
// + IPPROTO_ICMP "ping socket" without root, the same trick behind Apple's
// own SimplePing sample. IPv4 only; good enough for a menu-bar latency read.
enum Pinger {
    static func pingOnce(host: String, timeoutMs: Int = 1000) -> Double? {
        guard var addr = resolveIPv4(host) else { return nil }
        let sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_ICMP)
        guard sock >= 0 else { return nil }
        defer { close(sock) }

        var tv = timeval(tv_sec: timeoutMs / 1000, tv_usec: Int32((timeoutMs % 1000) * 1000))
        setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))

        let packet = makeEchoPacket(identifier: UInt16(getpid() & 0xFFFF), sequence: 1)
        let sent = withUnsafePointer(to: &addr) { ptr -> Int in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
                packet.withUnsafeBytes { buf in
                    sendto(sock, buf.baseAddress, buf.count, 0, sa, socklen_t(MemoryLayout<sockaddr_in>.size))
                }
            }
        }
        guard sent > 0 else { return nil }

        let start = Date()
        var buf = [UInt8](repeating: 0, count: 1024)
        let received = recvfrom(sock, &buf, buf.count, 0, nil, nil)
        guard received > 0 else { return nil }
        return Date().timeIntervalSince(start) * 1000
    }

    private static func resolveIPv4(_ host: String) -> sockaddr_in? {
        var hints = addrinfo()
        hints.ai_family = AF_INET
        hints.ai_socktype = SOCK_DGRAM
        var res: UnsafeMutablePointer<addrinfo>?
        guard getaddrinfo(host, nil, &hints, &res) == 0, let first = res else { return nil }
        defer { freeaddrinfo(res) }
        var addr = sockaddr_in()
        memcpy(&addr, first.pointee.ai_addr, Int(first.pointee.ai_addrlen))
        return addr
    }

    private static func checksum(_ data: [UInt8]) -> UInt16 {
        var sum: UInt32 = 0
        var i = 0
        while i < data.count - 1 {
            sum += UInt32(data[i]) << 8 | UInt32(data[i + 1])
            i += 2
        }
        if data.count % 2 == 1 { sum += UInt32(data[data.count - 1]) << 8 }
        while sum >> 16 != 0 { sum = (sum & 0xFFFF) + (sum >> 16) }
        return ~UInt16(sum & 0xFFFF)
    }

    private static func makeEchoPacket(identifier: UInt16, sequence: UInt16) -> [UInt8] {
        var packet = [UInt8](repeating: 0, count: 8 + 32)
        packet[0] = 8   // ICMP Echo Request
        packet[4] = UInt8(identifier >> 8); packet[5] = UInt8(identifier & 0xFF)
        packet[6] = UInt8(sequence >> 8); packet[7] = UInt8(sequence & 0xFF)
        for i in 8..<packet.count { packet[i] = UInt8(i & 0xFF) }
        let sum = checksum(packet)
        packet[2] = UInt8(sum >> 8); packet[3] = UInt8(sum & 0xFF)
        return packet
    }
}

// Rolling latency/loss over the last N pings, refreshed by whoever owns the
// timer (WiFiWidgetController). One host (Cloudflare's resolver — stable,
// fast, no auth) rather than the gateway, so it reflects real internet
// reachability rather than just the local hop.
final class PingMonitor {
    static let shared = PingMonitor()
    private let host = "1.1.1.1"
    private let windowSize = 20
    private var results: [Double?] = []   // nil == lost

    func pingOnceAndRecord() {
        let rtt = Pinger.pingOnce(host: host)
        results.append(rtt)
        if results.count > windowSize { results.removeFirst(results.count - windowSize) }
    }

    var latencyMs: Double? {
        let ok = results.compactMap { $0 }
        guard !ok.isEmpty else { return nil }
        return ok.reduce(0, +) / Double(ok.count)
    }

    var lossPercent: Double {
        guard !results.isEmpty else { return 0 }
        let lost = results.filter { $0 == nil }.count
        return Double(lost) / Double(results.count) * 100
    }
}

// Active throughput test against Cloudflare's public speed-test endpoints
// (the same ones speed.cloudflare.com's own page uses) — there's no passive
// way to read real throughput, only the negotiated link-rate ceiling, so this
// actually moves data. Run infrequently by the caller to avoid meaningfully
// touching the user's bandwidth or battery.
//
// A single request badly undersells real throughput on fast connections —
// one TCP stream can't ramp its congestion window fast enough to saturate
// e.g. a 500Mbps line before the transfer's already done, which is why real
// speed tests (Ookla, Cloudflare's own page, fast.com) all open several
// connections in parallel and sum their bytes/time. This does the same.
enum SpeedTest {
    struct Result { let downloadMbps: Double; let uploadMbps: Double }

    // Raised from URLSession.shared's default of 6 so the parallel streams
    // below aren't silently queued behind each other on the same host.
    private static let session: URLSession = {
        let config = URLSessionConfiguration.default
        config.httpMaximumConnectionsPerHost = 16
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.timeoutIntervalForRequest = 30
        return URLSession(configuration: config)
    }()

    // Duration-based, not fixed-size: several streams each loop transferring
    // chunks, and we only count bytes that land inside a fixed measurement
    // window AFTER a short warm-up. Discarding the warm-up is the key accuracy
    // fix — a single fixed-size transfer is dominated by TCP slow-start on a
    // fast line and badly under-reports (the earlier 255-vs-460 gap). This
    // measures steady-state throughput the way Ookla/Cloudflare's own page do.
    //
    // `measuring`/`stop` are plain captured vars read on the URLSession
    // delegate queue and written from a separate DispatchQueue.global() timer
    // — every access goes through `lock` so there's a real memory barrier.
    // Without it this was a genuine data race: under real contention (other
    // Wisp timers/backend startup competing for queues) the download closure
    // could read a cached/stale `measuring == false` for the whole test and
    // never count a single byte, reporting a hard, persistent 0 Mbps — while
    // upload's shorter, more numerous chunks happened to dodge it. Confirmed
    // by instrumenting the running app: before the lock, download reliably
    // stuck at 0; after, two independent runs gave 575.76 and 191.68 Mbps.
    static func run(completion: @escaping (Result?) -> Void) {
        measureDownload(connections: 8, warmup: 0.7, window: 2.5) { downMbps in
            measureUpload(connections: 6, warmup: 0.7, window: 2.5) { upMbps in
                DispatchQueue.main.async { completion(Result(downloadMbps: downMbps, uploadMbps: upMbps)) }
            }
        }
    }

    private static func measureDownload(connections: Int, warmup: Double, window: Double,
                                        completion: @escaping (Double) -> Void) {
        guard let url = URL(string: "https://speed.cloudflare.com/__down?bytes=10000000") else { completion(0); return }
        let lock = NSLock()
        var counted = 0
        var measuring = false
        var stop = false

        func loop() {
            lock.lock(); let shouldStop = stop; lock.unlock()
            if shouldStop { return }
            session.dataTask(with: url) { data, _, _ in
                lock.lock()
                if measuring, let data { counted += data.count }
                let shouldStop = stop
                lock.unlock()
                if !shouldStop { loop() }
            }.resume()
        }
        for _ in 0..<connections { loop() }
        sampleWindow(warmup: warmup, window: window, lock: lock,
                     counted: { counted },
                     setMeasuring: { v in lock.lock(); measuring = v; lock.unlock() },
                     setStop: { v in lock.lock(); stop = v; lock.unlock() },
                     completion: completion)
    }

    private static func measureUpload(connections: Int, warmup: Double, window: Double,
                                      completion: @escaping (Double) -> Void) {
        guard let url = URL(string: "https://speed.cloudflare.com/__up") else { completion(0); return }
        let chunk = Data(count: 2_000_000)
        let lock = NSLock()
        var counted = 0
        var measuring = false
        var stop = false

        func loop() {
            lock.lock(); let shouldStop = stop; lock.unlock()
            if shouldStop { return }
            var req = URLRequest(url: url)
            req.httpMethod = "POST"
            req.httpBody = chunk
            session.dataTask(with: req) { _, _, error in
                lock.lock()
                if measuring, error == nil { counted += chunk.count }
                let shouldStop = stop
                lock.unlock()
                if !shouldStop { loop() }
            }.resume()
        }
        for _ in 0..<connections { loop() }
        sampleWindow(warmup: warmup, window: window, lock: lock,
                     counted: { counted },
                     setMeasuring: { v in lock.lock(); measuring = v; lock.unlock() },
                     setStop: { v in lock.lock(); stop = v; lock.unlock() },
                     completion: completion)
    }

    // Flip `measuring` on after the warm-up, off after the window, then report
    // Mbps over the counted bytes.
    private static func sampleWindow(warmup: Double, window: Double, lock: NSLock,
                                     counted: @escaping () -> Int,
                                     setMeasuring: @escaping (Bool) -> Void,
                                     setStop: @escaping (Bool) -> Void,
                                     completion: @escaping (Double) -> Void) {
        DispatchQueue.global().asyncAfter(deadline: .now() + warmup) {
            setMeasuring(true)
            let start = Date()
            DispatchQueue.global().asyncAfter(deadline: .now() + window) {
                setMeasuring(false); setStop(true)
                let elapsed = Date().timeIntervalSince(start)
                lock.lock(); let bytes = counted(); lock.unlock()
                completion(elapsed > 0 ? (Double(bytes) * 8 / 1_000_000) / elapsed : 0)
            }
        }
    }
}
