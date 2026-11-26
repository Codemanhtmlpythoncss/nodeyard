import SwiftUI

// MARK: - Updates

struct UpdatesScreen: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var actions: Actions
    @StateObject private var data = Loader<Reply>("/api/updates")

    struct Reply: Decodable { var nodes: [Node]? }
    struct Node: Decodable, Identifiable {
        var node: String
        var ready: Bool?
        var os: String?
        var kernel: String?
        var checked: Bool?
        var probing: Bool?
        var probe_error: String?
        var counts: [String: Int]?
        var total: Int?
        var id: String { node }
    }

    var body: some View {
        ManageList(title: "Updates", problem: data.problem, loading: data.value == nil && data.loading, refresh: { await data.load(model.api) }) {
            Section {
                Button { check("all") } label: { Label("Check every node", systemImage: "arrow.clockwise") }
            }
            .rowStyle()
            ForEach(data.value?.nodes ?? []) { n in
                Section {
                    HStack {
                        TwoLine(title: n.node, detail: [n.os, n.kernel].compactMap { $0 }.joined(separator: " · "), symbol: "server.rack")
                        Spacer()
                        if n.probing == true { ProgressView() }
                        else if n.checked != true { Pill(text: "Not checked") }
                        else if (n.total ?? 0) == 0 { Pill(text: "Up to date", tone: .ok) }
                        else { Pill(text: "\(n.total ?? 0) updates", tone: (n.counts?["security"] ?? 0) > 0 ? .bad : .warn) }
                    }
                    if let e = n.probe_error, !e.isEmpty { Text(e).font(.system(size: 12)).foregroundStyle(Theme.danger) }
                    if let c = n.counts, (n.total ?? 0) > 0 {
                        HStack(spacing: 6) {
                            ForEach(["security", "kernel", "drivers", "other"], id: \.self) { g in
                                if let v = c[g], v > 0 { Pill(text: "\(v) \(g)", tone: g == "security" ? .bad : .idle) }
                            }
                        }
                    }
                    HStack {
                        Button("Check") { check(n.node) }.buttonStyle(.bordered)
                        Spacer()
                        if (n.total ?? 0) > 0 {
                            Button("Install updates") {
                                let groups = (n.counts ?? [:]).filter { $0.value > 0 }.map(\.key)
                                actions.planned("Update \(n.node)", path: "/api/updates/apply", body: ["node": n.node, "groups": groups], button: "Install") {
                                    Task { await data.load(model.api) }
                                }
                            }
                            .buttonStyle(.borderedProminent).tint(Theme.indigo)
                        }
                    }
                    .disabled(n.ready == false || n.probing == true)
                }
                .rowStyle()
            }
        }
        .task { if data.value == nil { await data.load(model.api) } }
    }

    private func check(_ node: String) {
        actions.quick("/api/updates/check", ["node": node], ok: "Checking for updates") {
            for _ in 0..<20 {
                try? await Task.sleep(nanoseconds: 3_000_000_000)
                await data.load(model.api)
                if !(data.value?.nodes ?? []).contains(where: { $0.probing == true }) { break }
            }
        }
    }
}

// MARK: - Hardware

