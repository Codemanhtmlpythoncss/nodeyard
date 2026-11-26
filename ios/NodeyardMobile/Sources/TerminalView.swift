import SwiftTerm
import SwiftUI
import UIKit

struct TermTarget: Decodable, Identifiable, Hashable {
    var id: String
    var name: String?
    var kind: String?
    var online: Bool?
    var status: String?
    var address: String?
    var methods: [String]?
    var note: String?

    var symbol: String { kind == "server" ? "desktopcomputer" : kind == "node" ? "server.rack" : "laptopcomputer" }

    static func methodName(_ m: String) -> String {
        switch m {
        case "local": return "Shell on this server"
        case "kube": return "Shell through Kubernetes"
        case "root": return "Root shell"
        case "diagnose": return "Checks, then a root shell"
        case "ssh": return "SSH"
        default: return m
        }
    }
}

struct TerminalsView: View {
    var body: some View { NavigationStack { TerminalsList() } }
}

/// The machines a shell can open on (also pushed from the More tab).
struct TerminalsList: View {
    @EnvironmentObject private var model: AppModel
    @State private var targets: [TermTarget] = []
    @State private var problem: String?
    @State private var loaded = false
    @State private var open: Opening?

    struct Opening: Identifiable, Hashable { let target: TermTarget; let method: String; var id: String { target.id + method } }
    private struct Reply: Decodable { var targets: [TermTarget]? }

    var body: some View {
        Group {
            List {
                if let problem { ErrorBanner(text: problem).listRowBackground(SwiftUI.Color.clear) }
                if targets.isEmpty && loaded && problem == nil {
                    EmptyNote(symbol: "terminal", title: "No devices", detail: "The dashboard hasn't found any machines to open.")
                        .listRowBackground(SwiftUI.Color.clear)
                }
                ForEach(targets) { t in
                    let methods = t.methods ?? []
                    Menu {
                        ForEach(methods, id: \.self) { m in
                            Button(TermTarget.methodName(m)) { open = Opening(target: t, method: m) }
                        }
                    } label: {
                        HStack(spacing: 14) {
                            Image(systemName: t.symbol).font(.system(size: 20)).foregroundStyle(Theme.teal).frame(width: 30)
                            VStack(alignment: .leading, spacing: 3) {
                                Text(t.name ?? t.id).font(.system(size: 16, weight: .semibold, design: .rounded)).foregroundStyle(.white)
                                Text([t.status, t.address].compactMap { $0?.isEmpty == false ? $0 : nil }.joined(separator: " · "))
                                    .font(.system(size: 12, design: .rounded)).foregroundStyle(Theme.mist)
                                if let n = t.note, !n.isEmpty { Text(n).font(.system(size: 12)).foregroundStyle(.orange) }
                            }
                            Spacer()
                            HealthDot(health: t.online == false ? .bad : .good)
                        }
                        .contentShape(Rectangle())
                    } primaryAction: {
                        if let m = methods.first { open = Opening(target: t, method: m) }
                    }
                    .disabled(methods.isEmpty)
                    .rowStyle()
                }
            }
            .listStyle(.insetGrouped)
            .screen()
            .navigationTitle("Terminal")
            .refreshable { await load() }
            .task { if !loaded { await load() } }
            .fullScreenCover(item: $open) { o in TerminalScreen(target: o.target, method: o.method) }
        }
    }

    private func load() async {
        guard let api = model.api else { return }
        do {
            targets = try await api.get("/api/term/targets", as: Reply.self).targets ?? []
            problem = nil
        } catch {
            problem = error.localizedDescription
        }
        loaded = true
    }
}

/// One live shell. The server keeps the shell; this screen shows it and sends the keys.
@MainActor
final class TermSession: ObservableObject {
    @Published var status = "Connecting…"
    @Published var ended = false
    @Published var problem: String?
    let api: NodeyardAPI
    let target: TermTarget
    let method: String
    weak var view: SwiftTerm.TerminalView?
    private var id: String?
    private var offset = 0
    private var reader: Task<Void, Never>?
    private var pending = ""
    private var flushing = false

    private struct Opened: Decodable { var id: String; var user: String? }
    private struct Out: Decodable { var d: String; var o: Int }

    init(api: NodeyardAPI, target: TermTarget, method: String) {
        self.api = api
        self.target = target
        self.method = method
    }

    func open(cols: Int, rows: Int) async {
        guard id == nil else { return }
        do {
            var body: [String: Any] = ["target": target.id, "method": method, "cols": cols, "rows": rows]
            if let pw = api.savedPassword { body["password"] = pw }
            let r = try await api.post("/api/term/open", body, as: Opened.self)
            id = r.id
            status = [r.user, target.name].compactMap { $0 }.joined(separator: "@")
            read()
        } catch {
            problem = error.localizedDescription
            status = "Couldn't open"
            Haptics.error()
        }
    }

