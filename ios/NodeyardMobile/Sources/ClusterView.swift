import SwiftUI

struct ClusterView: View {
    @EnvironmentObject private var cluster: ClusterStore
    @State private var section = Section.pods
    @State private var query = ""
    @State private var namespace = "All"

    enum Section: String, CaseIterable { case nodes = "Nodes", pods = "Pods", workloads = "Apps", services = "Services", events = "Events" }

    var body: some View {
        NavigationStack {
            List {
                Picker("Section", selection: $section) {
                    ForEach(Section.allCases, id: \.self) { Text($0.rawValue) }
                }
                .pickerStyle(.segmented)
                .listRowBackground(Color.clear)
                .listRowInsets(EdgeInsets(top: 4, leading: 16, bottom: 8, trailing: 16))
                content
            }
            .listStyle(.insetGrouped)
            .screen()
            .navigationTitle("Cluster")
            .searchable(text: $query, prompt: "Filter")
            .toolbar {
                if section != .nodes {
                    Menu {
                        Picker("Namespace", selection: $namespace) {
                            Text("All namespaces").tag("All")
                            ForEach(cluster.state?.namespaces ?? [], id: \.self) { Text($0).tag($0) }
                        }
                    } label: {
                        Label(namespace == "All" ? "Namespace" : namespace, systemImage: "line.3.horizontal.decrease.circle")
                    }
                }
            }
            .navigationDestination(for: Node.self) { NodeDetail(name: $0.name) }
            .navigationDestination(for: Pod.self) { PodDetail(pod: $0) }
        }
    }

    private func match(_ ns: String?, _ texts: String?...) -> Bool {
        if namespace != "All" && ns != namespace { return false }
        if query.isEmpty { return true }
        return texts.contains { ($0 ?? "").localizedCaseInsensitiveContains(query) }
    }

    @ViewBuilder private var content: some View {
        switch section {
        case .nodes:
            ForEach(cluster.nodes.filter { match(nil, $0.name, $0.internal_ip) }) { n in
                NavigationLink(value: n) {
                    Row(health: n.isReady ? .good : .bad, title: n.name,
                        detail: "\(n.role) · \(n.internal_ip ?? "")", trailing: Format.percent(n.cpuFraction) + " CPU")
                }
                .rowStyle()
            }
        case .pods:
            let pods = cluster.pods.filter { match($0.namespace, $0.name, $0.node, $0.status) }
                .sorted { rank($0.health) == rank($1.health) ? $0.name < $1.name : rank($0.health) < rank($1.health) }
            if pods.isEmpty { EmptyNote(symbol: "shippingbox", title: "No pods match").listRowBackground(Color.clear) }
            ForEach(pods) { p in
                NavigationLink(value: p) {
                    Row(health: p.health, title: p.name,
                        detail: "\(p.namespace) · \(p.status ?? "") · \(p.ready ?? "")",
                        trailing: (p.restarts ?? 0) > 0 ? "↻ \(p.restarts ?? 0)" : Format.age(p.created))
                }
                .rowStyle()
            }
        case .workloads:
            ForEach((cluster.state?.workloads ?? []).filter { match($0.namespace, $0.name, $0.kind) }) { w in
                Row(health: w.healthy ? .good : .warning, title: w.name,
                    detail: "\(w.kind ?? "") · \(w.namespace)", trailing: "\(w.ready ?? 0)/\(w.desired ?? 0)")
                    .rowStyle()
            }
        case .services:
            ForEach((cluster.state?.services ?? []).filter { match($0.namespace, $0.name, $0.type) }) { s in
                VStack(alignment: .leading, spacing: 4) {
                    Row(health: (s.endpoints ?? 1) > 0 ? .good : .warning, title: s.name,
                        detail: "\(s.type ?? "") · \(s.namespace)", trailing: s.cluster_ip ?? "")
                    if let ports = s.ports, !ports.isEmpty {
                        Text(ports.joined(separator: "  ")).font(.system(size: 12, design: .monospaced)).foregroundStyle(Theme.mist)
                            .padding(.leading, 21)
                    }
                }
                .rowStyle()
            }
        case .events:
            let events = (cluster.state?.events ?? []).filter { match($0.namespace, $0.reason, $0.message, $0.object) }
            if events.isEmpty { EmptyNote(symbol: "checkmark.seal", title: "No recent events").listRowBackground(Color.clear) }
            ForEach(events) { e in
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        Image(systemName: e.warning ? "exclamationmark.triangle.fill" : "info.circle")
                            .foregroundStyle(e.warning ? .orange : Theme.teal)
                        Text(e.reason ?? "").font(.system(size: 15, weight: .semibold, design: .rounded))
                        Spacer()
                        Text(Format.age(e.last)).font(.system(size: 12)).foregroundStyle(Theme.mist)
                    }
                    Text(e.object ?? "").font(.system(size: 12, design: .monospaced)).foregroundStyle(Theme.mist)
                    Text(e.message ?? "").font(.system(size: 13)).lineLimit(4)
                }
                .padding(.vertical, 2)
                .rowStyle()
            }
        }
    }

    private func rank(_ h: Health) -> Int { h == .bad ? 0 : h == .warning ? 1 : 2 }
}