struct HardwareScreen: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var actions: Actions
    @StateObject private var data = Loader<Reply>("/api/hwcare")

    struct Reply: Decodable { var nodes: [Node]? }
    struct Node: Decodable, Identifiable {
        var node: String
        var ready: Bool?
        var os: String?
        var probing: Bool?
        var probe_error: String?
        var guide: Guide?
        var fan: Note?
        var lid: Note?
        var id: String { node }
    }
    struct Guide: Decodable { var facts: [FactRow]?; var unknown: [String]? }
    struct FactRow: Decodable, Hashable { var k: String; var v: String; var source: String? }
    struct Note: Decodable { var mode: String?; var why: String?; var supported: Bool? }

    var body: some View {
        ManageList(title: "Hardware", problem: data.problem, loading: data.value == nil && data.loading, refresh: { await data.load(model.api) }) {
            Section {
                Button { probe("all") } label: { Label("Check every machine", systemImage: "stethoscope") }
            }
            .rowStyle()
            ForEach(data.value?.nodes ?? []) { n in
                Section {
                    ForEach(n.guide?.facts ?? [], id: \.self) { f in Fact(f.k, f.v) }
                    if let fan = n.fan {
                        Fact("Fan", [fan.mode == "unknown" ? nil : fan.mode, fan.why].compactMap { $0 }.joined(separator: " — "))
                    }
                    ForEach(n.guide?.unknown ?? [], id: \.self) { u in
                        Text(u).font(.system(size: 12)).foregroundStyle(Theme.mist)
                    }
                    if let e = n.probe_error, !e.isEmpty { Text(e).font(.system(size: 12)).foregroundStyle(Theme.danger) }
                    Button { probe(n.node) } label: {
                        HStack { Label("Check \(n.node)", systemImage: "arrow.clockwise"); if n.probing == true { Spacer(); ProgressView() } }
                    }
                    .disabled(n.ready == false || n.probing == true)
                } header: { Text(n.node) }
                .rowStyle()
            }
        }
        .task { if data.value == nil { await data.load(model.api) } }
    }

    private func probe(_ node: String) {
        actions.quick("/api/hwcare/probe", ["node": node], ok: "Checking the hardware") {
            for _ in 0..<20 {
                try? await Task.sleep(nanoseconds: 3_000_000_000)
                await data.load(model.api)
                if !(data.value?.nodes ?? []).contains(where: { $0.probing == true }) { break }
            }
        }
    }
}

// MARK: - Memory

struct MemoryScreen: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var actions: Actions
    @StateObject private var data = Loader<Reply>("/api/memory")

    struct Reply: Decodable { var nodes: [Node]? }
    struct Node: Decodable, Identifiable {
        var node: String
        var ready: Bool?
        var mem_total: Double?
        var mem_available: Double?
        var swap_total: Double?
        var swap_free: Double?
        var procs: [Proc]?
        var id: String { node }
    }
    struct Proc: Decodable, Identifiable {
        var pid: Int
        var start: Int?
        var name: String?
        var cmd: String?
        var rss: Double?
        var swap: Double?
        var verdict: String?
        var what: String?
        var can_swap: Bool?
        var can_stop: Bool?
        var unit: String?
        var id: Int { pid }
    }

    var body: some View {
        ManageList(title: "Memory", problem: data.problem, loading: data.value == nil && data.loading, refresh: { await data.load(model.api) }) {
            ForEach(data.value?.nodes ?? []) { n in
                Section {
                    if let total = n.mem_total, total > 0 {
                        let used = total - (n.mem_available ?? 0)
                        VStack(alignment: .leading, spacing: 6) {
                            HStack {
                                Text("RAM").foregroundStyle(Theme.mist)
                                Spacer()
                                Text("\(Format.bytes(used)) of \(Format.bytes(total))")
                            }
                            .font(.system(size: 13, design: .rounded))
                            Meter(fraction: used / total)
                            if let st = n.swap_total, st > 0 {
                                Text("Swap: \(Format.bytes(st - (n.swap_free ?? 0))) of \(Format.bytes(st))")
                                    .font(.system(size: 12, design: .rounded)).foregroundStyle(Theme.mist)
                            }
                        }
                        .padding(.vertical, 4)
                    }
                    ForEach((n.procs ?? []).prefix(25)) { p in
                        VStack(alignment: .leading, spacing: 4) {
                            HStack {
                                Text(p.name ?? "pid \(p.pid)").font(.system(size: 14, weight: .semibold, design: .rounded))
                                Spacer()
                                Text(Format.bytes(p.rss)).font(.system(size: 13, design: .rounded).monospacedDigit())
                                Pill(text: p.verdict ?? "?", tone: p.verdict == "needed" ? .ok : p.verdict == "unneeded" ? .warn : .idle)
                            }
                            if let w = p.what { Text(w).font(.system(size: 12)).foregroundStyle(Theme.mist).lineLimit(2) }
                        }
                        .swipeActions(edge: .trailing) {
                            if p.can_stop == true {
                                Button("Stop") { act(n.node, p, "stop") }.tint(Theme.danger)
                            }
                            if p.can_swap == true {
                                Button("Swap out") { act(n.node, p, "swap") }.tint(Theme.indigo)
                            }
                        }
                        .contextMenu {
                            if p.can_swap == true { Button { act(n.node, p, "swap") } label: { Label("Move to swap", systemImage: "arrow.down.doc") } }
                            if p.can_stop == true { Button(role: .destructive) { act(n.node, p, "stop") } label: { Label("Stop it", systemImage: "stop.circle") } }
                        }
                    }
                } header: { Text(n.node) } footer: { Text("Swipe a process to stop it or move it to swap.") }
                .rowStyle()
            }
        }
        .task { if data.value == nil { await data.load(model.api) } }
    }

    private func act(_ node: String, _ p: Proc, _ action: String) {
        var body: [String: Any] = ["node": node, "pid": p.pid, "action": action]
        if let s = p.start { body["start"] = s }
        actions.planned(action == "stop" ? "Stop \(p.name ?? "process")" : "Swap out \(p.name ?? "process")", path: "/api/memory/action", body: body,
                        button: action == "stop" ? "Stop it" : "Move to swap", destructive: action == "stop") {
            Task { await data.load(model.api) }
        }
    }
}