    private func read() {
        guard let id else { return }
        reader = Task { [weak self] in
            var tries = 0
            while !Task.isCancelled, let self, !self.ended {
                do {
                    for try await e in self.api.events("/api/term/stream", ["id": id, "from": String(self.offset)]) {
                        tries = 0
                        if e.name == "exit" { self.finish(); return }
                        guard e.name == "out", let o = try? JSONDecoder().decode(Out.self, from: Data(e.data.utf8)),
                              let bytes = Data(base64Encoded: o.d) else { continue }
                        self.offset = o.o
                        self.view?.feed(byteArray: ArraySlice([UInt8](bytes)))
                    }
                } catch {
                    tries += 1
                    if tries > 5 { self.problem = error.localizedDescription; self.finish(); return }
                }
                try? await Task.sleep(nanoseconds: 700_000_000)
            }
        }
    }

    private func finish() {
        ended = true
        status = "Session ended"
        view?.feed(text: "\r\n\u{1b}[2m[session ended]\u{1b}[0m\r\n")
    }

    /// Keys typed: batched so a fast typist or a paste goes in a few requests, in order.
    func send(_ text: String) {
        guard id != nil, !ended else { return }
        pending += text
        guard !flushing else { return }
        flushing = true
        Task {
            while !pending.isEmpty, let id {
                let chunk = pending
                pending = ""
                _ = try? await api.post("/api/term/input", ["id": id, "data": chunk])
            }
            flushing = false
        }
    }

    func resize(cols: Int, rows: Int) {
        guard let id else { return }
        Task { _ = try? await api.post("/api/term/resize", ["id": id, "cols": cols, "rows": rows]) }
    }

    func close() {
        reader?.cancel()
        guard let id else { return }
        let api = api
        Task { _ = try? await api.post("/api/term/close", ["id": id]) }
    }
}

struct TerminalScreen: View {
    let target: TermTarget
    let method: String
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        if let api = model.api {
            TerminalScreenBody(session: TermSession(api: api, target: target, method: method))
        }
    }
}

private struct TerminalScreenBody: View {
    @StateObject var session: TermSession
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ZStack(alignment: .top) {
                SwiftUI.Color.black.ignoresSafeArea()
                TermHost(session: session).ignoresSafeArea(.container, edges: .bottom)
                if let p = session.problem { ErrorBanner(text: p).padding() }
            }
            .navigationTitle(session.status)
            .navigationBarTitleDisplayMode(.inline)
            .toolbarBackground(SwiftUI.Color.black, for: .navigationBar)
            .toolbarColorScheme(.dark, for: .navigationBar)
            .toolbar {
                ToolbarItem(placement: .navigationBarLeading) {
                    Button("Close") { session.close(); dismiss() }
                }
            }
        }
        .preferredColorScheme(.dark)
    }
}

private struct TermHost: UIViewRepresentable {
    let session: TermSession

    func makeCoordinator() -> Coordinator { Coordinator(session: session) }

    func makeUIView(context: Context) -> SwiftTerm.TerminalView {
        let v = SwiftTerm.TerminalView(frame: CGRect(x: 0, y: 0, width: 390, height: 600))
        v.terminalDelegate = context.coordinator
        v.backgroundColor = .black
        v.nativeBackgroundColor = .black
        v.nativeForegroundColor = UIColor(red: 0.86, green: 0.89, blue: 0.97, alpha: 1)
        v.font = UIFont.monospacedSystemFont(ofSize: 12, weight: .regular)
        session.view = v
        DispatchQueue.main.async {
            _ = v.becomeFirstResponder()
            let t = v.getTerminal()
            Task { await session.open(cols: t.cols, rows: t.rows) }
        }
        return v
    }

    func updateUIView(_ v: SwiftTerm.TerminalView, context: Context) {}

    final class Coordinator: NSObject, TerminalViewDelegate {
        let session: TermSession
        init(session: TermSession) { self.session = session }

        func send(source: SwiftTerm.TerminalView, data: ArraySlice<UInt8>) {
            let s = String(decoding: data, as: UTF8.self)
            Task { @MainActor in session.send(s) }
        }
        func sizeChanged(source: SwiftTerm.TerminalView, newCols: Int, newRows: Int) {
            Task { @MainActor in session.resize(cols: newCols, rows: newRows) }
        }
        func setTerminalTitle(source: SwiftTerm.TerminalView, title: String) {}
        func hostCurrentDirectoryUpdate(source: SwiftTerm.TerminalView, directory: String?) {}
        func scrolled(source: SwiftTerm.TerminalView, position: Double) {}
        func requestOpenLink(source: SwiftTerm.TerminalView, link: String, params: [String: String]) {
            if let url = URL(string: link), ["http", "https"].contains(url.scheme ?? "") { UIApplication.shared.open(url) }
        }
        func bell(source: SwiftTerm.TerminalView) { Haptics.tap() }
        func clipboardCopy(source: SwiftTerm.TerminalView, content: Data) {
            UIPasteboard.general.string = String(decoding: content, as: UTF8.self)
        }
        func iTermContent(source: SwiftTerm.TerminalView, content: ArraySlice<UInt8>) {}
        func rangeChanged(source: SwiftTerm.TerminalView, startY: Int, endY: Int) {}
    }
}
