import Foundation

enum NodeyardError: LocalizedError {
    case invalidAddress
    case http(Int, String)
    case server(String)
    case noModel

    var errorDescription: String? {
        switch self {
        case .invalidAddress: "Enter a valid server address, such as https://nodeyard.example:9092."
        case .http(let status, let message): "The server returned HTTP \(status): \(message)"
        case .server(let message): message
        case .noModel: "No ready AI model is available. Start a downloaded model in the Models panel."
        }
    }
}

@MainActor
final class NodeyardClient {
    var baseAddress: String
    var apiKey: String
    private let session: URLSession

    init(baseAddress: String, apiKey: String) {
        self.baseAddress = baseAddress
        self.apiKey = apiKey
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 30
        config.timeoutIntervalForResource = 900
        config.waitsForConnectivity = true
        self.session = URLSession(configuration: config)
    }

    private func url(_ path: String, query: [URLQueryItem] = []) throws -> URL {
        guard let base = URL(string: baseAddress.trimmingCharacters(in: .whitespacesAndNewlines)),
              let scheme = base.scheme?.lowercased(), ["http", "https"].contains(scheme), base.host != nil else {
            throw NodeyardError.invalidAddress
        }
        var components = URLComponents(url: base, resolvingAgainstBaseURL: false)
        let root = components?.path.trimmingCharacters(in: CharacterSet(charactersIn: "/")) ?? ""
        components?.path = "/" + ([root, path].filter { !$0.isEmpty }.joined(separator: "/"))
        if !query.isEmpty { components?.queryItems = query }
        guard let result = components?.url else { throw NodeyardError.invalidAddress }
        return result
    }