// MARK: - Storage

struct StorageScreen: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var actions: Actions
    @StateObject private var data = Loader<Reply>("/api/storage/usage")

    struct Reply: Decodable { var nodes: [Node]?; var can_scan: Bool? }
    struct Node: Decodable, Identifiable {
        var node: String
        var ready: Bool?
        var disk_total: Double?
        var disk_used: Double?
        var scanning: Bool?
        var error: String?
        var dirs: [Entry]?
        var files: [Entry]?
        var id: String { node }
    }
    struct Entry: Decodable, Hashable { var path: String; var bytes: Double?; var depth: Int? }

    var body: some View {
        ManageList(title: "Storage", problem: data.problem, loading: data.value == nil && data.loading, refresh: { await data.load(model.api) }) {
            ForEach(data.value?.nodes ?? []) { n in
                Section {
                    if let total = n.disk_total, total > 0 {
                        VStack(alignment: .leading, spacing: 6) {
                            HStack {
                                Text("Used").foregroundStyle(Theme.mist)
                                Spacer()
                                Text("\(Format.bytes(n.disk_used)) of \(Format.bytes(total))")
                            }
                            .font(.system(size: 13, design: .rounded))
                            Meter(fraction: (n.disk_used ?? 0) / total)
                        }
                        .padding(.vertical, 4)
                    }
                    let dirs = (n.dirs ?? []).sorted { ($0.bytes ?? 0) > ($1.bytes ?? 0) }.prefix(12)
                    if !dirs.isEmpty {
                        ForEach(Array(dirs), id: \.self) { d in sizeRow(d, symbol: "folder") }
                    }
                    let files = (n.files ?? []).sorted { ($0.bytes ?? 0) > ($1.bytes ?? 0) }.prefix(8)
                    ForEach(Array(files), id: \.self) { f in sizeRow(f, symbol: "doc") }
                    if let e = n.error, !e.isEmpty { Text(e).font(.system(size: 12)).foregroundStyle(Theme.danger) }
                    if data.value?.can_scan != false {
                        Button { scan(n.node) } label: {
                            HStack { Label(dirs.isEmpty ? "Find what fills the disk" : "Scan again", systemImage: "magnifyingglass"); if n.scanning == true { Spacer(); ProgressView() } }
                        }
                        .disabled(n.ready == false || n.scanning == true)
                    }
                } header: { Text(n.node) }
                .rowStyle()
            }
        }
        .task { if data.value == nil { await data.load(model.api) } }
    }

    private func sizeRow(_ e: Entry, symbol: String) -> some View {
        HStack {
            Image(systemName: symbol).foregroundStyle(Theme.mist).frame(width: 20)
            Text(e.path).font(.system(size: 12, design: .monospaced)).lineLimit(1).truncationMode(.middle)
            Spacer()
            Text(Format.bytes(e.bytes)).font(.system(size: 12, design: .rounded).monospacedDigit()).foregroundStyle(Theme.mist)
        }
    }

    private func scan(_ node: String) {
        actions.quick("/api/storage/scan", ["node": node], ok: "Scanning \(node)") {
            for _ in 0..<40 {
                try? await Task.sleep(nanoseconds: 3_000_000_000)
                await data.load(model.api)
                if data.value?.nodes?.first(where: { $0.node == node })?.scanning != true { break }
            }
        }
    }
}

