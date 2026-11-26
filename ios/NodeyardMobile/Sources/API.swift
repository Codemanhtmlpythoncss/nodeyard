import Foundation
import Security

struct APIError: LocalizedError {
    let message: String
    var status = 0
    var errorDescription: String? { message }
    static let signedOut = APIError(message: "Sign in again to keep going.", status: 401)
}

/// Talks to the nodeyard dashboard's JSON API the same way its web page does: a sign-in cookie plus the
/// X-Nodeyard header the server asks every write to carry.
final class NodeyardAPI: @unchecked Sendable {
    let base: URL
    private let session: URLSession
    private let streaming: URLSession
    private let lock = NSLock()
    private var cookie: String                       // "nodeyard_session=…; nodeyard_remember=…"
    private var password: String?
    private var relogin: Task<Void, Error>?
    var onSignedOut: (@Sendable () -> Void)?

    init(base: URL, cookie: String = "", password: String? = nil) {
        self.base = base
        self.cookie = cookie
        self.password = password
        let config = URLSessionConfiguration.default
        config.httpShouldSetCookies = false          // cookies are sent by hand: the server's are SameSite=Strict
        config.httpCookieAcceptPolicy = .never
        config.timeoutIntervalForRequest = 20
        config.waitsForConnectivity = false
        session = URLSession(configuration: config)
        let long = URLSessionConfiguration.default
        long.httpShouldSetCookies = false
        long.httpCookieAcceptPolicy = .never
        long.timeoutIntervalForRequest = 120         // live streams send a ping every 15 s
        long.timeoutIntervalForResource = 7 * 24 * 3600
        streaming = URLSession(configuration: long)
    }

    var cookieHeader: String { lock.withLock { cookie } }
    var savedPassword: String? { lock.withLock { password } }

    // MARK: - Requests

    func url(_ path: String, _ query: [String: String] = [:]) -> URL {
        var parts = URLComponents(url: base, resolvingAgainstBaseURL: false)!
        parts.path = path
        parts.queryItems = query.isEmpty ? nil : query.sorted { $0.key < $1.key }.map { URLQueryItem(name: $0.key, value: $0.value) }
        return parts.url!
    }

    func request(_ path: String, query: [String: String] = [:], body: Any? = nil) -> URLRequest {
        var req = URLRequest(url: url(path, query))
        req.setValue("1", forHTTPHeaderField: "X-Nodeyard")
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        req.setValue("NodeyardMobile/1.0", forHTTPHeaderField: "User-Agent")
        let c = cookieHeader
        if !c.isEmpty { req.setValue(c, forHTTPHeaderField: "Cookie") }
        if let body {
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        }
        return req
    }

    /// One request; a 401 signs in again with the saved password and tries once more.
    func send(_ path: String, query: [String: String] = [:], body: Any? = nil) async throws -> Data {
        let (data, http) = try await raw(request(path, query: query, body: body))
        if http.statusCode == 401, path != "/api/login" {
            try await signInAgain()
            let (again, http2) = try await raw(request(path, query: query, body: body))
            try Self.check(again, http2)
            return again
        }
        try Self.check(data, http)
        return data
    }

    func get<T: Decodable>(_ path: String, _ query: [String: String] = [:], as: T.Type = T.self) async throws -> T {
        try Self.decode(T.self, try await send(path, query: query))
    }

    func post<T: Decodable>(_ path: String, _ body: [String: Any], as: T.Type = T.self) async throws -> T {
        try Self.decode(T.self, try await send(path, body: body))
    }

    @discardableResult
    func post(_ path: String, _ body: [String: Any]) async throws -> [String: Any] {
        let data = try await send(path, body: body)
        return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
    }

    private func raw(_ req: URLRequest) async throws -> (Data, HTTPURLResponse) {
        do {
            let (data, response) = try await session.data(for: req)
            guard let http = response as? HTTPURLResponse else { throw APIError(message: "The server's answer wasn't HTTP.") }
            remember(http)
            return (data, http)
        } catch let e as URLError {
            throw APIError(message: Self.explain(e))
        }
    }

    static func check(_ data: Data, _ http: HTTPURLResponse) throws {
        guard (200..<300).contains(http.statusCode) else {
            let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            let msg = (json?["error"] as? String) ?? (json?["error"] as? [String: Any])?["message"] as? String
            throw APIError(message: msg ?? "The dashboard answered \(http.statusCode).", status: http.statusCode)
        }
    }

    static func decode<T: Decodable>(_ type: T.Type, _ data: Data) throws -> T {
        do { return try JSONDecoder().decode(T.self, from: data) }
        catch { throw APIError(message: "The dashboard sent something this app doesn't understand. Update nodeyard on the server.") }
    }

    static func explain(_ e: URLError) -> String {
        switch e.code {
        case .cannotFindHost, .dnsLookupFailed:
            return "Can't find that address from this phone. If it's a tailnet name, turn on Tailscale."
        case .timedOut, .cannotConnectToHost, .networkConnectionLost:
            return "Nothing is answering. Check the dashboard is running and Tailscale is on."
        case .notConnectedToInternet:
            return "This phone is offline."
        case .secureConnectionFailed, .serverCertificateUntrusted, .serverCertificateHasBadDate, .serverCertificateHasUnknownRoot:
            return "The secure connection failed. Try the https://….ts.net address, or http:// with the port."
        case .cancelled:
            return "Cancelled."
        default:
            return e.localizedDescription
        }
    }

    // MARK: - Live streams (server-sent events)

    struct Event: Sendable { let name: String; let data: String }

