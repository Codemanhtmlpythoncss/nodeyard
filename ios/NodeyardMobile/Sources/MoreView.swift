import SwiftUI

/// Everything else the dashboard can do, as native screens.
struct MoreView: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var cluster: ClusterStore
    @StateObject private var actions = Actions()

    var body: some View {
        NavigationStack {
            List {
                Section {
                    link("Dashboard & cluster", "Restart, update, doctor, sign-in", "switch.2", ServerScreen())
                    link("Terminal", "A shell on any machine", "terminal", TerminalsList())
                    link("Commands", "Run any nodeyard command", "chevron.left.forwardslash.chevron.right", CommandsScreen())
                }
                .rowStyle()
                Section("Machines") {
                    link("Updates", "OS packages on every node", "arrow.down.circle", UpdatesScreen())
                    link("Hardware", "Model, temperatures, fans", "cpu", HardwareScreen())
                    link("Memory", "What's using RAM, free it", "memorychip", MemoryScreen())
                    link("Storage", "Disk use and what fills it", "internaldrive", StorageScreen())
                    link("Connections", "LAN and Tailscale routes", "point.3.connected.trianglepath.dotted", ConnectionsScreen())
                }
                .rowStyle()
                Section("Apps & AI") {
                    link("AI models", "Load and unload models", "sparkles", ModelsScreen())
                    link("Cleanup", "Remove stopped pods", "trash", CleanupScreen())
                    link("Homelab", "Uptime checks, Wake-on-LAN", "house", HomelabScreen())
                    link("Plugins", "Turn dashboard features on or off", "puzzlepiece.extension", PluginsScreen())
                }
                .rowStyle()
                Section("History") {
                    link("Activity", "Events and the audit log", "clock.arrow.circlepath", ActivityScreen())
                }
                .rowStyle()
            }
            .listStyle(.insetGrouped)
            .screen()
            .navigationTitle("More")
        }
        .environmentObject(actions)
        .actionHost(actions)
        .onAppear { actions.api = model.api }
    }

    private func link<D: View>(_ title: String, _ detail: String, _ symbol: String, _ dest: D) -> some View {
        NavigationLink {
            dest.environmentObject(actions)
        } label: {
            HStack(spacing: 14) {
                Image(systemName: symbol)
                    .font(.system(size: 16, weight: .semibold))
                    .foregroundStyle(.white)
                    .frame(width: 34, height: 34)
                    .background(Theme.brand, in: RoundedRectangle(cornerRadius: 9, style: .continuous))
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).font(.system(size: 16, weight: .semibold, design: .rounded))
                    Text(detail).font(.system(size: 12, design: .rounded)).foregroundStyle(Theme.mist)
                }
            }
            .padding(.vertical, 2)
        }
    }
}

// MARK: - Dashboard & cluster