struct Row: View {
    let health: Health
    let title: String
    let detail: String
    var trailing: String = ""
    var body: some View {
        HStack(spacing: 12) {
            HealthDot(health: health)
            VStack(alignment: .leading, spacing: 3) {
                Text(title).font(.system(size: 15, weight: .semibold, design: .rounded)).lineLimit(1)
                Text(detail).font(.system(size: 12, design: .rounded)).foregroundStyle(Theme.mist).lineLimit(1)
            }
            Spacer(minLength: 6)
            Text(trailing).font(.system(size: 12, design: .rounded).monospacedDigit()).foregroundStyle(Theme.mist)
        }
        .padding(.vertical, 2)
    }
}

// MARK: - Details

struct NodeDetail: View {
    let name: String
    @EnvironmentObject private var cluster: ClusterStore

    var body: some View {
        let node = cluster.nodes.first { $0.name == name }
        ScrollView {
            if let n = node {
                VStack(alignment: .leading, spacing: 16) {
                    Panel {
                        HStack {
                            Ring(fraction: n.cpuFraction, label: "CPU", detail: "\(Format.cores(n.cpu_used))/\(Format.cores(n.cpu_cores))", size: 84)
                            Spacer()
                            Ring(fraction: n.memFraction, label: "Memory", detail: Format.bytes(n.mem_used), size: 84)
                            Spacer()
                            Ring(fraction: n.diskFraction, label: "Disk", detail: Format.bytes(n.disk_used), size: 84)
                        }
                    }
                    Panel {
                        VStack(spacing: 10) {
                            Fact("Status", n.isReady ? "Ready" : (n.status ?? "Not ready"))
                            Fact("Role", n.role)
                            Fact("Address", n.internal_ip)
                            Fact("Pods", "\(n.pods_running ?? 0) running of \(n.pods_capacity ?? 0)")
                            Fact("Network", "↓ \(Format.rate(n.net_rx_rate))  ↑ \(Format.rate(n.net_tx_rate))")
                            if let hw = n.hw {
                                Fact("CPU", hw.cpu_model)
                                if let t = hw.temp_c { Fact("Temperature", "\(Int(t)) °C") }
                                if let f = hw.freq_mhz { Fact("Clock", "\(Int(f)) MHz") }
                                if let l = hw.load, !l.isEmpty { Fact("Load", l.prefix(3).map { String(format: "%.2f", $0) }.joined(separator: "  ")) }
                                if let u = hw.uptime { Fact("Up for", Format.duration(u)) }
                                if hw.undervoltage == true { Fact("Power", "Undervoltage seen") }
                            }
                            Fact("OS", n.os)
                            Fact("Kernel", n.kernel)
                            Fact("Kubelet", n.kubelet)
                            Fact("Runtime", n.runtime)
                            Fact("Age", Format.age(n.created))
                        }
                    }
                    let pods = cluster.pods.filter { $0.node == n.name }
                    if !pods.isEmpty {
                        Text("Pods").font(Theme.title(20))
                        VStack(spacing: 0) {
                            ForEach(pods) { p in
                                NavigationLink(value: p) {
                                    Row(health: p.health, title: p.name, detail: p.namespace, trailing: p.status ?? "")
                                        .padding(.horizontal, 14).padding(.vertical, 8)
                                        .contentShape(Rectangle())
                                }
                                .buttonStyle(.plain)
                                if p.id != pods.last?.id { Divider().overlay(Theme.line).padding(.leading, 35) }
                            }
                        }
                        .background(Theme.panel, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
                    }
                }
                .padding(16)
            } else {
                EmptyNote(symbol: "server.rack", title: "Node is gone", detail: "It isn't in the cluster any more.")
            }
        }
        .screen()
        .navigationTitle(name)
        .navigationBarTitleDisplayMode(.inline)
    }
}

struct Fact: View {
    let label: String
    let value: String?
    init(_ label: String, _ value: String?) { self.label = label; self.value = value }
    var body: some View {
        if let value, !value.isEmpty {
            HStack(alignment: .firstTextBaseline) {
                Text(label).foregroundStyle(Theme.mist)
                Spacer(minLength: 16)
                Text(value).multilineTextAlignment(.trailing).textSelection(.enabled)
            }
            .font(.system(size: 14, design: .rounded))
        }
    }
}

struct PodDetail: View {
    let pod: Pod
    @EnvironmentObject private var cluster: ClusterStore

