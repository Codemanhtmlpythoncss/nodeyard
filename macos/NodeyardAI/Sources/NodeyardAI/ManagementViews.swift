import AppKit
import SwiftUI

/// One row of dashboard data, for SwiftUI lists and tables.
struct Row: Identifiable, Hashable {
    let id: String
    let v: JSON
}

private func rows(_ items: [JSON], key: (JSON) -> String) -> [Row] {
    var seen = Set<String>()
    return items.map { item in
        var id = key(item)
        while seen.contains(id) { id += "'" }
        seen.insert(id)
        return Row(id: id, v: item)
    }
}

// MARK: - sidebar and detail

struct ManageSidebar: View {
    @EnvironmentObject private var mgmt: ManagementState

    var body: some View {
        List(selection: Binding(get: { mgmt.section }, set: { if let s = $0 { mgmt.section = s } })) {
            Section("Cluster") {
                ForEach([ManageSection.overview, .nodes, .pods, .workloads, .network, .storage, .hardware]) { item($0) }
            }
            Section("AI") { item(.models) }
            Section("Health") { ForEach([ManageSection.alerts, .events, .doctor, .tasks]) { item($0) } }
            Section("Help") { item(.guide) }
        }
        .listStyle(.sidebar)
        .safeAreaInset(edge: .bottom) {
            HStack(spacing: 8) {
                Circle().fill(mgmt.snapshot != nil && mgmt.error == nil ? Color.green : Color.orange).frame(width: 8, height: 8)
                VStack(alignment: .leading, spacing: 1) {
                    Text(mgmt.snapshot == nil ? "Not connected" : (mgmt.isDemo ? "Demo cluster" : "Connected")).font(.caption)
                    if let updated = mgmt.updated { Text("Updated \(updated, style: .relative) ago").font(.caption2).foregroundStyle(.tertiary) }
                }
                Spacer()
            }.padding(12)
        }
    }

    private func item(_ s: ManageSection) -> some View {
        Label(s.title, systemImage: s.icon)
            .badge(s == .alerts ? mgmt.alerts.filter { $0["level"].text != "info" }.count : (s == .tasks ? mgmt.jobs.filter { $0.status == "running" }.count : 0))
            .tag(s)
    }
}

struct ManageDetail: View {
    @EnvironmentObject private var mgmt: ManagementState

    var body: some View {
        Group {
            if mgmt.section == .guide { SetupGuideView() }
            else if mgmt.authEnabled && !mgmt.signedIn && mgmt.snapshot == nil { SignInPanel() }
            else {
                switch mgmt.section {
                case .overview: OverviewView()
                case .nodes: NodesView()
                case .pods: PodsView()
                case .workloads: WorkloadsView()
                case .network: NetworkView()
                case .storage: StorageView()
                case .hardware: HardwareView()
                case .models: ModelsManageView()
                case .events: EventsView()
                case .alerts: AlertsView()
                case .doctor: DoctorView()
                case .tasks: TasksView()
                case .guide: SetupGuideView()
                }
            }
        }
        .safeAreaInset(edge: .top) {
            if let error = mgmt.error, mgmt.snapshot != nil {
                Label(error, systemImage: "exclamationmark.triangle.fill").font(.callout).foregroundStyle(.orange)
                    .frame(maxWidth: .infinity, alignment: .leading).padding(10).background(.orange.opacity(0.08))
            }
        }
        .sheet(isPresented: Binding(get: { mgmt.shownJobID != nil }, set: { if !$0 { mgmt.shownJobID = nil } })) {
            if let job = mgmt.jobs.first(where: { $0.id == mgmt.shownJobID }) { JobSheet(job: job).environmentObject(mgmt).frame(width: 640, height: 460) }
        }
        .onAppear { mgmt.startPolling() }
        .onDisappear { mgmt.stopPolling() }
    }
}

struct SignInPanel: View {
    @EnvironmentObject private var mgmt: ManagementState
    @State private var password = ""
    @State private var remember = true

    var body: some View {
        VStack(spacing: 14) {
            NodeyardMark(size: 46)
            Text("Sign in to manage your cluster").font(.title2.weight(.semibold))
            Text("Managing nodes, models and settings needs the dashboard password: the one you type on the website's sign-in page. It is not the server API key. Chat and model status keep working with the API key alone.")
                .multilineTextAlignment(.center).foregroundStyle(.secondary).frame(maxWidth: 460)
            SecureField("Dashboard password", text: $password).textFieldStyle(.roundedBorder).frame(width: 300).onSubmit(signIn)
            Toggle("Remember it in this Mac's Keychain", isOn: $remember).frame(width: 300, alignment: .leading)
            Button(mgmt.signingIn ? "Signing in…" : "Sign in", action: signIn).buttonStyle(.borderedProminent).disabled(password.isEmpty || mgmt.signingIn)
            if let error = mgmt.error { Text(error).font(.callout).foregroundStyle(.red).multilineTextAlignment(.center).frame(maxWidth: 460) }
        }.frame(maxWidth: .infinity, maxHeight: .infinity).padding(30)
    }

