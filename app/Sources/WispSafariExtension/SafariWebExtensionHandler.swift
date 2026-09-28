import Foundation
import SafariServices
import CoreFoundation

// Native Safari context is authoritative for profile identity. A JS claim can
// never turn an unknown profile or a private context into an observation.
enum SafariRequestPolicy {
    static func response(_ message: Any?, profile: UUID?) -> [String: String] {
        guard let message = message as? [String: Any],
              Set(message.keys) == ["op", "schema_version", "private_context"],
              message["op"] as? String == "capabilities",
              message["schema_version"] as? String == "1.0",
              let rawPrivate = message["private_context"],
              CFGetTypeID(rawPrivate as CFTypeRef) == CFBooleanGetTypeID(),
              let privateContext = rawPrivate as? Bool else {
            return ["status": "denied", "error": "invalid_payload"]
        }
        if privateContext { return ["status": "denied", "error": "private_context"] }
        guard profile != nil else { return ["status": "denied", "error": "disabled"] }
        // No host credential, profile registry, or trusted private-mode signal
        // is wired in this stage. The only safe response is unavailable.
        return ["status": "denied", "error": "disabled"]
    }
}

final class SafariWebExtensionHandler: NSObject, NSExtensionRequestHandling {
    func beginRequest(with context: NSExtensionContext) {
        let item = context.inputItems.first as? NSExtensionItem
        let info = item?.userInfo
        let profile = info?[SFExtensionProfileKey] as? UUID
        let reply = NSExtensionItem()
        reply.userInfo = [SFExtensionMessageKey:
            SafariRequestPolicy.response(info?[SFExtensionMessageKey], profile: profile)]
        context.completeRequest(returningItems: [reply], completionHandler: nil)
    }
}