struct ServerScreen: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var actions: Actions
    @StateObject private var settings = Loader<SettingsInfo>("/api/settings")
    @StateObject private var version = Loader<VersionInfo>("/api/dashboard/version")
    @StateObject private var doctor = Loader<DoctorReply>("/api/doctor")
    @State private var newPassword = ""
    @State private var again = ""

    struct SettingsInfo: Decodable {
        var listen: String?
        var port: Int?
        var auth: Bool?
        var sessions: Int?
        var demo: Bool?
        var hf_token: Bool?
        var cluster_name: String?
        var nodes: [NodeRow]?
        struct NodeRow: Decodable { var name: String }
    }
    struct VersionInfo: Decodable { var build: String?; var update_pending: Bool?; var busy: String?; var can_restart: Bool? }
    struct DoctorReply: Decodable { var doctor: Doctor? }
    struct Doctor: Decodable { var checks: [Check]?; var issues: Int?; var warnings: Int? }
    struct Check: Decodable, Identifiable { var id: String; var category: String?; var title: String?; var status: String?; var detail: String?; var fix: String? }

    var body: some View {
        ManageList(title: "Dashboard & cluster", problem: settings.problem, loading: settings.value == nil && settings.loading, refresh: load) {
            Section("Dashboard") {
                Fact("Address", model.server?.absoluteString)
                Fact("Build", version.value?.build)
                Fact("Listens on", settings.value.map { "\($0.listen ?? "?") · port \($0.port.map(String.init) ?? "?")" })
                Fact("Signed-in sessions", settings.value?.sessions.map(String.init))
                if version.value?.update_pending == true { Label("A new version is waiting for a restart", systemImage: "arrow.triangle.2.circlepath").foregroundStyle(.orange) }
                Button { actions.quick("/api/refresh-all", ok: "Refreshing everything") } label: {
                    Label("Refresh all data", systemImage: "arrow.clockwise")
                }
                Button {
                    actions.ask("Update nodeyard", ["Pulls the newest nodeyard and its helpers (yt-dlp, ffmpeg, sc-pick…).",
                                                     "The dashboard restarts when it's done, so the app reconnects."],
                                path: "/api/update/run", body: [:], button: "Update")
                } label: { Label("Update nodeyard", systemImage: "square.and.arrow.down") }
                Button(role: .destructive) {
                    actions.ask("Restart the dashboard", ["The dashboard stops and starts again. The app reconnects by itself."],
                                path: "/api/dashboard/restart", body: [:], button: "Restart", destructive: true)
                } label: { Label("Restart the dashboard", systemImage: "power") }
            }
            .rowStyle()

            Section("Cluster") {
                Button { actions.job("Reconnect missing nodes", ["Finds nodes that dropped off and links them back, without a reboot."], ["action": "relink"]) } label: {
                    Label("Reconnect missing nodes", systemImage: "link")
                }
                Button(role: .destructive) {
                    actions.job("Restart Kubernetes", ["Restarts k3s on every node. Apps blink off for a minute."], ["action": "restart-cluster"], destructive: true)
                } label: { Label("Restart Kubernetes everywhere", systemImage: "arrow.clockwise.circle") }
                Button(role: .destructive) {
                    actions.job("Reboot every machine", ["Every machine in the cluster reboots. Everything is offline for a few minutes."], ["action": "reboot-cluster"], destructive: true)
                } label: { Label("Reboot every machine", systemImage: "restart.circle") }
                if let nodes = settings.value?.nodes, !nodes.isEmpty {
                    Menu {
                        ForEach(nodes, id: \.name) { n in
                            Button(n.name, role: .destructive) {
                                actions.job("Remove \(n.name)", ["\(n.name) leaves the cluster. Its apps move to other nodes.", "You'd have to join it again to get it back."],
                                            ["action": "remove-node", "node": n.name], destructive: true)
                            }
                        }
                    } label: { Label("Remove a node…", systemImage: "minus.circle").foregroundStyle(Theme.danger) }
                }
            }
            .rowStyle()

            Section {
                ForEach(doctor.value?.doctor?.checks ?? []) { c in
                    HStack(alignment: .top, spacing: 10) {
                        Image(systemName: c.status == "ok" ? "checkmark.circle.fill" : c.status == "warn" ? "exclamationmark.triangle.fill" : "xmark.octagon.fill")
                            .foregroundStyle(c.status == "ok" ? Theme.teal : c.status == "warn" ? .orange : Theme.danger)
                        VStack(alignment: .leading, spacing: 3) {
                            Text(c.title ?? c.id).font(.system(size: 14, weight: .medium, design: .rounded))
                            if let d = c.detail { Text(d).font(.system(size: 12)).foregroundStyle(Theme.mist) }
                        }
                        Spacer()
                        if c.status != "ok", let fix = c.fix {
                            Button("Fix") {
                                actions.job("Fix: \(c.title ?? c.id)", [String(fix.prefix(1)).uppercased() + String(fix.dropFirst()) + "."], ["action": "doctor-fix", "only": c.id]) {
                                    Task { await doctor.load(model.api) }
                                }
                            }
                            .buttonStyle(.bordered).controlSize(.small)
                        }
                    }
                }
                if (doctor.value?.doctor?.issues ?? 0) + (doctor.value?.doctor?.warnings ?? 0) > 0 {
                    Button { actions.job("Fix every problem", ["Runs nodeyard doctor --fix for every check that failed."], ["action": "doctor-fix"]) { Task { await doctor.load(model.api) } } } label: {
                        Label("Fix everything", systemImage: "wrench.and.screwdriver")
                    }
                }
            } header: {
                Text("Doctor")
            } footer: {
                if doctor.loading { Text("Checking…") }
            }
            .rowStyle()

            if settings.value?.auth == true {
                Section {
                    SecureField("New password", text: $newPassword).textContentType(.newPassword)
                    SecureField("Again", text: $again).textContentType(.newPassword)
                    Button("Change password") {
                        let pw = newPassword
                        actions.quick("/api/settings/password", ["password": pw, "again": again], ok: "Password changed") {
                            newPassword = ""; again = ""
                            model.api?.rememberPassword(pw)
                        }
                    }
                    .disabled(newPassword.isEmpty || newPassword != again)
                    Button(role: .destructive) {
                        actions.ask("Sign out everywhere", ["Every browser and app signed in to this dashboard has to sign in again, this phone too."],
                                    path: "/api/settings/signout-all", body: [:], button: "Sign out everywhere", destructive: true)
                    } label: { Label("Sign out everywhere", systemImage: "person.crop.circle.badge.xmark") }
                } header: { Text("Sign-in") } footer: { Text("This phone keeps the new password in its Keychain.") }
                .rowStyle()
            }
        }
        .task { if settings.value == nil { await load() } }
    }

    private func load() async {
        async let a: Void = settings.load(model.api)
        async let b: Void = version.load(model.api)
        async let c: Void = doctor.load(model.api)
        _ = await (a, b, c)
    }
}

