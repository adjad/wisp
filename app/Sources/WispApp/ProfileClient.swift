import Foundation

/// The profile calls (service/memory/profile.py), kept out of WispClient.swift
/// because several contract checks compile that file on its own, without
/// KnowYouFlow.swift's ProfileEnvelope.
extension WispClient {
    // --- the user's profile (service/memory/profile.py) ------------------------
    // Getting to know you's answers. Every call returns the whole envelope
    // (profile, status, preview), so a screen never needs a second request.

    struct ProfileResult {
        let envelope: ProfileEnvelope?
        let error: String?
    }

    func profile() async -> ProfileEnvelope? { (await profileRequest("GET")).envelope }

    func patchProfile(_ patch: [String: Any]) async -> ProfileResult {
        await profileRequest("PATCH", body: patch)
    }

    func completeProfile() async -> ProfileEnvelope? { (await profileRequest("POST", path: "profile/complete")).envelope }
    func dismissProfile() async -> ProfileEnvelope? { (await profileRequest("POST", path: "profile/dismiss")).envelope }
    func eraseProfile() async -> ProfileEnvelope? { (await profileRequest("DELETE")).envelope }

    private func profileRequest(_ method: String, path: String = "profile",
                                body: [String: Any]? = nil) async -> ProfileResult {
        var request = URLRequest(url: Self.baseURL.appendingPathComponent(path))
        request.httpMethod = method
        request.timeoutInterval = 15
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        }
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              let http = response as? HTTPURLResponse else {
            return ProfileResult(envelope: nil, error: "Wisp's service didn't answer. Your answer is still here; try again.")
        }
        let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        guard (200..<300).contains(http.statusCode) else {
            return ProfileResult(envelope: nil, error: object?["detail"] as? String ?? "Couldn't save that.")
        }
        guard let object, let envelope = ProfileEnvelope(json: object) else {
            return ProfileResult(envelope: nil, error: "Unexpected answer from Wisp's service.")
        }
        return ProfileResult(envelope: envelope, error: nil)
    }
}
