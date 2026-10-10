import Foundation

enum ManagementError: LocalizedError {
    case signInNeeded
    case wrongPassword
    case rateLimited(Int)
    case http(Int, String)
    case badReply

    var errorDescription: String? {
        switch self {
        case .signInNeeded: "Sign in with the dashboard password (Settings › Management) to manage the cluster from this Mac."
        case .wrongPassword: "That isn't the dashboard password. It is the password you use on the website's sign-in page, not the server API key."
        case .rateLimited(let seconds): "Too many wrong passwords. The dashboard accepts another try in \(seconds) seconds."
        case .http(let status, let message): message.isEmpty ? "The dashboard answered HTTP \(status)." : message
        case .badReply: "The dashboard sent a reply this app couldn't read. Is the address the Nodeyard dashboard (port 9092)?"
        }
    }
}

/// The website's own API (/api/...): the same data and actions as the dashboard pages, behind the dashboard's
/// sign-in. The session cookie lives only in this session's memory; the password (if saved) is in Keychain and is
/// used to sign in again when the session expires. Model status and automatic unloading use the server API key
/// (/api/v1/...), so those work without the password.
@MainActor
final class ManagementClient {
    var baseAddress: String
    var apiKey: String
    var password: String
    private let session: URLSession

    init(baseAddress: String, apiKey: String, password: String) {
        self.baseAddress = baseAddress
        self.apiKey = apiKey
        self.password = password
        let config = URLSessionConfiguration.ephemeral          // the session cookie never touches the disk
        config.timeoutIntervalForRequest = 20
        config.timeoutIntervalForResource = 120
        config.httpShouldSetCookies = true
        config.httpCookieAcceptPolicy = .onlyFromMainDocumentDomain
        session = URLSession(configuration: config)
    }

