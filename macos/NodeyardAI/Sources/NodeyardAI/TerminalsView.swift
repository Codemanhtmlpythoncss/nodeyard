import AppKit
import SwiftUI
import WebKit

/// One terminal window: the website's Terminal page for one device (embedded, signed in as this app).
struct TerminalRequest: Codable, Hashable {
    var target: String
    var method: String
    var title: String
}

/// Manage › Terminals: every cluster node and Tailscale device, as the server sees them. Node shells and checks open
/// the website's terminal in a window here (it asks for the dashboard password once more, like the website);
/// "SSH from this Mac" opens Terminal with `ssh` straight to the device's address (this Mac must be on the tailnet).
struct TerminalsView: View {
    @EnvironmentObject private var mgmt: ManagementState
    @Environment(\.openWindow) private var openWindow
    @AppStorage("terminalLogins") private var loginsData = Data()

    private var targets: [JSON] { mgmt.termTargets?["targets"].array ?? [] }

    var body: some View {
        Scrolling {
            VStack(alignment: .leading, spacing: 14) {
                SectionHeader(title: "Terminals", subtitle: "A shell on every cluster node and Tailscale device",
                              trailing: AnyView(Button("Refresh") { Task { await mgmt.refreshTerminals() } }))
                Text("Shell, Root shell and Checks open the server's terminal in a window here; they go through Kubernetes, so they work without SSH keys while the node's kubelet runs. Checks runs read-only checks for a node that keeps dropping out, then leaves you in a root shell. SSH from this Mac opens Terminal with ssh to the device's address: use it when a node isn't Ready.")
                    .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                if let error = mgmt.termError {
                    Label(error, systemImage: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                }
                group("This server", targets.filter { $0["kind"].text == "server" }, empty: "")
                group("Cluster nodes", targets.filter { $0["kind"].text == "node" }, empty: "No nodes yet.")
                group("Tailscale devices", targets.filter { $0["kind"].text == "tailnet" },
                      empty: mgmt.termTargets?["tailnet_error"].text.ifEmpty("No other devices on your tailnet.") ?? "Loading…")
            }.padding(24)
        }
        .task { await mgmt.refreshTerminals() }
    }

    @ViewBuilder private func group(_ title: String, _ items: [JSON], empty: String) -> some View {
        if !(items.isEmpty && empty.isEmpty) {
            Card(title) {
                if items.isEmpty { Text(empty).font(.callout).foregroundStyle(.secondary) }
                ForEach(items.indices, id: \.self) { i in
                    if i > 0 { Divider() }
                    DeviceTerminalRow(device: items[i], defaultLogin: mgmt.termTargets?["user"].text ?? "", login: loginBinding(items[i]["id"].text)) { method in
                        openWindow(value: TerminalRequest(target: items[i]["id"].text, method: method,
                                                          title: "\(items[i]["name"].text) · \(DeviceTerminalRow.label(method))"))
                    }
                }
            }
        }
    }

    private func loginBinding(_ id: String) -> Binding<String> {
        Binding(get: { logins[id] ?? "" }, set: { var all = logins; all[id] = $0; loginsData = (try? JSONEncoder().encode(all)) ?? Data() })
    }
    private var logins: [String: String] { (try? JSONDecoder().decode([String: String].self, from: loginsData)) ?? [:] }
}

private struct DeviceTerminalRow: View {
    let device: JSON
    let defaultLogin: String
    @Binding var login: String
    let open: (String) -> Void
    @State private var sshError: String?

    static func label(_ method: String) -> String {
        ["local": "Shell", "kube": "Shell", "root": "Root shell", "diagnose": "Checks", "ssh": "SSH"][method] ?? method
    }

    private var methods: [String] { device["methods"].array.map(\.text) }
    private var isPhone: Bool { ["ios", "android", "tvos"].contains(device["os"].text.lowercased()) }
    private var webMethods: [String] { methods.filter { $0 != "ssh" } }
    /// An address this Mac can reach over Tailscale (100.64.0.0/10), for SSH from here.
    private var tailnetAddress: String? {
        device["addresses"].array.map(\.text).first(where: isTailscaleIPv4) ?? (isTailscaleIPv4(device["address"].text) ? device["address"].text : nil)
    }