    private func signIn() {
        guard !password.isEmpty else { return }
        let value = password
        Task { await mgmt.signIn(password: value, remember: remember); password = "" }
    }
}

// MARK: - shared pieces

struct SectionHeader: View {
    let title: String
    let subtitle: String
    var trailing: AnyView? = nil
    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            VStack(alignment: .leading, spacing: 3) { Text(title).font(.largeTitle.bold()); Text(subtitle).foregroundStyle(.secondary) }
            Spacer()
            if let trailing { trailing }
        }
    }
}

struct StatTile: View {
    let title: String
    let value: String
    var detail: String = ""
    var fraction: Double? = nil
    var tint: Color = .accentColor
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.caption).foregroundStyle(.secondary)
            Text(value).font(.title2.weight(.semibold)).monospacedDigit()
            if let fraction { Meter(fraction: fraction, tint: fraction > 0.9 ? .red : fraction > 0.8 ? .orange : tint) }
            if !detail.isEmpty { Text(detail).font(.caption2).foregroundStyle(.tertiary).lineLimit(2) }
        }.padding(14).frame(maxWidth: .infinity, minHeight: 92, alignment: .topLeading).background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 12))
    }
}

/// A titled card, like the website's: plain SwiftUI so it looks the same everywhere (and renders in snapshots).
struct Card<Content: View, Label: View>: View {
    let content: Content
    let label: Label
    init(@ViewBuilder content: () -> Content, @ViewBuilder label: () -> Label) { self.content = content(); self.label = label() }
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            label.font(.subheadline.weight(.semibold)).foregroundStyle(.secondary).textCase(.uppercase)
            content
        }
        .padding(16).frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 14).fill(Color(nsColor: .controlBackgroundColor)))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(Color.secondary.opacity(0.18)))
    }
}

extension Card where Label == Text {
    init(_ title: String, @ViewBuilder content: () -> Content) { self.init(content: content, label: { Text(title) }) }
}

/// A thin progress bar (plain SwiftUI).
struct Meter: View {
    let fraction: Double
    var tint: Color = .accentColor
    var body: some View {
        GeometryReader { g in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.secondary.opacity(0.18))
                Capsule().fill(tint).frame(width: max(0, min(1, fraction)) * g.size.width)
            }
        }.frame(height: 6).accessibilityValue(Text("\(Int(fraction * 100)) percent"))
    }
}

struct StatusPill: View {
    let text: String
    var tone: Color = .secondary
    var body: some View {
        Text(text).font(.caption.weight(.medium)).padding(.horizontal, 8).padding(.vertical, 3)
            .foregroundStyle(tone).background(tone.opacity(0.12), in: Capsule())
    }
}

func statusTone(_ status: String) -> Color {
    if ["Running", "Succeeded", "Completed", "Ready", "Bound", "ok", "good"].contains(status) { return .green }
    if status.hasPrefix("Pending") || status.hasPrefix("ContainerCreating") || status.hasPrefix("Init:") || ["Terminating", "warn", "warning", "running"].contains(status) { return .orange }
    return .red
}

func levelTone(_ level: String) -> Color { level == "critical" ? .red : level == "warning" ? .orange : .blue }

/// A confirmation in front of anything that changes the cluster.
struct Confirm: Identifiable {
    let id = UUID()
    let title: String
    let message: String
    let button: String
    var destructive = true
    let action: @MainActor () -> Void
}

extension View {
    func confirm(_ item: Binding<Confirm?>) -> some View {
        alert(item.wrappedValue?.title ?? "", isPresented: Binding(get: { item.wrappedValue != nil }, set: { if !$0 { item.wrappedValue = nil } })) {
            if let c = item.wrappedValue {
                Button(c.button, role: c.destructive ? .destructive : nil) { c.action(); item.wrappedValue = nil }
                Button("Cancel", role: .cancel) { item.wrappedValue = nil }
            }
        } message: { Text(item.wrappedValue?.message ?? "") }
    }
}

// MARK: - overview

struct OverviewView: View {
    @EnvironmentObject private var mgmt: ManagementState
    private let columns = [GridItem(.adaptive(minimum: 170), spacing: 12, alignment: .top)]

