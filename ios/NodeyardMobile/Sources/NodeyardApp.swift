import AVFoundation
import SwiftUI

@main
struct NodeyardApp: App {
    @StateObject private var model = AppModel()
    @StateObject private var web = WebController()

    init() {
        // Music from the dashboard keeps playing with the screen off and shows on the lock screen.
        try? AVAudioSession.sharedInstance().setCategory(.playback, mode: .default, policy: .longFormAudio)
        try? AVAudioSession.sharedInstance().setActive(true)
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(model)
                .environmentObject(web)
                .preferredColorScheme(.dark)
                .tint(Theme.indigo)
        }
    }
}

/// Onboarding until there's a dashboard address, then the dashboard. The lock and the privacy cover sit on top.
struct RootView: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var web: WebController
    @Environment(\.scenePhase) private var phase
    @State private var covered = false
    @State private var askOnActive = true     // prompt once at launch and after each return from the background

    var body: some View {
        ZStack {
            Theme.ink.ignoresSafeArea()
            if model.server != nil {
                MainView().transition(.opacity)
            } else {
                OnboardingView().transition(.opacity)
            }
            if covered && !model.locked {
                PrivacyCover().transition(.opacity)
            }
            if model.locked {
                LockView().transition(.opacity.combined(with: .scale(scale: 1.04)))
                    .zIndex(2)
            }
        }
        .animation(.easeInOut(duration: 0.25), value: model.server)
        .animation(.easeInOut(duration: 0.2), value: covered)
        .onAppear {
            if phase == .active && model.locked && askOnActive { askOnActive = false; Task { await model.unlock() } }
        }
        .onChange(of: phase) { newPhase in
            switch newPhase {
            case .background:
                model.appWentToBackground()
                askOnActive = true
            case .inactive:
                covered = model.faceIDEnabled     // hide the dashboard in the app switcher
            case .active:
                covered = false
                // only once: a cancelled Face ID sheet also comes back through .active, and must not ask again
                if model.locked && askOnActive { askOnActive = false; Task { await model.unlock() } }
            @unknown default: break
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