    /// Reads a text/event-stream until it ends. Throws APIError.signedOut on 401.
    func events(_ path: String, _ query: [String: String] = [:]) -> AsyncThrowingStream<Event, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    var req = request(path, query: query)
                    req.setValue("text/event-stream", forHTTPHeaderField: "Accept")
                    req.setValue("identity", forHTTPHeaderField: "Accept-Encoding")   // each event arrives at once
                    var (bytes, response) = try await streaming.bytes(for: req)
                    var http = response as? HTTPURLResponse
                    if http?.statusCode == 401 {
                        try await signInAgain()
                        req.setValue(cookieHeader, forHTTPHeaderField: "Cookie")
                        (bytes, response) = try await streaming.bytes(for: req)
                        http = response as? HTTPURLResponse
                    }
                    guard let http, (200..<300).contains(http.statusCode) else {
                        throw APIError(message: "The live feed was refused (\(http?.statusCode ?? 0)).", status: http?.statusCode ?? 0)
                    }
                    var name = "message"
                    for try await line in bytes.lines {
                        if line.hasPrefix("event:") {
                            name = line.dropFirst(6).trimmingCharacters(in: .whitespaces)
                        } else if line.hasPrefix("data:") {
                            continuation.yield(Event(name: name, data: String(line.dropFirst(5)).trimmingCharacters(in: .whitespaces)))
                            name = "message"
                        }
                    }
                    continuation.finish()
                } catch let e as URLError {
                    continuation.finish(throwing: APIError(message: Self.explain(e)))
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    /// Lines of a streamed POST (the AI's OpenAI-style answer).
    func streamLines(_ path: String, body: [String: Any]) async throws -> URLSession.AsyncBytes {
        var req = request(path, body: body)
        req.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        req.setValue("identity", forHTTPHeaderField: "Accept-Encoding")
        var (bytes, response) = try await streaming.bytes(for: req)
        if (response as? HTTPURLResponse)?.statusCode == 401 {
            try await signInAgain()
            req.setValue(cookieHeader, forHTTPHeaderField: "Cookie")
            (bytes, response) = try await streaming.bytes(for: req)
        }
        guard let http = response as? HTTPURLResponse else { throw APIError(message: "The server's answer wasn't HTTP.") }
        if !(200..<300).contains(http.statusCode) {
            var text = ""
            for try await line in bytes.lines { text += line; if text.count > 2000 { break } }
            try Self.check(Data(text.utf8), http)
        }
        return bytes
    }

    // MARK: - Signing in

    struct AuthState: Decodable { let enabled: Bool; let authenticated: Bool }

    func authState() async throws -> AuthState {
        let (data, http) = try await raw(request("/api/auth"))
        try Self.check(data, http)
        return try Self.decode(AuthState.self, data)
    }

    func signIn(password: String) async throws {
        let (data, http) = try await raw(request("/api/login", body: ["password": password]))
        try Self.check(data, http)
        lock.withLock { self.password = password }
        Keychain.save(password, for: Keychain.password(base))
    }

    /// After the password changed from this phone: sign in again with the new one later.
    func rememberPassword(_ password: String) {
        lock.withLock { self.password = password }
        Keychain.save(password, for: Keychain.password(base))
    }

    func signOut() async {
        _ = try? await raw(request("/api/logout", body: [:]))
        lock.withLock { cookie = ""; password = nil }
        Keychain.delete(Keychain.password(base))
        Keychain.delete(Keychain.cookie(base))
    }

    private func signInAgain() async throws {
        let task: Task<Void, Error> = lock.withLock {
            if let relogin { return relogin }
            let pw = password
            let t = Task<Void, Error> {
                guard let pw, !pw.isEmpty else { throw APIError.signedOut }
                do { try await self.signIn(password: pw) } catch { throw APIError.signedOut }
            }
            relogin = t
            return t
        }
        defer { lock.withLock { relogin = nil } }
        do { try await task.value } catch { onSignedOut?(); throw error }
    }

    /// Keeps the sign-in cookies the server sets.
    private func remember(_ http: HTTPURLResponse) {
        guard let headers = http.allHeaderFields as? [String: String], headers.keys.contains(where: { $0.lowercased() == "set-cookie" }) else { return }
        let fresh = HTTPCookie.cookies(withResponseHeaderFields: headers, for: base)
        guard !fresh.isEmpty else { return }
        let value: String = lock.withLock {
            var jar = Dictionary(uniqueKeysWithValues: cookie.split(separator: ";").compactMap { part -> (String, String)? in
                let kv = part.split(separator: "=", maxSplits: 1).map { $0.trimmingCharacters(in: .whitespaces) }
                return kv.count == 2 ? (kv[0], kv[1]) : nil
            })
            for c in fresh { if c.value.isEmpty { jar[c.name] = nil } else { jar[c.name] = c.value } }
            cookie = jar.sorted { $0.key < $1.key }.map { "\($0.key)=\($0.value)" }.joined(separator: "; ")
            return cookie
        }
        Keychain.save(value, for: Keychain.cookie(base))
    }
}

/// The dashboard password and sign-in cookie live in the Keychain, never in plain settings.
enum Keychain {
    static func password(_ base: URL) -> String { "password:" + base.absoluteString }
    static func cookie(_ base: URL) -> String { "cookie:" + base.absoluteString }

    private static func query(_ account: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: "com.nodeyard.mobile", kSecAttrAccount as String: account]
    }

    static func save(_ value: String, for account: String) {
        SecItemDelete(query(account) as CFDictionary)
        var q = query(account)
        q[kSecValueData as String] = Data(value.utf8)
        q[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        SecItemAdd(q as CFDictionary, nil)
    }

    static func load(_ account: String) -> String? {
        var q = query(account)
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var out: AnyObject?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess, let data = out as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    static func delete(_ account: String) { SecItemDelete(query(account) as CFDictionary) }
}