// MARK: - Connections

struct ConnectionsScreen: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var actions: Actions
    @StateObject private var data = Loader<Reply>("/api/devices/connections")

    struct Reply: Decodable { var devices: [Device]? }
    struct Device: Decodable, Identifiable {
        var id: String
        var name: String?
        var status: String?
        var preferred: String?
        var k8s_ready: Bool?
        var lan: Route?
        var tailscale: Route?
    }
    struct Route: Decodable { var address: String?; var state: String?; var ms: Double?; var error: String? }

    var body: some View {
        ManageList(title: "Connections", problem: data.problem, loading: data.value == nil && data.loading, refresh: { await data.load(model.api) }) {
            Section {
                Button {
                    actions.quick("/api/devices/recheck", ok: "Checking every route") {
                        try? await Task.sleep(nanoseconds: 2_500_000_000)
                        await data.load(model.api)
                    }
                } label: { Label("Check every machine again", systemImage: "arrow.clockwise") }
            }
            .rowStyle()
            ForEach(data.value?.devices ?? []) { d in
                Section {
                    HStack {
                        TwoLine(title: d.name ?? d.id, detail: d.preferred.map { "Uses \($0 == "lan" ? "LAN" : $0 == "tailscale" ? "Tailscale" : $0)" }, symbol: "desktopcomputer")
                        Spacer()
                        Pill(text: d.status ?? "?", tone: d.status == "both" ? .ok : d.status == "none" || d.status == "down" ? .bad : .warn)
                    }
                    route("LAN", d.lan)
                    route("Tailscale", d.tailscale)
                    HStack {
                        Button("Test") {
                            actions.quick("/api/devices/test", ["id": d.id], ok: "Tested \(d.name ?? d.id)") { await data.load(model.api) }
                        }
                        .buttonStyle(.bordered)
                        Spacer()
                        Button("Forget", role: .destructive) {
                            actions.ask("Forget \(d.name ?? d.id)", ["Its saved addresses and history are removed. It's found again if it's still on the network."],
                                        path: "/api/devices/forget", body: ["id": d.id], button: "Forget", destructive: true) {
                                Task { await data.load(model.api) }
                            }
                        }
                        .buttonStyle(.bordered)
                    }
                }
                .rowStyle()
            }
        }
        .task { if data.value == nil { await data.load(model.api) } }
    }

    @ViewBuilder private func route(_ name: String, _ r: Route?) -> some View {
        if let r, let a = r.address, !a.isEmpty {
            HStack {
                Circle().fill(r.state == "reachable" ? Theme.teal : Theme.danger).frame(width: 8, height: 8)
                Text(name).foregroundStyle(Theme.mist)
                Spacer()
                Text(a).font(.system(size: 13, design: .monospaced))
                if let ms = r.ms { Text(String(format: "%.0f ms", ms)).foregroundStyle(Theme.mist) }
            }
            .font(.system(size: 13, design: .rounded))
        }
    }
}

// MARK: - AI models

