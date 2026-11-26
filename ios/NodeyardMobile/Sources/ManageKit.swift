import SwiftUI

/// Shared plumbing for the More tab: a screen that loads one JSON reply, quick actions with a toast,
/// and actions that ask first, show the server's plan, then run (and follow a job's output live).
@MainActor
final class Actions: ObservableObject {
    @Published var pending: Pending?
    @Published var toast: Toast?
    var api: NodeyardAPI?

    struct Toast: Identifiable, Equatable { let id = UUID(); let text: String; let ok: Bool }

    /// One action that needs a yes. With `fetchPlan`, the server is asked first what it would do.
    struct Pending: Identifiable {
        let id = UUID()
        var title: String
        var lines: [String] = []
        var path: String
        var body: [String: Any]
        var fetchPlan = false
        var addConfirm = false
        var button = "Run"
        var destructive = false
        var done: (() -> Void)?
    }

    func ask(_ title: String, _ lines: [String], path: String, body: [String: Any], button: String = "Run",
             destructive: Bool = false, confirm: Bool = false, done: (() -> Void)? = nil) {
        pending = Pending(title: title, lines: lines, path: path, body: body, addConfirm: confirm, button: button, destructive: destructive, done: done)
    }

    /// The server answers with {"plan": [...]} until it's sent again with "confirm": true.
    func planned(_ title: String, path: String, body: [String: Any], button: String = "Run",
                 destructive: Bool = false, done: (() -> Void)? = nil) {
        pending = Pending(title: title, path: path, body: body, fetchPlan: true, addConfirm: true, button: button, destructive: destructive, done: done)
    }

    /// A `nodeyard` command through /api/run, with its output followed live.
    func job(_ title: String, _ lines: [String], _ params: [String: Any], destructive: Bool = false, done: (() -> Void)? = nil) {
        var body = params
        if body["action"] == nil { body["action"] = "" }
        ask(title, lines, path: "/api/run", body: body, button: destructive ? "Run" : "Start", destructive: destructive, done: done)
    }

    /// Fire and forget, with a toast for the answer.
    func quick(_ path: String, _ body: [String: Any] = [:], ok message: String, then: (() async -> Void)? = nil) {
        guard let api else { return }
        Task {
            do {
                let r = try await api.post(path, body)
                try Self.check(r)
                show(Self.text(r["message"]).first ?? Self.text(r["done"]).first ?? message)
                Haptics.success()
                await then?()
            } catch {
                show(error.localizedDescription, ok: false)
                Haptics.error()
            }
        }
    }

    func show(_ text: String, ok: Bool = true) {
        let t = Toast(text: text, ok: ok)
        toast = t
        Task {
            try? await Task.sleep(nanoseconds: 3_200_000_000)
            if toast == t { toast = nil }
        }
    }

    static func check(_ r: [String: Any]) throws {
        if r["ok"] as? Bool == false { throw APIError(message: r["error"] as? String ?? "The dashboard said no.") }
    }

    /// "done" and "plan" come as a string or a list of strings.
    static func text(_ v: Any?) -> [String] {
        if let s = v as? String { return s.isEmpty ? [] : [s] }
        if let a = v as? [Any] { return a.compactMap { $0 as? String } }
        return []
    }
}

extension View {
    /// Hosts the confirm sheet and toast for every screen pushed under it.
    func actionHost(_ actions: Actions) -> some View {
        self
            .sheet(item: Binding(get: { actions.pending }, set: { actions.pending = $0 })) { p in
                ConfirmSheet(pending: p).environmentObject(actions)
            }
            .overlay(alignment: .top) {
                if let t = actions.toast {
                    Label(t.text, systemImage: t.ok ? "checkmark.circle.fill" : "exclamationmark.triangle.fill")
                        .font(.system(size: 14, weight: .medium, design: .rounded))
                        .foregroundStyle(t.ok ? .white : Theme.danger)
                        .padding(.horizontal, 16).padding(.vertical, 11)
                        .background(.ultraThinMaterial, in: Capsule())
                        .overlay(Capsule().stroke(Theme.line))
                        .padding(.horizontal, 20)
                        .padding(.top, 6)
                        .transition(.move(edge: .top).combined(with: .opacity))
                        .onTapGesture { actions.toast = nil }
                }
            }
            .animation(.spring(response: 0.35, dampingFraction: 0.85), value: actions.toast)
    }
}

/// Ask → (plan) → run → result or live job output, all in one sheet.
struct ConfirmSheet: View {
    let pending: Actions.Pending
    @EnvironmentObject private var actions: Actions
    @Environment(\.dismiss) private var dismiss

    enum Phase { case loading, ask, running, done, job(String) }
    @State private var phase = Phase.ask
    @State private var lines: [String] = []
    @State private var script = ""
    @State private var hash: String?
    @State private var result: [String] = []
    @State private var warning: String?
    @State private var problem: String?

