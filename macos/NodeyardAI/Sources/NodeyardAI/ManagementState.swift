import Foundation
import SwiftUI

/// The sections of the website this app mirrors (the website's own sidebar order), plus the setup guide.
enum ManageSection: String, CaseIterable, Identifiable {
    case overview, nodes, pods, workloads, network, storage, hardware, models, events, alerts, doctor, tasks, guide
    var id: String { rawValue }
    var title: String {
        switch self {
        case .overview: "Overview"
        case .nodes: "Nodes"
        case .pods: "Pods"
        case .workloads: "Workloads"
        case .network: "Network"
        case .storage: "Storage"
        case .hardware: "Hardware"
        case .models: "AI models"
        case .events: "Events"
        case .alerts: "Alerts"
        case .doctor: "Doctor"
        case .tasks: "Tasks"
        case .guide: "Setup guide"
        }
    }
    var icon: String {
        switch self {
        case .overview: "square.grid.2x2"
        case .nodes: "server.rack"
        case .pods: "shippingbox"
        case .workloads: "square.stack.3d.up"
        case .network: "network"
        case .storage: "externaldrive"
        case .hardware: "cpu"
        case .models: "sparkles"
        case .events: "list.bullet.rectangle"
        case .alerts: "exclamationmark.triangle"
        case .doctor: "stethoscope"
        case .tasks: "checklist"
        case .guide: "book"
        }
    }
}

/// A nodeyard command started from this app, followed until it ends.
struct TrackedJob: Identifiable, Equatable {
    var id: String
    var title: String
    var status = "running"
    var lines: [String] = []
    var next = 0
    var started = Date()
    var cancelable = false
    var rc: Int?
}

@MainActor
final class ManagementState: ObservableObject {
    @Published var section: ManageSection = .overview
    @Published var snapshot: JSON?
    @Published var updated: Date?
    @Published var error: String?
    @Published var authEnabled = true
    @Published var signedIn = false
    @Published var signingIn = false
    @Published var disk: JSON?
    @Published var ollama: JSON?
    @Published var lifecycle: JSON?
    @Published var lifecycleError: String?
    @Published var lifecycleSaving = false
    @Published var doctor: JSON?
    @Published var doctorLoading = false
    @Published var jobs: [TrackedJob] = []
    @Published var shownJobID: String?
    @Published var info: String?
    @Published var savedPassword: Bool

    private var client: ManagementClient
    private var pollTask: Task<Void, Never>?
    private var jobTasks: [String: Task<Void, Never>] = [:]
    private var refreshing = false

    init(address: String, apiKey: String) {
        let password = KeychainStore.readDashboardPassword()
        savedPassword = !password.isEmpty
        client = ManagementClient(baseAddress: address, apiKey: apiKey, password: password)
    }

    func reconnect(address: String, apiKey: String) {
        let password = client.password
        client = ManagementClient(baseAddress: address, apiKey: apiKey, password: password)
        signedIn = false
        snapshot = nil
        Task { await refresh() }
    }

    // MARK: sign-in

    func signIn(password: String, remember: Bool) async {
        signingIn = true
        defer { signingIn = false }
        do {
            try await client.signIn(password: password)
            client.password = password
            if remember {
                try? KeychainStore.writeDashboardPassword(password)
                savedPassword = true
            }
            signedIn = true
            error = nil
            await refresh()
        } catch {
            signedIn = false
            self.error = error.localizedDescription
        }
    }

    func signOut(forget: Bool) async {
        await client.signOut()
        if forget {
            try? KeychainStore.writeDashboardPassword("")
            client.password = ""
            savedPassword = false
        }
        signedIn = false
        snapshot = nil
    }

    // MARK: data

    /// Polls while the management window is visible: the cluster every 5 s, models less often.
    func startPolling() {
        guard pollTask == nil else { return }
        pollTask = Task { [weak self] in
            var round = 0
            while !Task.isCancelled {
                guard let self else { return }
                await self.refresh(models: round % 3 == 0)
                round += 1
                try? await Task.sleep(nanoseconds: 5_000_000_000)
            }
        }
    }

    func stopPolling() { pollTask?.cancel(); pollTask = nil }

    func refresh(models: Bool = true) async {
        guard !refreshing else { return }
        refreshing = true
        defer { refreshing = false }
        do {
            let auth = try await client.authStatus()
            authEnabled = auth.enabled
            if auth.enabled && !auth.signedIn && client.password.isEmpty {
                signedIn = false
                error = ManagementError.signInNeeded.localizedDescription
            } else {
                snapshot = try await client.state()
                updated = Date()
                signedIn = true
                error = nil
                if models { await refreshModels(scan: false) }
            }
        } catch {
            self.error = error.localizedDescription
            if case ManagementError.signInNeeded = error { signedIn = false }
        }
        await refreshLifecycle()
    }