    var body: some View {
        Scrolling {
            VStack(alignment: .leading, spacing: 18) {
                SectionHeader(title: mgmt.state["cluster"]["name"].string ?? "Overview", subtitle: "Your cluster at a glance", trailing: AnyView(
                    Button { Task { await mgmt.refresh() } } label: { Label("Refresh", systemImage: "arrow.clockwise") }))
                let t = mgmt.totals
                LazyVGrid(columns: columns, spacing: 12) {
                    StatTile(title: "Nodes ready", value: "\(t["nodes_ready"].int ?? 0) of \(t["nodes"].int ?? 0)", tint: .green)
                    StatTile(title: "CPU", value: Format.percent(t["cpu_used"].double, of: t["cpu_total"].double), detail: String(format: "%.1f of %.0f cores", t["cpu_used"].double ?? 0, t["cpu_total"].double ?? 0), fraction: Format.ratio(t["cpu_used"].double, of: t["cpu_total"].double))
                    StatTile(title: "Memory", value: Format.percent(t["mem_used"].double, of: t["mem_total"].double), detail: "\(Format.bytes(t["mem_used"].double)) of \(Format.bytes(t["mem_total"].double))", fraction: Format.ratio(t["mem_used"].double, of: t["mem_total"].double))
                    StatTile(title: "Disk", value: Format.percent(t["disk_used"].double, of: t["disk_total"].double), detail: "\(Format.bytes(t["disk_used"].double)) of \(Format.bytes(t["disk_total"].double))", fraction: Format.ratio(t["disk_used"].double, of: t["disk_total"].double))
                    StatTile(title: "Pods running", value: "\(t["pods_running"].int ?? 0) of \(t["pods"].int ?? 0)", detail: "\(t["restarts"].int ?? 0) restarts in total")
                    StatTile(title: "Alerts", value: "\(mgmt.alerts.filter { $0["level"].text != "info" }.count)", detail: "\(mgmt.alerts.filter { $0["level"].text == "critical" }.count) critical", tint: .orange)
                }
                Card("AI model") { SplitSummary().frame(maxWidth: .infinity, alignment: .leading) }
                if !mgmt.alerts.isEmpty {
                    Card("Needs attention") {
                        VStack(alignment: .leading, spacing: 8) { ForEach(rows(Array(mgmt.alerts.prefix(6)), key: { $0["title"].text })) { AlertRow(alert: $0.v) } }
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
                Card("Recent events") {
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(rows(Array(mgmt.events.prefix(8)), key: { $0["object"].text + $0["reason"].text + $0["last"].text })) { EventRow(event: $0.v) }
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }
            }.padding(24)
        }
    }
}

struct SplitSummary: View {
    @EnvironmentObject private var mgmt: ManagementState
    var body: some View {
        let sp = mgmt.split
        if sp.isNull {
            Text("No model is split across your machines. Run one from AI models.").foregroundStyle(.secondary)
        } else {
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Text(sp["alias"].string ?? sp["model"].text).font(.headline)
                    let state = sp["loaded"].bool == false ? "Unloaded" : (sp["ready"].bool == true ? "Serving" : (sp["download"].text == "running" ? "Downloading" : "Loading"))
                    StatusPill(text: state, tone: state == "Serving" ? .green : .orange)
                }
                Text(sp["model"].text).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                Text("Context \(sp["ctx"].text) · on \(sp["shares"].array.map { $0["node"].text }.joined(separator: ", "))").font(.caption).foregroundStyle(.tertiary)
                let restarts = sp["pods"].array.filter { ($0["restarts"].int ?? 0) > 0 }
                if !restarts.isEmpty {
                    Text("Restarts: " + restarts.map { "\($0["name"].text) ×\($0["restarts"].int ?? 0)" + ($0["last_reason"].text.isEmpty ? "" : " (\($0["last_reason"].text))") }.joined(separator: ", "))
                        .font(.caption).foregroundStyle(.orange)
                }
            }
        }
    }
}

struct AlertRow: View {
    let alert: JSON
    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: alert["level"].text == "critical" ? "xmark.octagon.fill" : alert["level"].text == "warning" ? "exclamationmark.triangle.fill" : "info.circle.fill")
                .foregroundStyle(levelTone(alert["level"].text))
            VStack(alignment: .leading, spacing: 2) {
                Text(alert["title"].text).font(.callout.weight(.medium))
                Text(alert["detail"].text).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

struct EventRow: View {
    let event: JSON
    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            StatusPill(text: event["type"].text, tone: event["type"].text == "Warning" ? .orange : .secondary)
            VStack(alignment: .leading, spacing: 2) {
                Text("\(event["reason"].text) · \(event["object"].text)").font(.callout.weight(.medium))
                Text(event["message"].text).font(.caption).foregroundStyle(.secondary).lineLimit(3)
            }
            Spacer()
            Text(Format.ago(event["last"].double)).font(.caption2).foregroundStyle(.tertiary)
        }
    }
}

// MARK: - nodes

struct NodesView: View {
    @EnvironmentObject private var mgmt: ManagementState
    @State private var selection: Row.ID?
    @State private var confirm: Confirm?