    var body: some View {
        NavigationStack {
            Group {
                if case .job(let id) = phase {
                    JobLog(id: id) { pending.done?() }
                } else {
                    List {
                        if let problem { ErrorBanner(text: problem).listRowBackground(Color.clear) }
                        switch phase {
                        case .loading:
                            HStack { Spacer(); ProgressView("Asking the server what it would do…"); Spacer() }
                                .padding(.vertical, 30).listRowBackground(Color.clear)
                        case .done:
                            Section {
                                ForEach(Array((result.isEmpty ? ["Done."] : result).enumerated()), id: \.offset) { _, l in
                                    Label(l, systemImage: "checkmark.circle.fill").foregroundStyle(.white)
                                        .symbolRenderingMode(.multicolor)
                                }
                                if let warning { Label(warning, systemImage: "exclamationmark.triangle.fill").foregroundStyle(.orange) }
                            }
                            .rowStyle()
                        default:
                            if !lines.isEmpty {
                                Section("What happens") {
                                    ForEach(Array(lines.enumerated()), id: \.offset) { _, l in
                                        HStack(alignment: .firstTextBaseline, spacing: 10) {
                                            Circle().fill(Theme.teal).frame(width: 6, height: 6)
                                            Text(l).font(.system(size: 15, design: .rounded))
                                        }
                                    }
                                }
                                .rowStyle()
                            }
                            if !script.isEmpty {
                                Section {
                                    DisclosureGroup("Commands it runs") {
                                        Text(script).font(.system(size: 11, design: .monospaced)).foregroundStyle(Theme.mist)
                                            .textSelection(.enabled)
                                    }
                                }
                                .rowStyle()
                            }
                        }
                    }
                    .listStyle(.insetGrouped)
                    .safeAreaInset(edge: .bottom) { footer }
                }
            }
            .screen()
            .navigationTitle(pending.title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .navigationBarLeading) {
                    Button(isFinished ? "Done" : "Cancel") { dismiss() }
                }
            }
        }
        .presentationDetents(isJob ? [.large] : [.medium, .large])
        .preferredColorScheme(.dark)
        .interactiveDismissDisabled(isRunning)
        .task {
            lines = pending.lines
            if pending.fetchPlan { await fetchPlan() }
        }
    }

    private var isJob: Bool { if case .job = phase { return true }; return false }
    private var isRunning: Bool { if case .running = phase { return true }; return false }
    private var isFinished: Bool {
        switch phase { case .done, .job: return true; default: return false }
    }

    @ViewBuilder private var footer: some View {
        switch phase {
        case .ask, .running:
            Button(role: pending.destructive ? .destructive : nil) { Task { await run() } } label: {
                Text(isRunning ? "Working" : pending.button)
            }
            .buttonStyle(BrandButtonStyle(busy: isRunning))
            .tint(pending.destructive ? Theme.danger : nil)
            .disabled(isRunning)
            .padding(16)
            .background(.ultraThinMaterial)
        default:
            EmptyView()
        }
    }

    private func fetchPlan() async {
        guard let api = actions.api else { return }
        phase = .loading
        do {
            let r = try await api.post(pending.path, pending.body)
            try Actions.check(r)
            let done = Actions.text(r["done"])
            if !done.isEmpty && r["plan"] == nil {
                result = done
                phase = .done
                return
            }
            lines = Actions.text(r["plan"])
            script = r["script"] as? String ?? ""
            hash = r["hash"] as? String
            phase = .ask
        } catch {
            problem = error.localizedDescription
            phase = .ask
        }
    }

    private func run() async {
        guard let api = actions.api else { return }
        phase = .running
        problem = nil
        var body = pending.body
        if pending.addConfirm { body["confirm"] = true }
        if let hash, body["hash"] == nil { body["hash"] = hash }
        do {
            let r = try await api.post(pending.path, body)
            try Actions.check(r)
            if let id = r["job"] as? String, !id.isEmpty {
                phase = .job(id)
                Haptics.tap()
                return
            }
            result = Actions.text(r["done"]) + Actions.text(r["message"]) + Actions.text(r["result"] as? String)
            warning = r["warning"] as? String
            phase = .done
            Haptics.success()
            pending.done?()
        } catch {
            problem = error.localizedDescription
            phase = .ask
            Haptics.error()
        }
    }
}

/// A `nodeyard` job's output, read as it runs.
struct JobLog: View {
    let id: String
    var finished: (() -> Void)?
    @EnvironmentObject private var actions: Actions
    @State private var lines: [String] = []
    @State private var status = "running"
    @State private var rc: Int?
    @State private var title = ""
    @State private var cancelable = false
    @State private var problem: String?