    func refreshModels(scan: Bool) async {
        guard signedIn || !authEnabled else { return }
        do { disk = try await client.diskModels(refresh: scan) } catch { info = "Model storage: \(error.localizedDescription)" }
        do { ollama = try await client.ollama() } catch { ollama = nil }
    }

    func refreshLifecycle() async {
        do { lifecycle = try await client.lifecycle(); lifecycleError = nil }
        catch { lifecycleError = error.localizedDescription }
    }

    /// Saves automatic unloading on the server (the one place the website and this app read it from).
    func saveLifecycle(enabled: Bool? = nil, idleSeconds: Int? = nil) async {
        lifecycleSaving = true
        defer { lifecycleSaving = false }
        do {
            let reply = try await client.setLifecycle(enabled: enabled, idleSeconds: idleSeconds)
            lifecycle = reply["view"].isNull ? lifecycle : reply["view"]
            lifecycleError = nil
            info = "Automatic unloading saved on the server."
        } catch {
            lifecycleError = error.localizedDescription
            await refreshLifecycle()          // show what the server really has, not the value that failed
        }
    }

    func loadDoctor(fresh: Bool) async {
        doctorLoading = true
        defer { doctorLoading = false }
        do { doctor = try await client.doctor(fresh: fresh)["doctor"] } catch { self.error = error.localizedDescription }
    }

    func logs(namespace: String, pod: String, container: String = "") async -> String {
        do { return try await client.logs(namespace: namespace, pod: pod, container: container) }
        catch { return "Couldn't read the logs: \(error.localizedDescription)" }
    }

    func setOllama(pod: String, model: String, load: Bool) async {
        do {
            try await client.ollamaLoad(pod: pod, model: model, load: load)
            info = load ? "Loaded \(model)." : "Unloaded \(model)."
        } catch { self.error = error.localizedDescription }
        await refreshModels(scan: false)
        await refreshLifecycle()
    }

    // MARK: tasks

    var busyJob: TrackedJob? { jobs.first { $0.status == "running" } }

    /// Starts a dashboard action (the same allow-list the website uses) and follows it.
    func run(_ action: String, params: [String: Any] = [:], title: String, then: (@MainActor (TrackedJob) -> Void)? = nil) async {
        do {
            let id = try await client.run(action, params: params)
            jobs.insert(TrackedJob(id: id, title: title), at: 0)
            if jobs.count > 30 { jobs.removeLast(jobs.count - 30) }
            shownJobID = id
            follow(id, then: then)
        } catch { self.error = error.localizedDescription }
    }

    private func follow(_ id: String, then: (@MainActor (TrackedJob) -> Void)?) {
        jobTasks[id]?.cancel()
        jobTasks[id] = Task { [weak self] in
            var misses = 0
            while !Task.isCancelled {
                guard let self else { return }
                guard let index = self.jobs.firstIndex(where: { $0.id == id }) else { return }
                do {
                    let view = try await self.client.job(id, since: self.jobs[index].next)
                    misses = 0
                    guard let i = self.jobs.firstIndex(where: { $0.id == id }) else { return }
                    self.jobs[i].lines += view["lines"].array.map(\.text)
                    if self.jobs[i].lines.count > 2000 { self.jobs[i].lines.removeFirst(self.jobs[i].lines.count - 2000) }
                    self.jobs[i].next = view["next"].int ?? self.jobs[i].next
                    self.jobs[i].title = view["title"].string ?? self.jobs[i].title
                    self.jobs[i].status = view["status"].string ?? "failed"
                    self.jobs[i].cancelable = view["cancelable"].bool ?? false
                    self.jobs[i].rc = view["rc"].int
                    if self.jobs[i].status != "running" {
                        let finished = self.jobs[i]
                        then?(finished)
                        await self.refresh(models: true)
                        self.jobTasks[id] = nil
                        return
                    }
                } catch {
                    misses += 1
                    if misses > 20 {
                        if let i = self.jobs.firstIndex(where: { $0.id == id }) { self.jobs[i].status = "unknown"; self.jobs[i].lines.append("Lost track of this task: \(error.localizedDescription)") }
                        return
                    }
                }
                try? await Task.sleep(nanoseconds: 1_500_000_000)
            }
        }
    }

    func cancel(_ job: TrackedJob) async {
        do { try await client.cancelJob(job.id); info = "Stopping \(job.title)…" } catch { self.error = error.localizedDescription }
    }

    // MARK: derived data

    var state: JSON { snapshot?["state"] ?? .null }
    var totals: JSON { snapshot?["totals"] ?? .null }
    var alerts: [JSON] { snapshot?["alerts"].array ?? [] }
    var nodes: [JSON] { state["nodes"].array }
    var pods: [JSON] { state["pods"].array }
    var workloads: [JSON] { state["workloads"].array }
    var events: [JSON] { state["events"].array }
    var services: [JSON] { state["services"].array }
    var volumes: [JSON] { state["volumes"].array }
    var split: JSON { state["ai"]["split"] }
    var isDemo: Bool { snapshot?["mode"].string == "demo" }
}
