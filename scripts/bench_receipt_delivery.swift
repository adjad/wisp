// Compile with the selected checkout's WispClient.swift, AgentSend.swift and
// AssistantDelivery.swift. Uses synthetic receipts and an in-memory HTTP ACK.
import Darwin
import Foundation

final class BenchmarkACK: URLProtocol {
    static var count = 0
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        precondition(request.url!.host == "offline.fixture")
        precondition(request.url!.path.hasSuffix("/ack"))
        Self.count += 1
        let id = request.url!.pathComponents.dropLast().last!
        let data = try! JSONSerialization.data(withJSONObject:
            ["ok": true, "event_id": id, "kind": "changed"])
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!,
            statusCode: 200, httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

@main
struct ReceiptDeliveryBenchmark {
    static func cpuSeconds() -> Double {
        var usage = rusage()
        precondition(getrusage(RUSAGE_SELF, &usage) == 0)
        return Double(usage.ru_utime.tv_sec + usage.ru_stime.tv_sec)
            + Double(usage.ru_utime.tv_usec + usage.ru_stime.tv_usec) / 1_000_000
    }

    @MainActor static func main() async throws {
        let args = CommandLine.arguments
        guard args.count == 5, let seedCount = Int(args[2]), seedCount >= 0,
              let events = Int(args[3]), events > 0,
              let interval = Double(args[4]), interval >= 0 else {
            fatalError("Usage: benchmark <new-fixture-directory> <seed-count> <events> <interval-seconds>")
        }
        let directory = URL(fileURLWithPath: args[1], isDirectory: true)
        // Never open a user receipt file: the fixture directory must be new.
        guard !FileManager.default.fileExists(atPath: directory.path) else {
            fatalError("Benchmark fixture directory already exists")
        }
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let path = directory.appendingPathComponent("receipts.json")
        var seed: [String: [String: Any]] = [:]
        for i in 0..<seedCount {
            seed[String(format: "%032x", i)] = ["kind": "changed", "done": true]
        }
        let original = try JSONSerialization.data(withJSONObject: seed)
        try original.write(to: path)
        let delivery = AssistantDelivery(path: path)
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [BenchmarkACK.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel() }
        let client = WispClient(deliverySession: session, deliveryURL: URL(string: "http://offline.fixture")!)
        var latencies: [Double] = []
        var performed = 0
        let start = ProcessInfo.processInfo.systemUptime
        let cpu = cpuSeconds()
        for i in 0..<events {
            let target = start + Double(i) * interval
            let remaining = target - ProcessInfo.processInfo.systemUptime
            if remaining > 0 { try await Task.sleep(for: .seconds(remaining)) }
            let eventStart = ProcessInfo.processInfo.systemUptime
            let event = WispClient.Event(type: "changed", payload:
                ["type": "changed", "event_id": "benchmark-refresh-\(i)"])
            let ok = await delivery.handle(event, client: client,
                perform: { _ in performed += 1; return true }, calendar: { _ in fatalError() })
            precondition(ok)
            latencies.append(ProcessInfo.processInfo.systemUptime - eventStart)
        }
        // Include the final interval so periodic CPU has a full observation window.
        let remaining = start + Double(events) * interval - ProcessInfo.processInfo.systemUptime
        if remaining > 0 { try await Task.sleep(for: .seconds(remaining)) }
        let cpuDelta = cpuSeconds() - cpu
        let elapsed = ProcessInfo.processInfo.systemUptime - start
        let final = try Data(contentsOf: path)
        let result: [String: Any] = [
            "kind": "synthetic_receipt_delivery", "seed_receipts": seedCount,
            "events": events, "performed": performed, "acks": BenchmarkACK.count,
            "interval_s": interval, "elapsed_s": elapsed, "cpu_s": cpuDelta,
            "cpu_pct_one_core": 100 * cpuDelta / elapsed,
            "handle_seconds": latencies, "initial_bytes": original.count,
            "final_bytes": final.count, "receipt_bytes_unchanged": final == original,
        ]
        let encoded = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
        print(String(data: encoded, encoding: .utf8)!)
    }
}
