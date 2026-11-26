import Foundation
import LocalAuthentication
import SwiftUI

/// App-wide state: which dashboard, whether we're signed in, and the Face ID lock.
@MainActor
final class AppModel: ObservableObject {
    private enum Key {
        static let server = "nodeyard.server"
        static let faceID = "nodeyard.faceID"
        static let active = "nodeyard.signedIn"
    }

    @Published private(set) var api: NodeyardAPI?
    @Published var faceIDEnabled: Bool { didSet { UserDefaults.standard.set(faceIDEnabled, forKey: Key.faceID) } }
    @Published var locked = false
    @Published var signedOutReason: String?

    let cluster = ClusterStore()
    let player = MusicPlayer()
    private var authenticating = false

    init() {
        faceIDEnabled = UserDefaults.standard.bool(forKey: Key.faceID)
        if UserDefaults.standard.bool(forKey: Key.active),
           let saved = UserDefaults.standard.string(forKey: Key.server).flatMap(URL.init(string:)) {
            use(NodeyardAPI(base: saved, cookie: Keychain.load(Keychain.cookie(saved)) ?? "", password: Keychain.load(Keychain.password(saved))))
        }
        locked = faceIDEnabled && api != nil
    }

    var server: URL? { api?.base }

    /// Signed in: remember the address and start the live feed.
    func use(_ api: NodeyardAPI) {
        api.onSignedOut = { [weak self] in
            Task { @MainActor in self?.signedOut("The dashboard password changed, or the sign-in ran out. Sign in again.") }
        }
        self.api = api
        signedOutReason = nil
        UserDefaults.standard.set(api.base.absoluteString, forKey: Key.server)
        UserDefaults.standard.set(true, forKey: Key.active)
        player.api = api
        cluster.start(api)
    }

    func signOut() async {
        guard let api else { return }
        player.stop()
        cluster.stop()
        await api.signOut()
        UserDefaults.standard.set(false, forKey: Key.active)
        self.api = nil
    }

    private func signedOut(_ why: String) {
        guard api != nil else { return }
        player.stop()
        cluster.stop()
        signedOutReason = why
        UserDefaults.standard.set(false, forKey: Key.active)
        api = nil
    }

    // MARK: - Lock

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
        if faceIDEnabled && api != nil { locked = true }
    }

    @discardableResult
    func unlock() async -> Bool {
        guard locked, !authenticating else { return !locked }
        authenticating = true
        defer { authenticating = false }
        let ok = await Self.evaluate(reason: "Unlock Nodeyard")
        if ok { withAnimation(.spring(response: 0.45, dampingFraction: 0.9)) { locked = false } }
        return ok
    }

    func setFaceID(_ on: Bool) async {
        if !on { faceIDEnabled = false; return }
        if await Self.evaluate(reason: "Turn on \(biometryName) for Nodeyard") { faceIDEnabled = true; Haptics.success() }
        else { faceIDEnabled = false }
    }

    private static func evaluate(reason: String) async -> Bool {
        let context = LAContext()
        context.localizedFallbackTitle = "Use Passcode"
        guard context.canEvaluatePolicy(.deviceOwnerAuthentication, error: nil) else { return true }
        return await withCheckedContinuation { cont in
            context.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: reason) { ok, _ in cont.resume(returning: ok) }
        }
    }

    // MARK: - Address

    /// "my-mac.tailnet.ts.net" or "http://10.0.0.5:9092/" → the dashboard's base URL.
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
}

/// The live cluster: the dashboard pushes a fresh snapshot every couple of seconds.
@MainActor
final class ClusterStore: ObservableObject {
    @Published private(set) var snapshot: Snapshot?
    @Published private(set) var live = false
    @Published private(set) var problem: String?
    @Published private(set) var cpuHistory: [Sample] = []
    @Published private(set) var memHistory: [Sample] = []

    private(set) var api: NodeyardAPI?
    private var feed: Task<Void, Never>?
    private var history: Task<Void, Never>?

    var state: ClusterState? { snapshot?.state }
    var totals: Totals? { snapshot?.totals }
    var nodes: [Node] { state?.nodes ?? [] }
    var pods: [Pod] { state?.pods ?? [] }
    var alerts: [Alert] { snapshot?.alerts ?? [] }

    func start(_ api: NodeyardAPI) {
        stop()
        self.api = api
        resume()
    }

    /// (Re)connects; called again when the app comes back to the front.
    func resume() {
        guard let api, feed == nil else { return }
        feed = Task { [weak self] in
            var wait: UInt64 = 1
            while !Task.isCancelled {
                do {
                    for try await event in api.events("/api/stream") {
                        guard event.name == "state", let data = event.data.data(using: .utf8),
                              let snap = try? JSONDecoder().decode(Snapshot.self, from: data) else { continue }
                        self?.snapshot = snap
                        self?.live = true
                        self?.problem = nil
                        wait = 1
                    }
                } catch let e as APIError where e.status == 401 {
                    self?.live = false
                    return
                } catch {
                    self?.problem = error.localizedDescription
                    // the feed is down: one plain read so the screens still fill
                    if self?.snapshot == nil, let snap = try? await api.get("/api/state", as: Snapshot.self) { self?.snapshot = snap }
                }
                self?.live = false
                try? await Task.sleep(nanoseconds: wait * 1_000_000_000)
                wait = min(wait * 2, 15)
            }
        }
        history = Task { [weak self] in
            while !Task.isCancelled {
                await self?.loadHistory()
                try? await Task.sleep(nanoseconds: 30_000_000_000)
            }
        }
    }

    /// Stops reading while the app is in the background (saves battery and data).
    func pause() {
        feed?.cancel(); feed = nil
        history?.cancel(); history = nil
        live = false
    }

    func stop() {
        pause()
        api = nil
        snapshot = nil
        cpuHistory = []
        memHistory = []
        problem = nil
    }

    private func loadHistory() async {
        guard let api, let reply = try? await api.get("/api/history", ["points": "180"], as: HistoryReply.self) else { return }
        let points = reply.points ?? []
        let cpuTotal = totals?.cpu_total ?? nodes.compactMap(\.cpu_cores).reduce(0, +)
        let memTotal = totals?.mem_total ?? nodes.compactMap(\.mem_total).reduce(0, +)
        cpuHistory = points.map { p in
            let used = (p.nodes ?? [:]).values.compactMap { $0.first ?? nil }.reduce(0, +)
            return Sample(time: Date(timeIntervalSince1970: p.t), value: cpuTotal > 0 ? min(1, used / cpuTotal) : 0)
        }
        memHistory = points.map { p in
            let used = (p.nodes ?? [:]).values.compactMap { $0.count > 1 ? $0[1] : nil }.reduce(0, +)
            return Sample(time: Date(timeIntervalSince1970: p.t), value: memTotal > 0 ? min(1, used / memTotal) : 0)
        }
    }
}