    var body: some View {
        HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(device["name"].text).fontWeight(.semibold)
                    status
                    if device["this_server"].bool == true { StatusPill(text: "this server") }
                }
                Text(detail).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                if let sshError { Text(sshError).font(.caption).foregroundStyle(.orange) }
            }
            Spacer(minLength: 8)
            if webMethods.isEmpty && (tailnetAddress == nil || isPhone) {
                Text(device["note"].text.ifEmpty("Can't be opened from here.")).font(.caption).foregroundStyle(.secondary)
            }
            ForEach(webMethods, id: \.self) { m in
                Button(Self.label(m)) { open(m) }.help(help(m))
            }
            if let address = tailnetAddress, !isPhone {
                TextField(defaultLogin.ifEmpty("user"), text: $login).frame(width: 110).textFieldStyle(.roundedBorder)
                    .help("The account to sign in as on \(device["name"].text)")
                Button("SSH from this Mac") { ssh(address) }.help("Opens Terminal with ssh to \(address)")
            }
        }.padding(.vertical, 4)
    }

    @ViewBuilder private var status: some View {
        switch device["kind"].text {
        case "server": StatusPill(text: "this server", tone: .green)
        case "node": device["ready"].bool == true ? StatusPill(text: "Ready", tone: .green) : StatusPill(text: device["status"].text.ifEmpty("NotReady"), tone: .red)
        default: device["online"].bool == true ? StatusPill(text: "online", tone: .green) : StatusPill(text: "offline")
        }
    }

    private var detail: String {
        var parts: [String] = []
        if !device["address"].text.isEmpty { parts.append(device["address"].text) }
        if device["kind"].text == "node", !device["tailnet"].text.isEmpty, device["tailnet"].text != device["name"].text { parts.append("tailnet: \(device["tailnet"].text)") }
        if device["kind"].text == "tailnet", !device["os"].text.isEmpty { parts.append(device["os"].text) }
        if !device["reason"].text.isEmpty { parts.append(device["reason"].text) }
        return parts.joined(separator: " · ")
    }

    private func help(_ m: String) -> String {
        switch m {
        case "kube", "local": "A shell as your user"
        case "root": "A root shell through a short-lived Kubernetes helper pod"
        case "diagnose": "Read-only checks for a node that drops out, then a root shell"
        default: ""
        }
    }

    private func ssh(_ address: String) {
        let user = login.trimmingCharacters(in: .whitespaces).ifEmpty(defaultLogin)
        guard user.range(of: #"^[A-Za-z_][A-Za-z0-9_.-]{0,31}$"#, options: .regularExpression) != nil else { sshError = "That isn't a valid user name."; return }
        guard let url = URL(string: "ssh://\(user)@\(address)") else { sshError = "Couldn't build the SSH address."; return }
        sshError = NSWorkspace.shared.open(url) ? nil : "macOS has no app for ssh:// links (Terminal normally handles them)."
    }
}

func isTailscaleIPv4(_ address: String) -> Bool {
    let parts = address.split(separator: ".").compactMap { Int($0) }
    guard parts.count == 4, parts.allSatisfy({ (0...255).contains($0) }) else { return false }
    return parts[0] == 100 && (64...127).contains(parts[1])
}

/// The website's terminal for one device, in its own window.
struct TerminalWindowView: View {
    let request: TerminalRequest?
    @EnvironmentObject private var mgmt: ManagementState
    @State private var page: (URL, [HTTPCookie])?

    var body: some View {
        Group {
            if let request, let page {
                TerminalWebView(url: page.0, cookies: page.1).navigationTitle(request.title)
            } else if request == nil {
                Text("Open a terminal from Manage › Terminals.").foregroundStyle(.secondary).frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                VStack(spacing: 10) {
                    ProgressView()
                    Text("Signing in to the dashboard…").foregroundStyle(.secondary)
                }.frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .task(id: request) {
            guard let request else { return }
            await mgmt.refreshTerminals()          // signs in again (saved password) if the session expired
            page = mgmt.terminalPage(target: request.target, method: request.method)
        }
    }
}

struct TerminalWebView: NSViewRepresentable {
    let url: URL
    let cookies: [HTTPCookie]

    func makeNSView(context: Context) -> WKWebView { Self.make(url: url, cookies: cookies) }

    /// A web view with its own, memory-only cookie store holding just this app's dashboard session.
    static func make(url: URL, cookies: [HTTPCookie]) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .nonPersistent()
        let view = WKWebView(frame: .zero, configuration: config)
        view.setValue(false, forKey: "drawsBackground")
        let store = config.websiteDataStore.httpCookieStore
        Task { @MainActor in
            for cookie in cookies { await store.setCookie(cookie) }
            view.load(URLRequest(url: url))
        }
        return view
    }

    func updateNSView(_ nsView: WKWebView, context: Context) {}
}

/// Manage › AI galaxy: the website's live picture of the cluster and its model.
struct GalaxyView: View {
    @EnvironmentObject private var mgmt: ManagementState
    @State private var page: (URL, [HTTPCookie])?
    var body: some View {
        Group {
            if let page { TerminalWebView(url: page.0, cookies: page.1) }
            else { ProgressView("Signing in to the dashboard…").frame(maxWidth: .infinity, maxHeight: .infinity) }
        }
        .task {
            await mgmt.refreshTerminals()     // (signs in again with the saved password if the session expired)
            page = mgmt.websitePage("galaxy")
        }
    }
}