    private struct Reply: Decodable {
        var title: String?
        var status: String?
        var rc: Int?
        var lines: [String]?
        var next: Int?
        var cancelable: Bool?
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                if status == "running" { ProgressView().tint(Theme.teal) } else {
                    Image(systemName: rc == 0 ? "checkmark.circle.fill" : "xmark.octagon.fill")
                        .foregroundStyle(rc == 0 ? Theme.teal : Theme.danger)
                }
                VStack(alignment: .leading, spacing: 2) {
                    Text(title.isEmpty ? "Running" : title).font(.system(size: 15, weight: .semibold, design: .rounded)).lineLimit(2)
                    Text(status == "running" ? "Running…" : (rc == 0 ? "Finished" : "Stopped with code \(rc.map(String.init) ?? "?")"))
                        .font(.system(size: 12, design: .rounded)).foregroundStyle(Theme.mist)
                }
                Spacer()
                if cancelable && status == "running" {
                    Button("Stop", role: .destructive) {
                        Task { _ = try? await actions.api?.post("/api/job/cancel", ["id": id]) }
                    }
                    .buttonStyle(.bordered)
                }
            }
            .padding(14)
            .background(Theme.panel)
            if let problem { ErrorBanner(text: problem).padding(10) }
            ScrollViewReader { proxy in
                ScrollView([.vertical, .horizontal]) {
                    LazyVStack(alignment: .leading, spacing: 1) {
                        ForEach(Array(lines.enumerated()), id: \.offset) { _, l in
                            Text(l.isEmpty ? " " : l).font(.system(size: 11, design: .monospaced))
                                .foregroundStyle(Color(hex: 0xD6DEF5)).fixedSize()
                        }
                        Color.clear.frame(height: 1).id("end")
                    }
                    .padding(12)
                    .textSelection(.enabled)
                }
                .background(Color.black)
                .onChange(of: lines.count) { _ in proxy.scrollTo("end", anchor: .bottom) }
            }
        }
        .task { await follow() }
    }

    private func follow() async {
        guard let api = actions.api else { return }
        var since = 0
        var misses = 0
        while !Task.isCancelled {
            do {
                let r = try await api.get("/api/job", ["id": id, "since": String(since)], as: Reply.self)
                misses = 0
                lines += r.lines ?? []
                since = r.next ?? since + (r.lines?.count ?? 0)
                title = r.title ?? title
                status = r.status ?? status
                rc = r.rc
                cancelable = r.cancelable ?? false
                if status != "running" {
                    rc == 0 ? Haptics.success() : Haptics.error()
                    finished?()
                    return
                }
            } catch {
                misses += 1
                if misses > 8 { problem = error.localizedDescription; return }
            }
            try? await Task.sleep(nanoseconds: 900_000_000)
        }
    }
}

/// Loads one GET reply; pull to refresh; error and spinner handled.
@MainActor
final class Loader<T: Decodable>: ObservableObject {
    @Published var value: T?
    @Published var problem: String?
    @Published var loading = false
    let path: String
    var query: [String: String]

    init(_ path: String, query: [String: String] = [:]) { self.path = path; self.query = query }

    func load(_ api: NodeyardAPI?) async {
        guard let api else { return }
        loading = true
        defer { loading = false }
        do {
            value = try await api.get(path, query, as: T.self)
            problem = nil
        } catch {
            if !(error is CancellationError) { problem = error.localizedDescription }
        }
    }
}

/// The List every More screen uses: dark, inset, error on top, spinner while first loading.
struct ManageList<Content: View>: View {
    let title: String
    let problem: String?
    let loading: Bool
    var refresh: () async -> Void
    @ViewBuilder var content: () -> Content

    var body: some View {
        List {
            if let problem { ErrorBanner(text: problem).listRowBackground(Color.clear) }
            content()
        }
        .listStyle(.insetGrouped)
        .overlay { if loading { ProgressView().controlSize(.large) } }
        .screen()
        .navigationTitle(title)
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { await refresh() }
    }
}

/// A coloured pill: ok / warn / bad / idle.
struct Pill: View {
    let text: String
    var tone: Tone = .idle
    enum Tone { case ok, warn, bad, idle }
    var color: Color {
        switch tone { case .ok: return Theme.teal; case .warn: return .orange; case .bad: return Theme.danger; case .idle: return Theme.mist }
    }
    var body: some View {
        Text(text).font(.system(size: 11, weight: .semibold, design: .rounded))
            .padding(.horizontal, 8).padding(.vertical, 3)
            .foregroundStyle(color)
            .background(color.opacity(0.14), in: Capsule())
    }
}

/// A row title with a quieter line under it.
struct TwoLine: View {
    let title: String
    var detail: String?
    var symbol: String?
    var body: some View {
        HStack(spacing: 12) {
            if let symbol { Image(systemName: symbol).foregroundStyle(Theme.teal).frame(width: 24) }
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(.system(size: 15, weight: .semibold, design: .rounded))
                if let detail, !detail.isEmpty {
                    Text(detail).font(.system(size: 12, design: .rounded)).foregroundStyle(Theme.mist)
                }
            }
        }
    }
}