    var body: some View {
        let list = rows(mgmt.nodes, key: { $0["name"].text })
        VStack(alignment: .leading, spacing: 14) {
            SectionHeader(title: "Nodes", subtitle: "Every machine, its address and its load", trailing: AnyView(HStack {
                Button("Restart Kubernetes…") { confirm = Confirm(title: "Restart Kubernetes on every node?", message: "k3s restarts on each worker one at a time, then on the control node. Pods move or restart; models reload.", button: "Restart") { Task { await mgmt.run("restart-cluster", title: "Restart Kubernetes on every node") } } }
                Button("Reboot every machine…") { confirm = Confirm(title: "Reboot every machine?", message: "Each worker is drained and rebooted one at a time, then the control server. Everything stops for a while.", button: "Reboot") { Task { await mgmt.run("reboot-cluster", title: "Reboot every machine in the cluster") } } }
            }.disabled(mgmt.busyJob != nil)))
            Table(list, selection: $selection) {
                TableColumn("Name") { r in HStack { Circle().fill(r.v["ready"].bool == true ? Color.green : Color.red).frame(width: 8, height: 8); Text(r.v["name"].text).fontWeight(.medium) } }
                TableColumn("Status") { r in StatusPill(text: r.v["status"].text, tone: r.v["ready"].bool == true ? .green : .red) }.width(90)
                TableColumn("Roles") { r in Text(r.v["roles"].array.map(\.text).joined(separator: ", ").ifEmpty("worker")).foregroundStyle(.secondary) }
                TableColumn("Address") { r in Text(r.v["internal_ip"].text).monospaced().textSelection(.enabled) }
                TableColumn("CPU") { r in Text(Format.percent(r.v["cpu_used"].double, of: r.v["cpu_cores"].double)).monospacedDigit() }.width(55)
                TableColumn("Memory") { r in Text(Format.percent(r.v["mem_used"].double, of: r.v["mem_total"].double)).monospacedDigit() }.width(65)
                TableColumn("Disk") { r in Text(Format.percent(r.v["disk_used"].double, of: r.v["disk_total"].double)).monospacedDigit() }.width(55)
                TableColumn("Temp") { r in Text(r.v["hw"]["temp_c"].double.map { String(format: "%.0f °C", $0) } ?? "–").monospacedDigit() }.width(60)
            }
            if let node = list.first(where: { $0.id == selection })?.v {
                HStack(alignment: .top, spacing: 12) {
                    NodeDetail(node: node)
                    ConnectionCard(record: mgmt.connections.first { $0["name"].text == node["name"].text })
                }.frame(maxHeight: 280)
            }
        }.padding(24).confirm($confirm)
        .task { await mgmt.refreshConnections() }
    }
}

struct NodeDetail: View {
    let node: JSON
    var body: some View {
        Card(node["name"].text) {
            Scrolling {
                Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 18, verticalSpacing: 6) {
                    detail("Status", node["ready"].bool == true ? "Ready" : "Not ready" + (node["ready_reason"].text.isEmpty ? "" : " (\(node["ready_reason"].text): \(node["ready_message"].text))"))
                    if !node["ready_since"].isNull, node["ready"].bool != true { detail("Not ready since", Format.ago(node["ready_since"].double)) }
                    detail("Addresses", node["addresses"].array.map { "\($0["type"].text) \($0["address"].text)" }.joined(separator: " · "))
                    detail("System", "\(node["os"].text) · \(node["kernel"].text) · \(node["arch"].text)")
                    detail("Kubernetes", "\(node["kubelet"].text) · \(node["runtime"].text)")
                    detail("Pods", "\(node["pods_running"].int ?? 0) running of \(node["pods_capacity"].int ?? 0)")
                    detail("Memory", "\(Format.bytes(node["mem_used"].double)) of \(Format.bytes(node["mem_total"].double))")
                    detail("Disk", "\(Format.bytes(node["disk_used"].double)) of \(Format.bytes(node["disk_total"].double))")
                    if !node["hw"]["cpu_model"].isNull { detail("CPU", "\(node["hw"]["cpu_model"].text) · \(node["hw"]["cores"].int ?? 0) cores") }
                    detail("Conditions", node["conditions"].object.sorted { $0.key < $1.key }.map { "\($0.key) \($0.value.text)" }.joined(separator: " · "))
                }.frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 4)
            }
        }
    }
    private func detail(_ k: String, _ v: String) -> some View {
        GridRow { Text(k).foregroundStyle(.secondary); Text(v.isEmpty ? "–" : v).textSelection(.enabled) }
    }
}

extension String { func ifEmpty(_ other: String) -> String { isEmpty ? other : self } }

// MARK: - pods and workloads

struct PodsView: View {
    @EnvironmentObject private var mgmt: ManagementState
    @State private var search = ""
    @State private var selection: Row.ID?
    @State private var logText = ""
    @State private var loadingLogs = false

