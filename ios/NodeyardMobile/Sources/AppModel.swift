import Foundation
import LocalAuthentication
import SwiftUI

/// App-wide state: which dashboard to open, and the Face ID lock.
@MainActor
final class AppModel: ObservableObject {
    private enum Key {
        static let server = "nodeyard.server"
        static let faceID = "nodeyard.faceID"
        static let lastPage = "nodeyard.lastPage"
    }

    @Published private(set) var server: URL?
    @Published var faceIDEnabled: Bool { didSet { UserDefaults.standard.set(faceIDEnabled, forKey: Key.faceID) } }
    @Published var locked = false
    @Published var lastPage: String { didSet { UserDefaults.standard.set(lastPage, forKey: Key.lastPage) } }

    private var authenticating = false

    init() {
        let defaults = UserDefaults.standard
        server = defaults.string(forKey: Key.server).flatMap(URL.init(string:))
        faceIDEnabled = defaults.bool(forKey: Key.faceID)
        lastPage = defaults.string(forKey: Key.lastPage) ?? Pages.overview.id
        locked = faceIDEnabled && server != nil
    }

    func setServer(_ url: URL?) {
        server = url
        UserDefaults.standard.set(url?.absoluteString, forKey: Key.server)
    }

    // MARK: - Lock

    /// What the device unlocks with, for labels ("Face ID", "Touch ID", or the passcode).
    var biometryName: String {
        let context = LAContext()
        _ = context.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: nil)
        switch context.biometryType {
        case .faceID: return "Face ID"
        case .touchID: return "Touch ID"
        default: return "Passcode"
        }
    }

    var biometrySymbol: String {
        switch biometryName {
        case "Face ID": return "faceid"
        case "Touch ID": return "touchid"
        default: return "lock.fill"
        }
    }

    var canUseLock: Bool { LAContext().canEvaluatePolicy(.deviceOwnerAuthentication, error: nil) }

    func appWentToBackground() {
        if faceIDEnabled { locked = true }
    }

    /// Ask for Face ID (falls back to the passcode). Returns whether it worked.
    @discardableResult
    func unlock() async -> Bool {
        guard locked, !authenticating else { return !locked }
        authenticating = true
        defer { authenticating = false }
        let ok = await Self.evaluate(reason: "Unlock Nodeyard")
        if ok { withAnimation(.spring(response: 0.45, dampingFraction: 0.9)) { locked = false } }
        return ok
    }

    /// Turning the lock on asks once, so nobody gets locked out by a lock that doesn't work.
    func setFaceID(_ on: Bool) async {
        if !on { faceIDEnabled = false; return }
        if await Self.evaluate(reason: "Turn on \(biometryName) for Nodeyard") { faceIDEnabled = true; Haptics.success() }
        else { faceIDEnabled = false }
    }

    private static func evaluate(reason: String) async -> Bool {
        let context = LAContext()
        context.localizedFallbackTitle = "Use Passcode"
        guard context.canEvaluatePolicy(.deviceOwnerAuthentication, error: nil) else { return true }   // no passcode set: nothing to lock with
        return await withCheckedContinuation { cont in
            context.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: reason) { ok, _ in cont.resume(returning: ok) }
        }
    }

    // MARK: - Server address

    /// Turns what someone typed ("barnaby.tailnet.ts.net", "http://10.0.0.5:9092/") into the dashboard's base URL.
    static func normalize(_ text: String) -> URL? {
        var s = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !s.isEmpty else { return nil }
        if !s.contains("://") { s = "https://" + s }
        guard var parts = URLComponents(string: s), let scheme = parts.scheme?.lowercased(), ["http", "https"].contains(scheme),
              let host = parts.host, !host.isEmpty else { return nil }
        parts.scheme = scheme
        parts.path = ""
        parts.query = nil
        parts.fragment = nil
        return parts.url
    }

    enum CheckResult { case ok, notNodeyard, unreachable(String) }

    /// Is a nodeyard dashboard answering at this address?
    static func check(_ base: URL) async -> CheckResult {
        var request = URLRequest(url: base.appendingPathComponent("login"), cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 10)
        request.httpMethod = "GET"
        do {
            let (_, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse else { return .notNodeyard }
            let serverHeader = (http.value(forHTTPHeaderField: "Server") ?? "").lowercased()
            if serverHeader.contains("nodeyard") || http.statusCode == 200 { return .ok }
            return .notNodeyard
        } catch let error as URLError {
            switch error.code {
            case .cannotFindHost, .dnsLookupFailed:
                return .unreachable("That address doesn't exist from this phone. If it's a tailnet name, turn on Tailscale and try again.")
            case .timedOut, .cannotConnectToHost, .networkConnectionLost, .notConnectedToInternet:
                return .unreachable("Nothing answered at that address. Check the dashboard is running and Tailscale is on.")
            case .secureConnectionFailed, .serverCertificateUntrusted, .serverCertificateHasBadDate, .serverCertificateHasUnknownRoot:
                return .unreachable("The secure connection failed. Try the https://name.ts.net address, or http:// with the port.")
            default:
                return .unreachable(error.localizedDescription)
            }
        } catch {
            return .unreachable(error.localizedDescription)
        }
    }
}