struct ModelsScreen: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var actions: Actions
    @StateObject private var data = Loader<Reply>("/api/v1/models")

    struct Reply: Decodable { var models: [AIModel]? }
    struct AIModel: Decodable, Identifiable {
        var id: String
        var kind: String?
        var name: String?
        var size: Double?
        var nodes: [String]?
        var downloaded: Bool?
        var loaded: Bool?
        var ready: Bool?
        var loaded_on: [String]?
    }

    var body: some View {
        ManageList(title: "AI models", problem: data.problem, loading: data.value == nil && data.loading, refresh: { await data.load(model.api) }) {
            let all = data.value?.models ?? []
            let groups: [(String, [AIModel])] = [("Shared across the cluster", all.filter { $0.kind == "split" }),
                                                 ("Ollama", all.filter { $0.kind == "ollama" }),
                                                 ("Other", all.filter { $0.kind != "split" && $0.kind != "ollama" })]
            ForEach(groups.filter { !$0.1.isEmpty }, id: \.0) { g in
                Section(g.0) {
                    ForEach(g.1) { m in
                        HStack {
                            TwoLine(title: m.name ?? m.id, detail: [m.size.map { Format.bytes($0) }, (m.loaded_on ?? []).isEmpty ? nil : "on " + (m.loaded_on ?? []).joined(separator: ", ")].compactMap { $0 }.joined(separator: " · "))
                            Spacer()
                            if m.ready == true { Pill(text: "Running", tone: .ok) }
                            else if m.loaded == true { Pill(text: "Loading", tone: .warn) }
                            if m.loaded == true {
                                Button("Unload") {
                                    actions.ask("Unload \(m.name ?? m.id)", ["Frees its memory. Chats can't use it until it's loaded again."],
                                                path: "/api/v1/models/unload", body: ["model": m.id], button: "Unload", destructive: true) { reload() }
                                }
                                .buttonStyle(.bordered).controlSize(.small)
                            } else if m.downloaded != false {
                                Button("Load") {
                                    actions.ask("Load \(m.name ?? m.id)", [m.kind == "split" ? "Replaces the model that's running across the cluster now." : "Loads it into Ollama's memory."],
                                                path: "/api/v1/models/load", body: ["model": m.id], button: "Load") { reload() }
                                }
                                .buttonStyle(.borderedProminent).tint(Theme.indigo).controlSize(.small)
                            }
                        }
                    }
                }
                .rowStyle()
            }
        }
        .task { if data.value == nil { await data.load(model.api) } }
    }

    private func reload() { Task { await data.load(model.api) } }
}

// MARK: - Cleanup

struct CleanupScreen: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var actions: Actions
    @StateObject private var data = Loader<Reply>("/api/cleanup")
    @State private var picked: Set<String> = []

    struct Reply: Decodable { var auto: Bool?; var hash: String?; var candidates: [Candidate]?; var policy: Policy? }
    struct Policy: Decodable { var hours: Double? }
    struct Candidate: Decodable, Identifiable {
        var uid: String
        var namespace: String?
        var name: String?
        var phase: String?
        var node: String?
        var confidence: String?
        var remove: Bool?
        var reason: String?
        var id: String { uid }
    }

    var body: some View {
        ManageList(title: "Cleanup", problem: data.problem, loading: data.value == nil && data.loading, refresh: { await data.load(model.api) }) {
            Section {
                Toggle(isOn: Binding(get: { data.value?.auto == true }, set: { on in
                    actions.quick("/api/cleanup/auto", ["enabled": on, "confirm": true, "policy": data.value?.hash ?? ""], ok: on ? "Automatic cleanup on" : "Automatic cleanup off") {
                        await data.load(model.api)
                    }
                })) { Label("Clean up by itself", systemImage: "clock.arrow.2.circlepath") }
                .tint(Theme.indigo)
            } footer: {
                Text("Removes pods that stopped more than \(Int(data.value?.policy?.hours ?? 12)) hours ago.")
            }
            .rowStyle()
            let list = data.value?.candidates ?? []
            Section {
                if list.isEmpty && data.value != nil {
                    EmptyNote(symbol: "sparkles", title: "Nothing to clean", detail: "No stopped pods are old enough.")
                }
                ForEach(list) { c in
                    Button { toggle(c.uid) } label: {
                        HStack(alignment: .top, spacing: 12) {
                            Image(systemName: picked.contains(c.uid) ? "checkmark.circle.fill" : "circle")
                                .foregroundStyle(picked.contains(c.uid) ? Theme.indigo : Theme.mist)
                            VStack(alignment: .leading, spacing: 3) {
                                Text(c.name ?? c.uid).font(.system(size: 14, weight: .semibold, design: .rounded)).foregroundStyle(.white)
                                Text([c.namespace, c.node, c.phase].compactMap { $0 }.joined(separator: " · "))
                                    .font(.system(size: 12, design: .rounded)).foregroundStyle(Theme.mist)
                                if let r = c.reason { Text(r).font(.system(size: 12)).foregroundStyle(Theme.mist) }
                            }
                        }
                    }
                }
            } header: { Text(list.isEmpty ? "" : "Stopped pods") }
            .rowStyle()
            if !picked.isEmpty {
                Section {
                    Button(role: .destructive) {
                        let uids = Array(picked)
                        actions.ask("Remove \(uids.count) pod\(uids.count == 1 ? "" : "s")", ["Deletes the stopped pods you picked. Running apps aren't touched."],
                                    path: "/api/cleanup/run", body: ["uids": uids, "confirm": true], button: "Remove", destructive: true) {
                            picked = []
                            Task { await data.load(model.api) }
                        }
                    } label: { Label("Remove \(picked.count) picked", systemImage: "trash") }
                }
                .rowStyle()
            }
        }
        .task {
            if data.value == nil {
                await data.load(model.api)
                picked = Set((data.value?.candidates ?? []).filter { $0.remove == true }.map(\.uid))
            }
        }
    }

    private func toggle(_ uid: String) {
        if picked.contains(uid) { picked.remove(uid) } else { picked.insert(uid) }
        Haptics.tap()
    }
}