    private func request(_ method: String, _ path: String, body: Any? = nil, query: [URLQueryItem] = []) throws -> URLRequest {
        guard !apiKey.isEmpty else { throw NodeyardError.server("Add the server API key in Settings to connect.") }
        var req = URLRequest(url: try url(path, query: query))
        req.httpMethod = method
        req.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        req.setValue("NodeyardAI/1.0", forHTTPHeaderField: "User-Agent")
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: body, options: [.fragmentsAllowed, .sortedKeys])
        }
        return req
    }

    static func suggestedDashboardAddress(from value: String) -> String? {
        guard var parts = URLComponents(string: value.trimmingCharacters(in: .whitespacesAndNewlines)), parts.host != nil else { return nil }
        if let migrated = migratedDashboardAddress(from: value) { return migrated }
        if parts.port == 9092, parts.scheme?.lowercased() == "https" {
            parts.scheme = "http"
            return parts.string
        }
        return nil
    }

    static func migratedDashboardAddress(from value: String) -> String? {
        guard var parts = URLComponents(string: value.trimmingCharacters(in: .whitespacesAndNewlines)), parts.host != nil, parts.port == 31435 else { return nil }
        parts.scheme = "http"
        parts.port = 9092
        parts.path = ""
        parts.query = nil
        parts.fragment = nil
        return parts.string
    }

    private func explainNetworkError(_ error: Error) -> Error {
        guard let urlError = error as? URLError else { return error }
        guard [.secureConnectionFailed, .serverCertificateHasBadDate, .serverCertificateUntrusted,
               .serverCertificateHasUnknownRoot, .serverCertificateNotYetValid].contains(urlError.code) else { return error }
        let parts = URLComponents(string: baseAddress)
        let host = parts?.host ?? "your server"
        if parts?.port == 31435 {
            return NodeyardError.server("This address points to the model API on port 31435. Nodeyard AI needs the dashboard API instead. In Settings, change Server address to http://\(host):9092 (or your dashboard's address). Keep the same server-wide API key.")
        }
        if parts?.port == 9092, parts?.scheme?.lowercased() == "https" {
            return NodeyardError.server("The Nodeyard dashboard on port 9092 serves HTTP, not HTTPS. In Settings, change Server address to http://\(host):9092. Use this on your private LAN or Tailscale; for public access, use a valid HTTPS dashboard address.")
        }
        return NodeyardError.server("The secure connection to \(host) failed because the certificate or host name could not be verified. Use the HTTPS address that matches the server's certificate, or use HTTP only on a private, trusted network.")
    }

    private func get<T: Decodable>(_ path: String, query: [URLQueryItem] = [], as type: T.Type) async throws -> T {
        do {
            let (data, response) = try await session.data(for: request("GET", path, query: query))
            try check(response, data)
            return try JSONDecoder().decode(type, from: data)
        } catch { throw explainNetworkError(error) }
    }

    private func post<T: Decodable>(_ path: String, body: Any, as type: T.Type) async throws -> T {
        do {
            let (data, response) = try await session.data(for: request("POST", path, body: body))
            try check(response, data)
            return try JSONDecoder().decode(type, from: data)
        } catch { throw explainNetworkError(error) }
    }

    private func check(_ response: URLResponse, _ data: Data) throws {
        guard let http = response as? HTTPURLResponse else { throw NodeyardError.server("The server response was not HTTP.") }
        guard (200..<300).contains(http.statusCode) else {
            let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            throw NodeyardError.http(http.statusCode, json?["error"] as? String ?? String(data: data, encoding: .utf8) ?? "Request failed.")
        }
    }

    func testConnection() async throws -> [ModelTarget] {
        try await get("/api/v1/targets", as: TargetsReply.self).targets
    }

    func targets() async throws -> [ModelTarget] {
        try await get("/api/v1/targets", as: TargetsReply.self).targets
    }

    func models() async throws -> ModelInventory {
        try await get("/api/v1/models", as: ModelInventory.self)
    }

    func loadModel(_ model: ModelInventoryRow, context: Int) async throws {
        let name = model.file.isEmpty ? model.name : model.file
        _ = try await post("/api/v1/models/load", body: ["model": name, "ctx": context], as: BasicReply.self)
    }

    func streamChat(model: String, messages: [OpenAIMessage], temperature: Double, maxTokens: Int,
                    onContent: @escaping (String) -> Void, onReasoning: @escaping (String) -> Void) async throws -> StreamStats {
        let payload = StreamRequest(model: model, messages: messages, temperature: temperature, max_tokens: maxTokens == 0 ? nil : maxTokens)
        let encoder = JSONEncoder()
        let data = try encoder.encode(payload)
        var req = try request("POST", "/api/v1/chat/completions")
        req.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        req.httpBody = data
        let bytes: URLSession.AsyncBytes
        let response: URLResponse
        do { (bytes, response) = try await session.bytes(for: req) }
        catch { throw explainNetworkError(error) }
        guard let http = response as? HTTPURLResponse else { throw NodeyardError.server("The server response was not HTTP.") }
        guard (200..<300).contains(http.statusCode) else {
            var errorText = "Request failed."
            for try await line in bytes.lines {
                if !line.isEmpty { errorText = line; break }
            }
            if let range = errorText.range(of: "\"error\"\\s*:\\s*\"", options: .regularExpression) {
                errorText = String(errorText[range.upperBound...]).trimmingCharacters(in: CharacterSet(charactersIn: "\"}"))
            }
            throw NodeyardError.http(http.statusCode, errorText)
        }
        var stats = StreamStats()
        do {
            for try await line in bytes.lines {
                try Task.checkCancellation()
                guard line.hasPrefix("data: ") else { continue }
                let chunk = String(line.dropFirst(6))
                if chunk == "[DONE]" { break }
                guard let json = chunk.data(using: .utf8), let part = try? JSONDecoder().decode(StreamDelta.self, from: json) else { continue }
                if let usage = part.usage?.completion_tokens { stats.tokens = usage }
                if let speed = part.timings?.predicted_per_second { stats.tokensPerSecond = speed }
                for choice in part.choices ?? [] {
                    if let thought = choice.delta.reasoning_content ?? choice.delta.reasoning, !thought.isEmpty { onReasoning(thought) }
                    if let text = choice.delta.content, !text.isEmpty { onContent(text) }
                }
            }
        } catch { throw explainNetworkError(error) }
        return stats
    }

    func remoteChats() async throws -> [RemoteChatSummary] {
        try await get("/api/v1/chats", as: ChatListReply.self).chats
    }

    func remoteChat(_ id: String) async throws -> RemoteChatReply {
        try await get("/api/v1/chat", query: [URLQueryItem(name: "id", value: id)], as: RemoteChatReply.self)
    }

    func saveRemote(_ chat: ChatRecord) async throws {
        let messages = chat.messages.map { ["role": $0.role, "content": $0.content] }
        let remoteID = chat.id.hasPrefix("mac-") ? chat.id : "mac-" + chat.id
        _ = try await post("/api/v1/chats/save", body: ["chat": ["id": remoteID, "title": chat.title, "source": "nodeyard-macos",
                                                                       "model": chat.modelName, "messages": messages]], as: BasicReply.self)
    }

    func deleteRemote(_ id: String) async throws {
        _ = try await post("/api/v1/chats/delete", body: ["id": id], as: BasicReply.self)
    }

    private struct TargetsReply: Decodable { var targets: [ModelTarget] }
    struct ChatListReply: Decodable { var chats: [RemoteChatSummary] }
    struct RemoteChatReply: Decodable { var chat: RemoteChatBody }
    struct RemoteChatBody: Decodable { var id: String; var title: String; var model: String; var messages: [RemoteMessage] }
    struct RemoteMessage: Decodable { var role: String; var content: String }
    private struct BasicReply: Decodable { var ok: Bool?; var error: String?; var job: String? }
    private struct StreamRequest: Encodable {
        var model: String
        var messages: [OpenAIMessage]
        var temperature: Double
        var max_tokens: Int?
    }
}

struct StreamStats { var tokens: Int?; var tokensPerSecond: Double? }
