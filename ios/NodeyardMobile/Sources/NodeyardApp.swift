import AVFoundation
import SwiftUI

@main
struct NodeyardApp: App {
    @StateObject private var model = AppModel()

    init() {
        // music keeps playing with the screen off and shows on the lock screen
        try? AVAudioSession.sharedInstance().setCategory(.playback, mode: .default, policy: .longFormAudio)
        let bar = UITabBarAppearance()
        bar.configureWithDefaultBackground()
        bar.backgroundColor = Theme.uiInk.withAlphaComponent(0.85)
        UITabBar.appearance().standardAppearance = bar
        UITabBar.appearance().scrollEdgeAppearance = bar
        UISegmentedControl.appearance().selectedSegmentTintColor = UIColor(Theme.indigo)
        UISegmentedControl.appearance().setTitleTextAttributes([.foregroundColor: UIColor.white], for: .selected)
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(model)
                .environmentObject(model.cluster)
                .environmentObject(model.player)
                .preferredColorScheme(.dark)
                .tint(Theme.indigo)
        }
    }
}

/// Sign-in until there's a dashboard, then the tabs. The lock and the privacy cover sit on top.
struct RootView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.scenePhase) private var phase
    @State private var covered = false
    @State private var askOnActive = true     // prompt once at launch and after each return from the background

    var body: some View {
        ZStack {
            Theme.ink.ignoresSafeArea()
            if model.api != nil {
                MainTabs().transition(.opacity)
            } else {
                SignInView().transition(.opacity)
            }
            if covered && !model.locked {
                PrivacyCover().transition(.opacity)
            }
            if model.locked {
                LockView().transition(.opacity.combined(with: .scale(scale: 1.04))).zIndex(2)
            }
        }
        .animation(.easeInOut(duration: 0.25), value: model.api == nil)
        .animation(.easeInOut(duration: 0.2), value: covered)
        .onAppear {
            if phase == .active && model.locked && askOnActive { askOnActive = false; Task { await model.unlock() } }
        }
        .onChange(of: phase) { newPhase in
            switch newPhase {
            case .background:
                model.appWentToBackground()
                model.cluster.pause()
                askOnActive = true
            case .inactive:
                covered = model.faceIDEnabled     // hide the app in the app switcher
            case .active:
                covered = false
                model.cluster.resume()
                // only once: a cancelled Face ID sheet also comes back through .active, and must not ask again
                if model.locked && askOnActive { askOnActive = false; Task { await model.unlock() } }
            @unknown default: break
            }
        }
    }
}

struct MainTabs: View {
    @EnvironmentObject private var player: MusicPlayer
    @SceneStorage("tab") private var tab = "overview"

    var body: some View {
        TabView(selection: $tab) {
            OverviewView().withMiniPlayer()
                .tabItem { Label("Overview", systemImage: "square.grid.2x2") }.tag("overview")
            ClusterView().withMiniPlayer()
                .tabItem { Label("Cluster", systemImage: "server.rack") }.tag("cluster")
            ChatView()
                .tabItem { Label("AI", systemImage: "sparkles") }.tag("ai")
            MusicView().withMiniPlayer()
                .tabItem { Label("Music", systemImage: "music.note") }.tag("music")
            TerminalsView().withMiniPlayer()
                .tabItem { Label("Terminal", systemImage: "terminal") }.tag("terminal")
        }
        .animation(.spring(response: 0.4, dampingFraction: 0.85), value: player.current?.id)
    }
}

private extension View {
    func withMiniPlayer() -> some View { safeAreaInset(edge: .bottom, spacing: 0) { MiniPlayer() } }
}

// MARK: - Sign in

struct SignInView: View {
    @EnvironmentObject private var model: AppModel
    @AppStorage("nodeyard.server") private var savedAddress = ""
    @State private var address = ""
    @State private var password = ""
    @State private var found: NodeyardAPI?       // the dashboard answered and wants a password
    @State private var busy = false
    @State private var problem: String?
    @FocusState private var addressFocus: Bool
    @FocusState private var passwordFocus: Bool

    var body: some View {
        ScrollView {
            VStack(spacing: 0) {
                Spacer(minLength: 72)
                NodeTile(size: 112)
                Text("Nodeyard").font(Theme.title(38)).padding(.top, 28)
                Text("Your cluster, music and AI\nin your pocket.")
                    .font(.system(size: 17, design: .rounded))
                    .foregroundStyle(Theme.mist)
                    .multilineTextAlignment(.center)
                    .padding(.top, 8)

                VStack(alignment: .leading, spacing: 10) {
                    if let why = model.signedOutReason, problem == nil {
                        ErrorBanner(text: why).padding(.bottom, 6)
                    }
                    Text("Dashboard address")
                        .font(.system(size: 14, weight: .semibold, design: .rounded)).foregroundStyle(Theme.mist)
                    InputField(symbol: "link", placeholder: "my-mac.tailnet.ts.net", text: $address,
                               failed: problem != nil && found == nil, focused: $addressFocus, submit: go)
                        .onChange(of: address) { _ in found = nil }
                    if found != nil {
                        Text("Dashboard password")
                            .font(.system(size: 14, weight: .semibold, design: .rounded)).foregroundStyle(Theme.mist)
                            .padding(.top, 10)
                        InputField(symbol: "key.fill", placeholder: "Password", text: $password, secure: true,
                                   failed: problem != nil, focused: $passwordFocus, submit: go)
                            .transition(.opacity.combined(with: .move(edge: .top)))
                    }
                    if let problem {
                        Label(problem, systemImage: "exclamationmark.triangle.fill")
                            .font(.system(size: 14, design: .rounded)).foregroundStyle(Theme.danger)
                            .transition(.opacity)
                    } else if found == nil {
                        Text("The address you open the dashboard at on your computer. Turn on Tailscale to reach a tailnet name.")
                            .font(.system(size: 13, design: .rounded)).foregroundStyle(Theme.mist.opacity(0.8))
                    } else {
                        Text("Kept in this phone's Keychain, so you only type it once.")
                            .font(.system(size: 13, design: .rounded)).foregroundStyle(Theme.mist.opacity(0.8))
                    }
                }
                .padding(.top, 44)

                Button(found == nil ? (busy ? "Connecting" : "Connect") : (busy ? "Signing in" : "Sign in"), action: go)
                    .buttonStyle(BrandButtonStyle(busy: busy))
                    .disabled(busy || address.trimmingCharacters(in: .whitespaces).isEmpty || (found != nil && password.isEmpty))
                    .padding(.top, 28)
            }
            .padding(.horizontal, 24)
            .frame(maxWidth: 480)
            .frame(maxWidth: .infinity)
        }
        .scrollDismissesKeyboard(.interactively)
        .animation(.easeInOut(duration: 0.25), value: found == nil)
        .animation(.easeInOut(duration: 0.2), value: problem)
        .onAppear { if address.isEmpty { address = savedAddress } }
    }