// MARK: - Homelab

struct HomelabScreen: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var actions: Actions
    @StateObject private var data = Loader<Reply>("/api/homelab/services")
    @State private var adding = false
    @State private var addingWake = false

    struct Reply: Decodable { var services: [Service]?; var wol: [Wake]? }
    struct Service: Decodable, Identifiable {
        var id: String
        var name: String?
        var kind: String?
        var target: String?
        var group: String?
        var status: String?
        var uptime: Double?
        var avg_ms: Double?
        var link: String?
    }
    struct Wake: Decodable, Identifiable { var id: String; var name: String?; var mac: String? }

    var body: some View {
        ManageList(title: "Homelab", problem: data.problem, loading: data.value == nil && data.loading, refresh: { await data.load(model.api) }) {
            Section {
                ForEach(data.value?.services ?? []) { s in
                    HStack {
                        Circle().fill(s.status == "up" ? Theme.teal : s.status == "down" ? Theme.danger : Theme.mist).frame(width: 9, height: 9)
                        TwoLine(title: s.name ?? s.id, detail: [s.target, s.uptime.map { String(format: "%.1f%% up", $0) }, s.avg_ms.map { String(format: "%.0f ms", $0) }].compactMap { $0 }.joined(separator: " · "))
                        Spacer()
                    }
                    .swipeActions(edge: .trailing) {
                        Button("Delete", role: .destructive) {
                            actions.quick("/api/homelab/service-delete", ["id": s.id], ok: "Removed \(s.name ?? "it")") { await data.load(model.api) }
                        }
                        Button("Check") {
                            actions.quick("/api/homelab/check", ["id": s.id], ok: "Checked \(s.name ?? "it")") { await data.load(model.api) }
                        }
                        .tint(Theme.indigo)
                    }
                    .contextMenu {
                        if let l = s.link, let url = URL(string: l) { Link(destination: url) { Label("Open", systemImage: "safari") } }
                    }
                }
                Button { adding = true } label: { Label("Watch a service", systemImage: "plus") }
            } header: { Text("Uptime") } footer: { Text("Swipe a service to check it now or delete it.") }
            .rowStyle()
            Section("Wake-on-LAN") {
                ForEach(data.value?.wol ?? []) { w in
                    HStack {
                        TwoLine(title: w.name ?? w.id, detail: w.mac, symbol: "power")
                        Spacer()
                        Button("Wake") { actions.quick("/api/homelab/wake", ["id": w.id], ok: "Sent the wake signal to \(w.name ?? "it")") }
                            .buttonStyle(.borderedProminent).tint(Theme.indigo).controlSize(.small)
                    }
                    .swipeActions {
                        Button("Delete", role: .destructive) {
                            actions.quick("/api/homelab/wol", ["action": "delete", "id": w.id], ok: "Removed") { await data.load(model.api) }
                        }
                    }
                }
                Button { addingWake = true } label: { Label("Add a machine", systemImage: "plus") }
            }
            .rowStyle()
        }
        .task { if data.value == nil { await data.load(model.api) } }
        .sheet(isPresented: $adding) {
            FormSheet(title: "Watch a service", fields: [("Name", "Jellyfin", false), ("Address", "https://… or host:port", true), ("Group", "Media", false)],
                      choice: ("Check with", ["http", "tcp", "ping"])) { v, kind in
                actions.quick("/api/homelab/service", ["name": v[0], "target": v[1], "group": v[2], "kind": kind, "interval": 60], ok: "Watching \(v[0])") {
                    await data.load(model.api)
                }
            }
        }
        .sheet(isPresented: $addingWake) {
            FormSheet(title: "Add a machine", fields: [("Name", "Gaming PC", false), ("MAC address", "aa:bb:cc:dd:ee:ff", true)], choice: nil) { v, _ in
                actions.quick("/api/homelab/wol", ["name": v[0], "mac": v[1]], ok: "Added \(v[0])") { await data.load(model.api) }
            }
        }
    }
}

