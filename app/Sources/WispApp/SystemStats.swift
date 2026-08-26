import Foundation

// CPU + memory readings straight from the kernel (mach host_statistics) —
// the same counters Activity Monitor uses. No entitlements, no LLM.
enum SystemStats {
    // CPU usage is a delta between two point-in-time tick snapshots, so the
    // first call after launch always returns 0 — that's expected, not a bug.
    private static var prevTicks: host_cpu_load_info?

    static func cpuUsagePercent() -> Double {
        var size = mach_msg_type_number_t(MemoryLayout<host_cpu_load_info_data_t>.size / MemoryLayout<integer_t>.size)
        var info = host_cpu_load_info()
        let result = withUnsafeMutablePointer(to: &info) { ptr -> kern_return_t in
            ptr.withMemoryRebound(to: integer_t.self, capacity: Int(size)) { intPtr in
                host_statistics(mach_host_self(), HOST_CPU_LOAD_INFO, intPtr, &size)
            }
        }
        guard result == KERN_SUCCESS else { return 0 }
        defer { prevTicks = info }
        guard let prev = prevTicks else { return 0 }
        let user = Double(info.cpu_ticks.0 &- prev.cpu_ticks.0)
        let system = Double(info.cpu_ticks.1 &- prev.cpu_ticks.1)
        let idle = Double(info.cpu_ticks.2 &- prev.cpu_ticks.2)
        let nice = Double(info.cpu_ticks.3 &- prev.cpu_ticks.3)
        let total = user + system + idle + nice
        guard total > 0 else { return 0 }
        return max(0, min(100, (user + system + nice) / total * 100))
    }

    // usedBytes = Total − Free − Cached Files, where **Cached Files =
    // external_page_count ONLY** (file-backed pages). Nailed down by two
    // atomic side-by-side captures against Activity Monitor's live headline
    // (a compiled binary dumping vm_stat immediately before a screenshot, so
    // ~10ms drift instead of seconds):
    //
    //   capture   AM headline   this (−external)   old (−external−purgeable)
    //   1         20.48 GB      20.38 (off 0.10)   20.22 (off 0.26)
    //   2         19.93 GB      19.73 (off 0.20)   19.15 (off 0.78)
    //
    // The "ghost" ~0.2-1GB the widget was UNDER-reporting was purgeable
    // memory being wrongly counted as cache. Purgeable pages are anonymous
    // app memory the app marked discardable — macOS counts them under App
    // Memory / Used, NOT under Cached Files (which is strictly file-backed =
    // external). Subtracting purgeable in the cached term removed it from
    // "used" a second time, and the error tracked how much purgeable existed
    // (why the gap fluctuated). Component checks in the same atomic captures
    // confirmed the model: internal−purgeable == AM's App Memory, wire_count
    // == AM's Wired, compressor_page_count == AM's Compressed, all to the
    // decimal. The residual ~0.1-0.2GB is free_count sampling jitter (the
    // fastest-moving counter) between our read and AM's — not closable
    // without reading AM's number directly.
    struct MemoryUsage {
        let usedBytes: UInt64
        let totalBytes: UInt64
        let fraction: Double
        let compressedBytes: UInt64
        let wiredBytes: UInt64
    }

    static func memoryUsage() -> MemoryUsage {
        let total = ProcessInfo.processInfo.physicalMemory
        var stats = vm_statistics64()
        var count = mach_msg_type_number_t(MemoryLayout<vm_statistics64_data_t>.size / MemoryLayout<integer_t>.size)
        let result = withUnsafeMutablePointer(to: &stats) { ptr -> kern_return_t in
            ptr.withMemoryRebound(to: integer_t.self, capacity: Int(count)) { intPtr in
                host_statistics64(mach_host_self(), HOST_VM_INFO64, intPtr, &count)
            }
        }
        guard result == KERN_SUCCESS else {
            return MemoryUsage(usedBytes: 0, totalBytes: total, fraction: 0, compressedBytes: 0, wiredBytes: 0)
        }
        let pageSize = UInt64(vm_kernel_page_size)
        let free = UInt64(stats.free_count) * pageSize
        let cached = UInt64(stats.external_page_count) * pageSize
        let used = total > (free + cached) ? total - free - cached : 0
        let fraction = total > 0 ? Double(used) / Double(total) : 0
        let compressed = UInt64(stats.compressor_page_count) * pageSize
        let wired = UInt64(stats.wire_count) * pageSize
        return MemoryUsage(usedBytes: used, totalBytes: total, fraction: min(1, fraction),
                            compressedBytes: compressed, wiredBytes: wired)
    }

    // vm.swapusage via sysctlbyname — the same source `sysctl vm.swapusage`
    // and Activity Monitor's "Swap Used" figure read from.
    struct SwapUsage { let usedBytes: UInt64; let totalBytes: UInt64 }

    static func swapUsage() -> SwapUsage? {
        var usage = xsw_usage()
        var size = MemoryLayout<xsw_usage>.size
        let result = sysctlbyname("vm.swapusage", &usage, &size, nil, 0)
        guard result == 0 else { return nil }
        return SwapUsage(usedBytes: usage.xsu_used, totalBytes: usage.xsu_total)
    }
}