    private func go() {
        guard !busy else { return }
        if let api = found { signIn(api); return }
        guard let url = AppModel.normalize(address) else {
            problem = "That doesn't look like a web address."
            Haptics.error()
            return
        }
        busy = true
        problem = nil
        addressFocus = false
        Task {
            defer { busy = false }
            var candidates = [url]
            // a bare name that doesn't answer on https often runs on plain http
            if !address.contains("://"), var parts = URLComponents(url: url, resolvingAgainstBaseURL: false) {
                parts.scheme = "http"
                if let http = parts.url { candidates.append(http) }
            }
            var lastError: Error?
            for base in candidates {
                let api = NodeyardAPI(base: base)
                do {
                    let auth = try await api.authState()
                    if !auth.enabled || auth.authenticated {
                        Haptics.success()
                        model.use(api)
                    } else {
                        found = api
                        passwordFocus = true
                    }
                    return
                } catch {
                    lastError = error
                }
            }
            if let e = lastError as? APIError, e.status != 0 {
                problem = "Something answered, but it isn't a nodeyard dashboard."
            } else {
                problem = lastError?.localizedDescription ?? "Nothing answered."
            }
            Haptics.error()
        }
    }

    private func signIn(_ api: NodeyardAPI) {
        busy = true
        problem = nil
        Task {
            defer { busy = false }
            do {
                try await api.signIn(password: password)
                Haptics.success()
                password = ""
                model.use(api)
            } catch {
                problem = (error as? APIError)?.status == 401 ? "That isn't the right password." : error.localizedDescription
                Haptics.error()
            }
        }
    }
}

// MARK: - Lock

/// Covers the app until Face ID (or the passcode) unlocks it.
struct LockView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        ZStack {
            Theme.ink.ignoresSafeArea()
            VStack(spacing: 0) {
                Spacer()
                NodeTile(size: 96)
                Text("Nodeyard is locked").font(Theme.title(24)).padding(.top, 28)
                Spacer()
                Button { Task { await model.unlock() } } label: {
                    Label("Unlock with \(model.biometryName)", systemImage: model.biometrySymbol)
                }
                .buttonStyle(BrandButtonStyle())
                .padding(.horizontal, 24)
                .padding(.bottom, 24)
                .frame(maxWidth: 480)
            }
        }
    }
}

/// Shown over the app in the app switcher when the lock is on.
private struct PrivacyCover: View {
    var body: some View {
        ZStack {
            Theme.ink.ignoresSafeArea()
            NodeMark(size: 84, animated: false)
        }
    }
}

// MARK: - Settings

struct SettingsView: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var cluster: ClusterStore
    @Environment(\.dismiss) private var dismiss
    @State private var confirmSignOut = false

    var body: some View {
        NavigationStack {
            List {
                Section("Dashboard") {
                    Fact("Address", model.server?.absoluteString)
                    Fact("Cluster", cluster.state?.cluster?.name)
                    Fact("k3s", cluster.state?.cluster?.k3s_version)
                    Fact("Server build", cluster.snapshot?.build)
                    if let server = model.server {
                        Link(destination: server) { Label("Open the web dashboard", systemImage: "safari") }
                    }
                }
                .rowStyle()
                Section {
                    if model.canUseLock {
                        Toggle(isOn: Binding(get: { model.faceIDEnabled }, set: { on in Task { await model.setFaceID(on) } })) {
                            Label("Lock with \(model.biometryName)", systemImage: model.biometrySymbol)
                        }
                        .tint(Theme.indigo)
                    }
                } footer: {
                    Text("Asks when you open the app and hides it in the app switcher.")
                }
                .rowStyle()
                Section {
                    Button(role: .destructive) { confirmSignOut = true } label: {
                        Label("Sign out", systemImage: "rectangle.portrait.and.arrow.right")
                    }
                }
                .rowStyle()
                Section {
                    Fact("App version", Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String)
                }
                .rowStyle()
            }
            .listStyle(.insetGrouped)
            .screen()
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { Button("Done") { dismiss() } }
            .confirmationDialog("Sign out of this dashboard?", isPresented: $confirmSignOut, titleVisibility: .visible) {
                Button("Sign out", role: .destructive) {
                    dismiss()
                    Task { await model.signOut() }
                }
            } message: {
                Text("The saved password is removed from this phone.")
            }
        }
        .preferredColorScheme(.dark)
    }
}
