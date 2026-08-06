import Foundation

// Shared settings for the reader app. The API key is read from
// ~/.wispair/api_key — the same file the Python service generates — rather
// than being compiled in, so rotating the key never requires a rebuild (and a
// rebuild is expensive here: it changes the code signature and, without the
// stable signing identity, drops every TCC grant).
enum Config {
    static let baseURL = URL(string: "http://127.0.0.1:8767")!

    static var apiKey: String = {
        let path = (NSHomeDirectory() as NSString)
            .appendingPathComponent(".wispair/api_key")
        return (try? String(contentsOfFile: path, encoding: .utf8))?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    }()

    /// A POST with the auth header already attached.
    static func request(_ path: String, method: String = "POST") -> URLRequest {
        var req = URLRequest(url: baseURL.appendingPathComponent(path))
        req.httpMethod = method
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue(apiKey, forHTTPHeaderField: "X-Wisp-Key")
        return req
    }

    static func post(_ path: String, body: [String: Any]) {
        var req = request(path)
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        URLSession.shared.dataTask(with: req) { _, resp, err in
            if let err {
                NSLog("[WispAirReader] POST \(path) failed: \(err.localizedDescription)")
            } else if let http = resp as? HTTPURLResponse, http.statusCode != 200 {
                NSLog("[WispAirReader] POST \(path) → HTTP \(http.statusCode)")
            }
        }.resume()
    }

    /// GET returning parsed JSON on the main queue. Used for the reminder poll.
    static func get(_ path: String, done: @escaping ([String: Any]?) -> Void) {
        let req = request(path, method: "GET")
        URLSession.shared.dataTask(with: req) { data, _, _ in
            guard let data,
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            else { done(nil); return }
            done(json)
        }.resume()
    }
}