/// A small form: a few text fields and an optional segmented choice.
struct FormSheet: View {
    let title: String
    let fields: [(String, String, Bool)]        // label, placeholder, technical (no caps/autocorrect)
    let choice: (String, [String])?
    let save: ([String], String) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var values: [String] = []
    @State private var picked = ""

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    ForEach(Array(fields.enumerated()), id: \.offset) { i, f in
                        TextField(f.0, text: Binding(get: { i < values.count ? values[i] : "" }, set: { if i < values.count { values[i] = $0 } }), prompt: Text(f.1))
                            .textInputAutocapitalization(f.2 ? .never : .words)
                            .autocorrectionDisabled(f.2)
                            .keyboardType(f.2 ? .URL : .default)
                    }
                    if let choice {
                        Picker(choice.0, selection: $picked) {
                            ForEach(choice.1, id: \.self) { Text($0.uppercased()).tag($0) }
                        }
                        .pickerStyle(.segmented)
                    }
                }
                .rowStyle()
            }
            .screen()
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .navigationBarLeading) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .navigationBarTrailing) {
                    Button("Save") { save(values.map { $0.trimmingCharacters(in: .whitespaces) }, picked); dismiss() }
                        .disabled(values.isEmpty || values[0].trimmingCharacters(in: .whitespaces).isEmpty || (values.count > 1 && values[1].trimmingCharacters(in: .whitespaces).isEmpty))
                }
            }
        }
        .presentationDetents([.medium])
        .preferredColorScheme(.dark)
        .onAppear {
            if values.isEmpty { values = Array(repeating: "", count: fields.count) }
            if picked.isEmpty { picked = choice?.1.first ?? "" }
        }
    }
}

// MARK: - Plugins

struct PluginsScreen: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var actions: Actions
    @StateObject private var data = Loader<Reply>("/api/plugins")

    struct Reply: Decodable { var plugins: [Plugin]? }
    struct Plugin: Decodable, Identifiable {
        var id: String
        var name: String?
        var description: String?
        var permission: String?
        var can_disable: Bool?
        var enabled: Bool?
        var status: String?
        var detail: String?
    }

    var body: some View {
        ManageList(title: "Plugins", problem: data.problem, loading: data.value == nil && data.loading, refresh: { await data.load(model.api) }) {
            Section {
                ForEach(data.value?.plugins ?? []) { p in
                    Toggle(isOn: Binding(get: { p.enabled == true }, set: { on in
                        actions.quick("/api/plugins", ["id": p.id, "enabled": on], ok: "\(p.name ?? p.id) \(on ? "on" : "off")") { await data.load(model.api) }
                    })) {
                        VStack(alignment: .leading, spacing: 3) {
                            Text(p.name ?? p.id).font(.system(size: 15, weight: .semibold, design: .rounded))
                            if let d = p.description { Text(d).font(.system(size: 12)).foregroundStyle(Theme.mist) }
                            if let d = p.detail, !d.isEmpty { Text(d).font(.system(size: 11)).foregroundStyle(p.status == "ok" ? Theme.teal : .orange) }
                        }
                    }
                    .tint(Theme.indigo)
                    .disabled(p.can_disable == false)
                }
            }
            .rowStyle()
        }
        .task { if data.value == nil { await data.load(model.api) } }
    }
}