    private func url(_ path: String, query: [URLQueryItem] = []) throws -> URL {
        guard let base = URL(string: baseAddress.trimmingCharacters(in: .whitespacesAndNewlines)),
              let scheme = base.scheme?.lowercased(), ["http", "https"].contains(scheme), base.host != nil,
              var parts = URLComponents(url: base, resolvingAgainstBaseURL: false) else { throw NodeyardError.invalidAddress }
        let root = parts.path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        parts.path = "/" + ([root, path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))].filter { !$0.isEmpty }.joined(separator: "/"))
        parts.queryItems = query.isEmpty ? nil : query
        guard let result = parts.url else { throw NodeyardError.invalidAddress }
        return result
    }

    private func request(_ method: String, _ path: String, body: [String: Any]? = nil, query: [URLQueryItem] = [], bearer: Bool = false) throws -> URLRequest {
        var req = URLRequest(url: try url(path, query: query))
        req.httpMethod = method
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        req.setValue("NodeyardAI/1.1", forHTTPHeaderField: "User-Agent")
        req.setValue("1", forHTTPHeaderField: "X-Nodeyard")          // the dashboard's cross-site request guard
        if bearer {
            guard !apiKey.isEmpty else { throw NodeyardError.server("Add the server API key in Settings to connect.") }
            req.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        }
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        return req
    }

    private func send(_ req: URLRequest) async throws -> (Int, JSON?) {
        let (data, response) = try await session.data(for: req)
        guard let http = response as? HTTPURLResponse else { throw ManagementError.badReply }
        return (http.statusCode, JSON.parse(data))
    }

    /// Signs in to the dashboard with its password; the session cookie is kept for later requests.
    func signIn(password: String) async throws {
        let (status, reply) = try await send(try request("POST", "/api/login", body: ["password": password]))
        switch status {
        case 200 where reply?["ok"].bool == true || reply?["enabled"].bool == false: return
        case 401: throw ManagementError.wrongPassword
        case 429: throw ManagementError.rateLimited(reply?["wait"].int ?? 60)
        default: throw ManagementError.http(status, reply?["error"].text ?? "")
        }
    }

    func signOut() async {
        _ = try? await send(try request("POST", "/api/logout", body: [:]))
        session.configuration.httpCookieStorage?.cookies?.forEach { session.configuration.httpCookieStorage?.deleteCookie($0) }
    }

    /// Does the dashboard need a sign-in, and is this session signed in?
    func authStatus() async throws -> (enabled: Bool, signedIn: Bool) {
        let (status, reply) = try await send(try request("GET", "/api/auth"))
        guard status == 200, let reply else { throw ManagementError.http(status, reply?["error"].text ?? "") }
        return (reply["enabled"].bool ?? true, reply["authenticated"].bool ?? false)
    }

    /// A website API call; signs in again once (with the saved password) when the session has expired.
    func call(_ method: String, _ path: String, body: [String: Any]? = nil, query: [URLQueryItem] = []) async throws -> JSON {
        var (status, reply) = try await send(try request(method, path, body: body, query: query))
        if status == 401 {
            guard !password.isEmpty else { throw ManagementError.signInNeeded }
            try await signIn(password: password)
            (status, reply) = try await send(try request(method, path, body: body, query: query))
            if status == 401 { throw ManagementError.signInNeeded }
        }
        guard let reply else { throw ManagementError.badReply }
        guard (200..<300).contains(status) else { throw ManagementError.http(status, reply["error"].text) }
        if reply["ok"].bool == false { throw ManagementError.http(status, reply["error"].text) }
        return reply
    }

    /// A control API call with the server API key (works without the dashboard password).
    func keyed(_ method: String, _ path: String, body: [String: Any]? = nil) async throws -> JSON {
        let (status, reply) = try await send(try request(method, path, body: body, bearer: true))
        guard let reply else { throw ManagementError.badReply }
        guard (200..<300).contains(status), reply["ok"].bool != false else { throw ManagementError.http(status, reply["error"].text) }
        return reply
    }

    // MARK: the website's data and actions

    func state() async throws -> JSON { try await call("GET", "/api/state") }
    func diskModels(refresh: Bool) async throws -> JSON { try await call("GET", "/api/ai/models", query: refresh ? [URLQueryItem(name: "refresh", value: "1")] : []) }
    func ollama() async throws -> JSON { try await call("GET", "/api/ai/ollama") }
    func targets() async throws -> JSON { try await call("GET", "/api/ai/targets") }
    func doctor(fresh: Bool) async throws -> JSON { try await call("GET", "/api/doctor", query: fresh ? [URLQueryItem(name: "fresh", value: "1")] : []) }
    func settings() async throws -> JSON { try await call("GET", "/api/settings") }
    func agents(node: String = "") async throws -> JSON { try await call("GET", "/api/agents", query: node.isEmpty ? [] : [URLQueryItem(name: "node", value: node)]) }
    func logs(namespace: String, pod: String, container: String = "", lines: Int = 300) async throws -> String {
        var query = [URLQueryItem(name: "ns", value: namespace), URLQueryItem(name: "pod", value: pod), URLQueryItem(name: "lines", value: String(lines))]
        if !container.isEmpty { query.append(URLQueryItem(name: "container", value: container)) }
        let reply = try await call("GET", "/api/logs", query: query)
        return reply["text"].string ?? reply["logs"].string ?? ""
    }

    /// Starts one of the dashboard's allowed nodeyard commands; returns the task id to follow.
    func run(_ action: String, params: [String: Any] = [:]) async throws -> String {
        var body = params
        body["action"] = action
        let reply = try await call("POST", "/api/run", body: body)
        guard let job = reply["job"].string, !job.isEmpty else { throw ManagementError.badReply }
        return job
    }
    func job(_ id: String, since: Int) async throws -> JSON { try await call("GET", "/api/job", query: [URLQueryItem(name: "id", value: id), URLQueryItem(name: "since", value: String(since))]) }
    func cancelJob(_ id: String) async throws { _ = try await call("POST", "/api/job/cancel", body: ["id": id]) }
    func ollamaLoad(pod: String, model: String, load: Bool) async throws { _ = try await call("POST", "/api/ai/ollama-load", body: ["pod": pod, "model": model, "load": load]) }

    func lifecycle() async throws -> JSON { try await keyed("GET", "/api/v1/lifecycle") }
    func setLifecycle(enabled: Bool? = nil, idleSeconds: Int? = nil) async throws -> JSON {
        var body: [String: Any] = [:]
        if let enabled { body["enabled"] = enabled }
        if let idleSeconds { body["idle_seconds"] = idleSeconds }
        return try await keyed("POST", "/api/v1/lifecycle", body: body)
    }
}