    var body: some View {
        let list = rows(mgmt.pods.filter { search.isEmpty || $0["name"].text.localizedCaseInsensitiveContains(search) || $0["namespace"].text.localizedCaseInsensitiveContains(search) || $0["node"].text.localizedCaseInsensitiveContains(search) },
                        key: { $0["namespace"].text + "/" + $0["name"].text })
        VStack(alignment: .leading, spacing: 14) {
            SectionHeader(title: "Pods", subtitle: "Everything that is running, with live usage and logs", trailing: AnyView(TextField("Filter by name, namespace or node", text: $search).textFieldStyle(.roundedBorder).frame(width: 260)))
            Table(list, selection: $selection) {
                TableColumn("Name") { r in Text(r.v["name"].text).fontWeight(.medium).textSelection(.enabled) }
                TableColumn("Namespace") { r in Text(r.v["namespace"].text).foregroundStyle(.secondary) }.width(110)
                TableColumn("Status") { r in StatusPill(text: r.v["status"].text, tone: statusTone(r.v["status"].text)) }.width(140)
                TableColumn("Ready") { r in Text(r.v["ready"].text).monospacedDigit() }.width(50)
                TableColumn("Restarts") { r in Text("\(r.v["restarts"].int ?? 0)" + (r.v["last_reason"].text.isEmpty ? "" : " · \(r.v["last_reason"].text)")).monospacedDigit() }.width(110)
                TableColumn("Node") { r in Text(r.v["node"].text) }.width(110)
                TableColumn("CPU") { r in Text(r.v["cpu"].double.map { String(format: "%.2f", $0) } ?? "–").monospacedDigit() }.width(50)
                TableColumn("Memory") { r in Text(Format.bytes(r.v["mem"].double)).monospacedDigit() }.width(80)
            }
            if let pod = list.first(where: { $0.id == selection })?.v {
                Card("Pod") {
                    VStack(alignment: .leading, spacing: 8) {
                        HStack {
                            Text("\(pod["namespace"].text)/\(pod["name"].text)").font(.headline).textSelection(.enabled)
                            Spacer()
                            Button(loadingLogs ? "Reading logs…" : "Show logs") {
                                loadingLogs = true
                                Task { logText = await mgmt.logs(namespace: pod["namespace"].text, pod: pod["name"].text); loadingLogs = false }
                            }.disabled(loadingLogs)
                        }
                        Text(pod["containers"].array.map { "\($0["name"].text): \($0["state"].text)\($0["reason"].text.isEmpty ? "" : " (\($0["reason"].text))") · \($0["image"].text)" }.joined(separator: "\n"))
                            .font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                        if !logText.isEmpty {
                            Scrolling { Text(logText).font(.system(.caption, design: .monospaced)).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading) }.frame(height: 180)
                        }
                    }
                }.onChange(of: selection) { _ in logText = "" }
            }
        }.padding(24)
    }
}

struct WorkloadsView: View {
    @EnvironmentObject private var mgmt: ManagementState
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            SectionHeader(title: "Workloads", subtitle: "Deployments, daemon sets, stateful sets, jobs and cron jobs")
            Table(rows(mgmt.workloads, key: { $0["kind"].text + $0["namespace"].text + $0["name"].text })) {
                TableColumn("Kind") { r in Text(r.v["kind"].text).foregroundStyle(.secondary) }.width(110)
                TableColumn("Name") { r in Text(r.v["name"].text).fontWeight(.medium) }
                TableColumn("Namespace") { r in Text(r.v["namespace"].text) }.width(120)
                TableColumn("Ready") { r in
                    let ok = (r.v["ready"].int ?? 0) >= (r.v["desired"].int ?? 0)
                    Text("\(r.v["ready"].int ?? 0)/\(r.v["desired"].int ?? 0)").monospacedDigit().foregroundStyle(ok ? Color.primary : Color.orange)
                }.width(60)
                TableColumn("Images") { r in Text(r.v["images"].array.map(\.text).joined(separator: ", ")).font(.caption).foregroundStyle(.secondary).lineLimit(1) }
            }
        }.padding(24)
    }
}

struct NetworkView: View {
    @EnvironmentObject private var mgmt: ManagementState
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            SectionHeader(title: "Network", subtitle: "Services, their addresses and ports")
            Table(rows(mgmt.services, key: { $0["namespace"].text + "/" + $0["name"].text })) {
                TableColumn("Service") { r in Text(r.v["name"].text).fontWeight(.medium) }
                TableColumn("Namespace") { r in Text(r.v["namespace"].text).foregroundStyle(.secondary) }.width(120)
                TableColumn("Type") { r in Text(r.v["type"].text) }.width(100)
                TableColumn("Cluster IP") { r in Text(r.v["cluster_ip"].text).monospaced().textSelection(.enabled) }.width(120)
                TableColumn("Ports") { r in Text((r.v["ports"].array + r.v["node_ports"].array.map { .string("node \($0.text)") }).map(\.text).joined(separator: ", ")).monospaced() }
                TableColumn("Endpoints") { r in Text("\(r.v["endpoints"].int ?? 0)").monospacedDigit() }.width(70)
            }
        }.padding(24)
    }
}