// MARK: - Commands

struct CommandsScreen: View {
    @EnvironmentObject private var model: AppModel
    @StateObject private var data = Loader<Reply>("/api/commands")
    @State private var search = ""

    struct Reply: Decodable { var commands: [Command]? }
    struct Command: Decodable, Identifiable, Hashable { var path: String; var group: String?; var summary: String?; var help: String?; var id: String { path } }

    private var groups: [(String, [Command])] {
        let all = (data.value?.commands ?? []).filter { $0.path != "menu" }
            .filter { search.isEmpty || $0.path.localizedCaseInsensitiveContains(search) || ($0.summary ?? "").localizedCaseInsensitiveContains(search) }
        var order: [String] = []
        var map: [String: [Command]] = [:]
        for c in all {
            let g = c.group ?? "Other"
            if map[g] == nil { order.append(g) }
            map[g, default: []].append(c)
        }
        return order.map { ($0, map[$0] ?? []) }
    }

    var body: some View {
        ManageList(title: "Commands", problem: data.problem, loading: data.value == nil && data.loading, refresh: { await data.load(model.api) }) {
            ForEach(groups, id: \.0) { g in
                Section(g.0) {
                    ForEach(g.1) { c in
                        NavigationLink { CommandScreen(command: c) } label: {
                            VStack(alignment: .leading, spacing: 3) {
                                Text("nodeyard \(c.path)").font(.system(size: 14, weight: .semibold, design: .monospaced))
                                if let s = c.summary { Text(s).font(.system(size: 12, design: .rounded)).foregroundStyle(Theme.mist) }
                            }
                        }
                    }
                }
                .rowStyle()
            }
        }
        .searchable(text: $search, prompt: "Find a command")
        .task { if data.value == nil { await data.load(model.api) } }
    }
}

struct CommandScreen: View {
    let command: CommandsScreen.Command
    @EnvironmentObject private var actions: Actions
    @State private var args = ""
    @State private var dryRun = false
    @State private var yes = true

    var body: some View {
        List {
            Section {
                TextField("Options (e.g. --node yard-2)", text: $args)
                    .font(.system(size: 14, design: .monospaced))
                    .textInputAutocapitalization(.never).autocorrectionDisabled()
                Toggle("Answer yes to questions", isOn: $yes).tint(Theme.indigo)
                Toggle("Dry run (only show what it would do)", isOn: $dryRun).tint(Theme.indigo)
                Button {
                    actions.job("nodeyard \(command.path)", ["nodeyard \(([command.path, args] + (yes ? ["--yes"] : []) + (dryRun ? ["--dry-run"] : [])).filter { !$0.isEmpty }.joined(separator: " "))"],
                                ["action": "cli", "path": command.path, "args": args, "yes": yes, "dry_run": dryRun])
                } label: { Label("Run", systemImage: "play.fill") }
            } footer: { Text("Commands that ask questions as they go belong in the Terminal tab.") }
            .rowStyle()
            if let help = command.help {
                Section("Help") {
                    Text(help).font(.system(size: 12, design: .monospaced)).foregroundStyle(Theme.mist).textSelection(.enabled)
                }
                .rowStyle()
            }
        }
        .listStyle(.insetGrouped)
        .screen()
        .navigationTitle(command.path)
        .navigationBarTitleDisplayMode(.inline)
    }
}
