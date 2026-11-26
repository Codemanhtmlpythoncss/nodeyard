import SwiftUI

/// First launch: say where the dashboard is, check it answers, then open it.
struct OnboardingView: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var web: WebController
    @State private var address = ""
    @State private var checking = false
    @State private var problem: String?
    @FocusState private var focused: Bool

    var body: some View {
        ScrollView {
            VStack(spacing: 0) {
                Spacer(minLength: 72)
                NodeTile(size: 112)
                Text("Nodeyard")
                    .font(Theme.title(38))
                    .padding(.top, 28)
                Text("Your cluster, music and AI\nin your pocket.")
                    .font(.system(size: 17, design: .rounded))
                    .foregroundStyle(Theme.mist)
                    .multilineTextAlignment(.center)
                    .padding(.top, 8)

                VStack(alignment: .leading, spacing: 10) {
                    Text("Dashboard address")
                        .font(.system(size: 14, weight: .semibold, design: .rounded))
                        .foregroundStyle(Theme.mist)
                    HStack(spacing: 12) {
                        Image(systemName: "link")
                            .foregroundStyle(Theme.mist)
                        TextField("", text: $address, prompt: Text("my-mac.tailnet.ts.net").foregroundColor(Theme.mist.opacity(0.5)))
                            .keyboardType(.URL)
                            .textContentType(.URL)
                            .textInputAutocapitalization(.never)
                            .autocorrectionDisabled()
                            .submitLabel(.go)
                            .focused($focused)
                            .onSubmit(connect)
                    }
                    .padding(.horizontal, 16)
                    .frame(height: 54)
                    .background(Theme.panel, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
                    .overlay(
                        RoundedRectangle(cornerRadius: 16, style: .continuous)
                            .stroke(problem == nil ? (focused ? Theme.indigo : Theme.line) : Theme.danger, lineWidth: 1.5)
                    )
                    if let problem {
                        Label(problem, systemImage: "exclamationmark.triangle.fill")
                            .font(.system(size: 14, design: .rounded))
                            .foregroundStyle(Theme.danger)
                            .transition(.opacity.combined(with: .move(edge: .top)))
                    } else {
                        Text("The address you open the dashboard at on your computer. Your phone needs Tailscale on to reach a tailnet name.")
                            .font(.system(size: 13, design: .rounded))
                            .foregroundStyle(Theme.mist.opacity(0.8))
                    }
                }
                .padding(.top, 44)

                Button(checking ? "Connecting" : "Connect", action: connect)
                    .buttonStyle(BrandButtonStyle(busy: checking))
                    .disabled(checking || address.trimmingCharacters(in: .whitespaces).isEmpty)
                    .padding(.top, 28)
            }
            .padding(.horizontal, 24)
            .frame(maxWidth: 480)
            .frame(maxWidth: .infinity)
        }
        .scrollDismissesKeyboard(.interactively)
        .animation(.easeInOut(duration: 0.2), value: problem)
    }

    private func connect() {
        guard !checking else { return }
        guard let url = AppModel.normalize(address) else {
            problem = "That doesn't look like a web address."
            Haptics.error()
            return
        }
        focused = false
        checking = true
        problem = nil
        Task {
            var result = await AppModel.check(url)
            var final = url
            // Typed a bare name and https didn't answer? Many dashboards run on plain http.
            if case .unreachable = result, !address.contains("://"),
               var parts = URLComponents(url: url, resolvingAgainstBaseURL: false) {
                parts.scheme = "http"
                if let http = parts.url, case .ok = await AppModel.check(http) { result = .ok; final = http }
            }
            checking = false
            switch result {
            case .ok:
                Haptics.success()
                web.start(server: final, route: model.lastPage)
                model.setServer(final)
            case .notNodeyard:
                problem = "Something answered, but it isn't a nodeyard dashboard."
                Haptics.error()
            case .unreachable(let why):
                problem = why
                Haptics.error()
            }
        }
    }
}

/// Covers the app until Face ID (or the passcode) unlocks it.
struct LockView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        ZStack {
            Theme.ink.ignoresSafeArea()
            VStack(spacing: 0) {
                Spacer()
                NodeTile(size: 96)
                Text("Nodeyard is locked")
                    .font(Theme.title(24))
                    .padding(.top, 28)
                Spacer()
                Button {
                    Task { await model.unlock() }
                } label: {
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