struct StorageView: View {
    @EnvironmentObject private var mgmt: ManagementState
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            SectionHeader(title: "Storage", subtitle: "Disks and volumes")
            ScrollView(.horizontal) {
                HStack(spacing: 12) {
                    ForEach(rows(mgmt.nodes, key: { $0["name"].text })) { r in
                        StatTile(title: r.v["name"].text, value: Format.percent(r.v["disk_used"].double, of: r.v["disk_total"].double),
                                 detail: "\(Format.bytes(r.v["disk_used"].double)) of \(Format.bytes(r.v["disk_total"].double))", fraction: Format.ratio(r.v["disk_used"].double, of: r.v["disk_total"].double)).frame(width: 190)
                    }
                }
            }
            Table(rows(mgmt.volumes, key: { $0["kind"].text + $0["namespace"].text + $0["name"].text })) {
                TableColumn("Kind") { r in Text(r.v["kind"].text) }.width(50)
                TableColumn("Name") { r in Text(r.v["name"].text).fontWeight(.medium) }
                TableColumn("Namespace") { r in Text(r.v["namespace"].text).foregroundStyle(.secondary) }.width(110)
                TableColumn("Status") { r in StatusPill(text: r.v["status"].text, tone: statusTone(r.v["status"].text)) }.width(90)
                TableColumn("Size") { r in Text(Format.bytes(r.v["capacity"].double)).monospacedDigit() }.width(90)
                TableColumn("Class") { r in Text(r.v["storage_class"].text) }.width(100)
            }
        }.padding(24)
    }
}

struct HardwareView: View {
    @EnvironmentObject private var mgmt: ManagementState
    var body: some View {
        Scrolling {
            VStack(alignment: .leading, spacing: 14) {
                SectionHeader(title: "Hardware", subtitle: "What every machine is made of, from the node agents")
                ForEach(rows(mgmt.nodes, key: { $0["name"].text })) { r in
                    let hw = r.v["hw"]
                    Card(r.v["name"].text) {
                        if hw.isNull {
                            Text("No node agent data. Install the agents from the website's Hardware page (or `sudo nodeyard dashboard agent install`).").foregroundStyle(.secondary).frame(maxWidth: .infinity, alignment: .leading)
                        } else {
                            Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 18, verticalSpacing: 5) {
                                GridRow { Text("CPU").foregroundStyle(.secondary); Text("\(hw["cpu_model"].text) · \(hw["cores"].int ?? 0) cores · \(hw["freq_mhz"].int ?? 0) MHz (\(hw["governor"].text))") }
                                GridRow { Text("Use").foregroundStyle(.secondary); Text(String(format: "%.0f%% CPU · load %@", hw["cpu_use"].double ?? 0, hw["load"].array.compactMap(\.double).map { String(format: "%.2f", $0) }.joined(separator: " "))) }
                                GridRow { Text("Memory").foregroundStyle(.secondary); Text("\(Format.bytes(hw["mem_available"].double)) available of \(Format.bytes(hw["mem_total"].double)) · swap \(Format.bytes(hw["swap_total"].double))") }
                                GridRow { Text("Temperature").foregroundStyle(.secondary); Text(hw["temp_c"].double.map { String(format: "%.1f °C", $0) } ?? "–") }
                                if (hw["oom_kills"].int ?? 0) > 0 { GridRow { Text("Out of memory").foregroundStyle(.secondary); Text("\(hw["oom_kills"].int ?? 0) processes killed since boot").foregroundStyle(.orange) } }
                                ForEach(rows(hw["gpu_live"].array, key: { $0["name"].text })) { g in
                                    GridRow { Text("GPU").foregroundStyle(.secondary); Text("\(g.v["name"].text) · \(Format.bytes((g.v["mem_used"].double ?? 0) * 1_048_576)) of \(Format.bytes((g.v["mem_total"].double ?? 0) * 1_048_576)) video memory · \(g.v["use"].int ?? 0)%") }
                                }
                            }.frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                }
            }.padding(24)
        }
    }
}

// MARK: - health

struct AlertsView: View {
    @EnvironmentObject private var mgmt: ManagementState
    var body: some View {
        Scrolling {
            VStack(alignment: .leading, spacing: 12) {
                SectionHeader(title: "Alerts", subtitle: "Things that need your attention")
                if mgmt.alerts.isEmpty { Label("Nothing needs attention.", systemImage: "checkmark.seal.fill").foregroundStyle(.green) }
                ForEach(rows(mgmt.alerts, key: { $0["title"].text })) { r in AlertRow(alert: r.v).padding(10).frame(maxWidth: .infinity, alignment: .leading).background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 10)) }
            }.padding(24)
        }
    }
}

