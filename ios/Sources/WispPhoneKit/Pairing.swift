import Foundation

/// What the phone keeps after scanning the Mac's `wisp-pair://v1?...` QR code.
public struct MacPairing: Codable, Equatable, Sendable {
    public var host: String?
    public var ip: String?
    public var port: Int
    public var macName: String
    /// Stored in the Keychain, never in this struct's persisted form (see AppModel).
    public var token: String

    public init(host: String?, ip: String?, port: Int, macName: String, token: String) {
        self.host = host
        self.ip = ip
        self.port = port
        self.macName = macName
        self.token = token
    }

    /// Base URLs to try, MagicDNS name first. Built from components and re-checked, so a
    /// host can never smuggle a path, fragment or other server into the URL.
    public var baseURLs: [URL] {
        [host, ip].compactMap { $0 }.compactMap { address in
            guard Self.isPrivateHost(address) else { return nil }
            var components = URLComponents()
            components.scheme = "http"
            components.host = address
            components.port = port
            guard let url = components.url, url.host == address, url.port == port,
                  url.path.isEmpty, url.query == nil, url.fragment == nil, url.user == nil else { return nil }
            return url
        }
    }

    /// What the pairing confirmation shows, so the user sees where the token will go.
    public var addressDescription: String {
        "\(host ?? ip ?? "?"):\(port)"
    }

    public enum ParseError: Error, Equatable, LocalizedError {
        case notAPairingLink, missingToken, missingAddress, badPort, unsafeAddress

        public var errorDescription: String? {
            switch self {
            case .notAPairingLink: "That isn't a Wisp pairing code."
            case .missingToken: "The pairing code has no token. Run the pair command on your Mac again."
            case .missingAddress: "The pairing code has no Mac address."
            case .badPort: "The pairing code has an invalid port."
            case .unsafeAddress: "The pairing code points outside your Tailscale network."
            }
        }
    }

    public static func parse(_ text: String) throws -> MacPairing {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let components = URLComponents(string: trimmed),
              components.scheme == "wisp-pair", components.host == "v1" else { throw ParseError.notAPairingLink }
        var query: [String: String] = [:]
        for item in components.queryItems ?? [] { query[item.name] = item.value ?? "" }
        guard let token = query["token"], token.count >= 32,
              token.allSatisfy({ $0.isLetter || $0.isNumber || $0 == "-" || $0 == "_" }) else {
            throw ParseError.missingToken
        }
        let host = query["host"].flatMap { $0.isEmpty ? nil : $0.lowercased() }
        let ip = query["ip"].flatMap { $0.isEmpty ? nil : $0.lowercased() }
        guard host != nil || ip != nil else { throw ParseError.missingAddress }
        // The gateway only ever listens on Tailscale. Refuse anything else, so a
        // doctored code can't point the phone's token at some other server.
        if let host, !isPrivateHost(host) { throw ParseError.unsafeAddress }
        if let ip, !isTailscaleIPv4(ip), !isSimulatorLoopback(ip) { throw ParseError.unsafeAddress }
        if let token = query["token"], token.count > 256 { throw ParseError.missingToken }
        guard let port = Int(query["port"] ?? ""), (1...65535).contains(port) else { throw ParseError.badPort }
        let mac = query["mac"].flatMap { $0.isEmpty ? nil : String($0.prefix(64)) } ?? "your Mac"
        let pairing = MacPairing(host: host, ip: ip, port: port, macName: mac, token: token)
        guard pairing.baseURLs.count == [host, ip].compactMap({ $0 }).count else { throw ParseError.unsafeAddress }
        return pairing
    }

    /// A tailnet name or address. The Simulator also accepts loopback, which is the Mac itself.
    public static func isPrivateHost(_ host: String) -> Bool {
        isMagicDNSName(host) || isTailscaleIPv4(host) || isSimulatorLoopback(host)
    }

    /// `name.tailnet.ts.net`: DNS labels of letters, digits and hyphens only, so nothing
    /// like `1.2.3.4/.ts.net` or `evil.com#.ts.net` can pass as a tailnet name.
    public static func isMagicDNSName(_ host: String) -> Bool {
        guard host.count <= 253, host.hasSuffix(".ts.net") else { return false }
        let labels = host.split(separator: ".", omittingEmptySubsequences: false)
        guard labels.count >= 3 else { return false }
        return labels.allSatisfy { label in
            !label.isEmpty && label.count <= 63 && label.first != "-" && label.last != "-"
                && label.allSatisfy { ($0.isASCII && ($0.isLetter || $0.isNumber)) || $0 == "-" }
        }
    }

    static func isSimulatorLoopback(_ host: String) -> Bool {
        #if targetEnvironment(simulator)
        host == "127.0.0.1" || host == "localhost"
        #else
        false
        #endif
    }

    /// Tailscale hands out IPv4 addresses from the CGNAT range 100.64.0.0/10.
    public static func isTailscaleIPv4(_ text: String) -> Bool {
        let parts = text.split(separator: ".", omittingEmptySubsequences: false)
        guard parts.count == 4, parts.allSatisfy({ (1...3).contains($0.count) && $0.allSatisfy(\.isASCII) && $0.allSatisfy(\.isNumber) })
        else { return false }
        let octets = parts.compactMap { UInt8($0) }
        guard octets.count == 4 else { return false }
        return octets[0] == 100 && (64...127).contains(octets[1])
    }
}