// MARK: - Activity

struct ActivityScreen: View {
    @EnvironmentObject private var model: AppModel
    @StateObject private var events = Loader<EventsReply>("/api/diag/log")
    @StateObject private var audit = Loader<AuditReply>("/api/audit")
    @State private var tab = 0

    struct EventsReply: Decodable { var events: [Event]? }
    struct Event: Decodable, Hashable { var t: Double?; var kind: String?; var level: String?; var title: String?; var detail: String? }
    struct AuditReply: Decodable { var entries: [Entry]? }
    struct Entry: Decodable, Hashable { var time: Double?; var plugin: String?; var route: String?; var actor: String?; var summary: String?; var ok: Bool? }

    var body: some View {
        ManageList(title: "Activity", problem: tab == 0 ? events.problem : audit.problem,
                   loading: (tab == 0 ? events.value == nil && events.loading : audit.value == nil && audit.loading), refresh: load) {
            Picker("", selection: $tab) {
                Text("Events").tag(0)
                Text("Changes").tag(1)
            }
            .pickerStyle(.segmented)
            .listRowBackground(Color.clear)
            if tab == 0 {
                Section {
                    ForEach(Array((events.value?.events ?? []).reversed().prefix(200)), id: \.self) { e in
                        HStack(alignment: .top, spacing: 10) {
                            Circle().fill(e.level == "critical" ? Theme.danger : e.level == "warning" ? .orange : Theme.teal).frame(width: 8, height: 8).padding(.top, 5)
                            VStack(alignment: .leading, spacing: 3) {
                                Text(e.title ?? e.kind ?? "event").font(.system(size: 14, weight: .medium, design: .rounded))
                                if let d = e.detail { Text(d).font(.system(size: 12)).foregroundStyle(Theme.mist) }
                                if let t = e.t { Text(Date(timeIntervalSince1970: t), style: .relative).font(.system(size: 11)).foregroundStyle(Theme.mist) }
                            }
                        }
                    }
                }
                .rowStyle()
            } else {
                Section {
                    if (audit.value?.entries ?? []).isEmpty && audit.value != nil {
                        EmptyNote(symbol: "list.bullet.rectangle", title: "No changes yet", detail: "Changes made through the dashboard or its API show here.")
                    }
                    ForEach(Array((audit.value?.entries ?? []).reversed()), id: \.self) { e in
                        HStack(alignment: .top, spacing: 10) {
                            Image(systemName: e.ok == false ? "xmark.circle.fill" : "checkmark.circle.fill").foregroundStyle(e.ok == false ? Theme.danger : Theme.teal)
                            VStack(alignment: .leading, spacing: 3) {
                                Text(e.summary ?? e.route ?? "change").font(.system(size: 14, weight: .medium, design: .rounded))
                                Text([e.actor, e.plugin].compactMap { $0 }.joined(separator: " · ")).font(.system(size: 12)).foregroundStyle(Theme.mist)
                                if let t = e.time { Text(Date(timeIntervalSince1970: t), style: .relative).font(.system(size: 11)).foregroundStyle(Theme.mist) }
                            }
                        }
                    }
                }
                .rowStyle()
            }
        }
        .task { if events.value == nil { await load() } }
    }

    private func load() async {
        async let a: Void = events.load(model.api)
        async let b: Void = audit.load(model.api)
        _ = await (a, b)
    }
}
