import Foundation

@main
struct SafariPolicyFixture {
    static func main() {
        let first = UUID(), second = UUID()
        let valid: [String: Any] = ["op": "capabilities", "schema_version": "1.0", "private_context": false]
        let cases: [(Any?, UUID?, String)] = [
            (valid, first, "disabled"),
            (valid, second, "disabled"),
            (valid, nil, "disabled"),
            (["op": "capabilities", "schema_version": "1.0", "private_context": true], first, "private_context"),
            (["op": "capabilities", "schema_version": "1.0", "private_context": 1], first, "invalid_payload"),
            (["op": "capabilities", "schema_version": "1.0", "private_context": false, "profile": first.uuidString], second, "invalid_payload"),
            (["op": "capture", "schema_version": "1.0", "private_context": false], first, "invalid_payload"),
            (nil, first, "invalid_payload")
        ]
        for (index, entry) in cases.enumerated() {
            let result = SafariRequestPolicy.response(entry.0, profile: entry.1)
            guard result == ["status": "denied", "error": entry.2] else {
                fputs("Safari policy case \(index) failed: \(result)\n", stderr)
                exit(1)
            }
        }
        print("Safari policy: \(cases.count) isolated denial cases passed")
    }
}