struct EventsView: View {
    @EnvironmentObject private var mgmt: ManagementState
    @State private var warningsOnly = false
    var body: some View {
        Scrolling {
            VStack(alignment: .leading, spacing: 10) {
                SectionHeader(title: "Events", subtitle: "What Kubernetes has been doing", trailing: AnyView(Toggle("Warnings only", isOn: $warningsOnly)))
                ForEach(rows(mgmt.events.filter { !warningsOnly || $0["type"].text == "Warning" }, key: { $0["object"].text + $0["reason"].text + $0["last"].text })) { EventRow(event: $0.v); Divider() }
            }.padding(24)
        }
    }
}

struct DoctorView: View {
    @EnvironmentObject private var mgmt: ManagementState
    @State private var confirm: Confirm?
    var body: some View {
        Scrolling {
            VStack(alignment: .leading, spacing: 12) {
                SectionHeader(title: "Doctor", subtitle: "Checks for common problems, with fixes", trailing: AnyView(HStack {
                    Button(mgmt.doctorLoading ? "Checking…" : "Check again") { Task { await mgmt.loadDoctor(fresh: true) } }.disabled(mgmt.doctorLoading)
                    Button("Fix everything…") { confirm = Confirm(title: "Fix every problem doctor found?", message: "Doctor runs each check's fix on the server (as root). It changes system settings, such as services and time sync.", button: "Fix them") { Task { await mgmt.run("doctor-fix", title: "Fix every problem doctor found") } } }
                        .disabled(mgmt.busyJob != nil || (mgmt.doctor?["checks"].array.allSatisfy { $0["status"].text == "ok" } ?? true))
                }))
                if let doctor = mgmt.doctor {
                    let issues = doctor["issues"].int ?? 0, warnings = doctor["warnings"].int ?? 0
                    Text("\(issues) problem\(issues == 1 ? "" : "s") · \(warnings) warning\(warnings == 1 ? "" : "s")").foregroundStyle(.secondary)
                    ForEach(rows(doctor["checks"].array, key: { $0["id"].text })) { r in
                        let check = r.v
                        HStack(alignment: .top, spacing: 10) {
                            Image(systemName: check["status"].text == "ok" ? "checkmark.circle.fill" : check["status"].text == "warn" ? "exclamationmark.triangle.fill" : "xmark.octagon.fill")
                                .foregroundStyle(check["status"].text == "ok" ? Color.green : check["status"].text == "warn" ? Color.orange : Color.red)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(check["title"].text).fontWeight(.medium)
                                if !check["detail"].text.isEmpty { Text(check["detail"].text).font(.caption).foregroundStyle(.secondary) }
                            }
                            Spacer()
                            if check["status"].text != "ok", !check["fix"].text.isEmpty {
                                Button("Fix: \(check["fix"].text)") {
                                    confirm = Confirm(title: "Run this fix?", message: "\(check["fix"].text). It runs on the server as root.", button: "Fix") { Task { await mgmt.run("doctor-fix", params: ["only": check["id"].text], title: "Fix: \(check["id"].text)") } }
                                }.disabled(mgmt.busyJob != nil)
                            }
                        }.padding(8)
                        Divider()
                    }
                } else if mgmt.doctorLoading { ProgressView("Running the checks…") }
            }.padding(24)
        }
        .task { if mgmt.doctor == nil { await mgmt.loadDoctor(fresh: false) } }
        .confirm($confirm)
    }
}

struct TasksView: View {
    @EnvironmentObject private var mgmt: ManagementState
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            SectionHeader(title: "Tasks", subtitle: "Commands started from this Mac, with their output")
            if mgmt.jobs.isEmpty { Text("Nothing started from this Mac yet. Tasks started on the website show there.").foregroundStyle(.secondary) }
            List(mgmt.jobs) { job in
                Button { mgmt.shownJobID = job.id } label: {
                    HStack {
                        Text(job.title).fontWeight(.medium)
                        Spacer()
                        StatusPill(text: job.status == "ok" ? "done" : job.status, tone: job.status == "ok" ? .green : job.status == "running" ? .orange : .red)
                        Text(job.started, style: .relative).font(.caption).foregroundStyle(.tertiary)
                    }
                }.buttonStyle(.plain)
            }
        }.padding(24)
    }
}