    var body: some View {
        let p = cluster.pods.first { $0.id == pod.id } ?? pod
        List {
            Section {
                Fact("Status", p.status)
                Fact("Ready", p.ready)
                Fact("Namespace", p.namespace)
                Fact("Node", p.node)
                Fact("Pod IP", p.ip)
                Fact("Restarts", "\(p.restarts ?? 0)")
                Fact("Owner", p.owner)
                Fact("Age", Format.age(p.created))
                if let cpu = p.cpu { Fact("CPU", String(format: "%.0f m", cpu * 1000)) }
                if let mem = p.mem { Fact("Memory", Format.bytes(mem)) }
            }
            .rowStyle()
            Section("Containers") {
                ForEach(p.containers ?? [], id: \.name) { c in
                    NavigationLink {
                        LogsView(namespace: p.namespace, pod: p.name, container: c.name)
                    } label: {
                        VStack(alignment: .leading, spacing: 3) {
                            HStack {
                                HealthDot(health: c.ready == true ? .good : (c.state == "running" ? .warning : .bad))
                                Text(c.name).font(.system(size: 15, weight: .semibold, design: .rounded))
                                Spacer()
                                Text("Logs").font(.system(size: 13)).foregroundStyle(Theme.teal)
                            }
                            Text(c.image ?? "").font(.system(size: 11, design: .monospaced)).foregroundStyle(Theme.mist).lineLimit(1)
                            if let r = c.reason, !r.isEmpty { Text(r).font(.system(size: 12)).foregroundStyle(.orange) }
                        }
                    }
                }
            }
            .rowStyle()
        }
        .listStyle(.insetGrouped)
        .screen()
        .navigationTitle(p.name)
        .navigationBarTitleDisplayMode(.inline)
    }
}

struct LogsView: View {
    let namespace: String
    let pod: String
    let container: String
    @EnvironmentObject private var model: AppModel
    @State private var text = ""
    @State private var loading = true
    @State private var problem: String?
    @State private var follow = true

    private struct Reply: Decodable { var text: String? }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView([.vertical, .horizontal]) {
                VStack(alignment: .leading, spacing: 0) {
                    if let problem { ErrorBanner(text: problem).padding() }
                    Text(text.isEmpty && !loading ? "No log lines yet." : text)
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(Color(hex: 0xD6DEF5))
                        .textSelection(.enabled)
                        .padding(12)
                    Color.clear.frame(height: 1).id("end")
                }
            }
            .onChange(of: text) { _ in if follow { proxy.scrollTo("end", anchor: .bottom) } }
        }
        .overlay { if loading && text.isEmpty { ProgressView() } }
        .screen()
        .navigationTitle(container)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItemGroup(placement: .navigationBarTrailing) {
                Button { follow.toggle() } label: {
                    Image(systemName: follow ? "pause.circle" : "play.circle")
                }
                .accessibilityLabel(follow ? "Stop following" : "Follow")
                ShareLink(item: text) { Image(systemName: "square.and.arrow.up") }
            }
        }
        .task {
            while !Task.isCancelled {
                if follow { await load() }
                try? await Task.sleep(nanoseconds: 3_000_000_000)
            }
        }
    }

    private func load() async {
        guard let api = model.api else { return }
        do {
            let r = try await api.get("/api/logs", ["ns": namespace, "pod": pod, "container": container, "lines": "500"], as: Reply.self)
            if r.text != text { text = r.text ?? "" }
            problem = nil
        } catch {
            problem = error.localizedDescription
        }
        loading = false
    }
}