struct JobSheet: View {
    @EnvironmentObject private var mgmt: ManagementState
    @Environment(\.dismiss) private var dismiss
    let job: TrackedJob
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text(job.title).font(.headline)
                Spacer()
                StatusPill(text: job.status == "ok" ? "done" : job.status == "running" ? "running…" : job.status + (job.rc.map { " (exit \($0))" } ?? ""),
                           tone: job.status == "ok" ? .green : job.status == "running" ? .orange : .red)
            }
            ScrollViewReader { proxy in
                Scrolling {
                    Text(job.lines.isEmpty ? "Starting…" : job.lines.joined(separator: "\n")).font(.system(.caption, design: .monospaced))
                        .textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading).padding(8)
                    Color.clear.frame(height: 1).id("end")
                }.background(.quaternary.opacity(0.4), in: RoundedRectangle(cornerRadius: 8))
                    .onChange(of: job.lines.count) { _ in proxy.scrollTo("end") }
            }
            HStack {
                Text("You can close this window; the task carries on and shows under Tasks.").font(.caption).foregroundStyle(.secondary)
                Spacer()
                if job.status == "running" && job.cancelable { Button("Cancel task") { Task { await mgmt.cancel(job) } } }
                Button("Close") { dismiss() }.keyboardShortcut(.defaultAction)
            }
        }.padding(18)
    }
}

/// One device's Wi-Fi/LAN and Tailscale paths, as the dashboard monitors them, with a test and the manual addresses.
struct ConnectionCard: View {
    @EnvironmentObject private var mgmt: ManagementState
    let record: JSON?
    @State private var lanOverride = ""
    @State private var tailscaleOverride = ""
    @State private var preference = "auto"

    var body: some View {
        Card("Connections") {
            if let r = record {
                VStack(alignment: .leading, spacing: 8) {
                    HStack {
                        StatusPill(text: statusText(r["status"].text), tone: r["status"].text == "both" || r["status"].text == "lan" ? .green : r["status"].text == "unreachable" ? .red : .orange)
                        Text("Using: \(r["preferred"].string.map(pathName) ?? "none")").font(.caption).foregroundStyle(.secondary)
                        Spacer()
                        Button(mgmt.testingConnection == r["id"].text ? "Testing…" : "Test now") { Task { await mgmt.testConnection(r["id"].text) } }.disabled(mgmt.testingConnection != nil)
                    }
                    ForEach(["lan", "tailscale"], id: \.self) { path in
                        let p = r[path]
                        HStack(alignment: .firstTextBaseline) {
                            Text(pathName(path)).frame(width: 80, alignment: .leading).foregroundStyle(.secondary)
                            Text(p["address"].string.flatMap { $0.isEmpty ? nil : $0 } ?? p["candidate"].text.ifEmpty("–")).monospaced().textSelection(.enabled)
                            StatusPill(text: p["state"].text.ifEmpty("not checked"), tone: p["state"].text == "reachable" ? .green : p["state"].text == "unreachable" ? .red : .orange)
                            if let ms = p["ms"].double { Text(String(format: "%.0f ms", ms)).font(.caption2).foregroundStyle(.tertiary) }
                            if !r["settings"][path + "_override"].text.isEmpty { Text("manual").font(.caption2).foregroundStyle(.blue) }
                        }
                        if !p["error"].text.isEmpty && p["state"].text != "reachable" { Text(p["error"].text).font(.caption2).foregroundStyle(.secondary) }
                    }
                    Divider()
                    HStack {
                        TextField("Wi-Fi/LAN address (automatic)", text: $lanOverride).textFieldStyle(.roundedBorder)
                        TextField("Tailscale address (automatic)", text: $tailscaleOverride).textFieldStyle(.roundedBorder)
                        Picker("", selection: $preference) { Text("Automatic").tag("auto"); Text("Prefer Wi-Fi/LAN").tag("lan"); Text("Prefer Tailscale").tag("tailscale") }.frame(width: 150)
                    }
                    HStack {
                        Button("Save and test") { Task { await mgmt.updateConnection(r["id"].text, ["lan_override": lanOverride, "tailscale_override": tailscaleOverride, "preference": preference]) } }
                        Button("Reset to automatic discovery") { Task { await mgmt.updateConnection(r["id"].text, ["reset": true]) } }
                    }
                }
                .onAppear { load(r) }
                .onChange(of: r["id"].text) { _ in load(r) }
            } else {
                Text("No connection record yet: the dashboard checks each machine within a minute of starting (needs a dashboard from October 2026 or later).").font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    private func load(_ r: JSON) {
        lanOverride = r["settings"]["lan_override"].text
        tailscaleOverride = r["settings"]["tailscale_override"].text
        preference = r["settings"]["preference"].string ?? "auto"
    }
    private func pathName(_ p: String) -> String { p == "lan" ? "Wi-Fi/LAN" : p == "tailscale" ? "Tailscale" : p }
    private func statusText(_ s: String) -> String {
        ["both": "both paths", "lan": "Wi-Fi/LAN only", "tailscale": "Tailscale only", "partial": "agent not answering", "unreachable": "unreachable"][s] ?? "unknown"
    }
}
