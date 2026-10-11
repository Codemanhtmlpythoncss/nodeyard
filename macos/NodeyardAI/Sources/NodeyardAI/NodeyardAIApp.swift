import SwiftUI

@main
struct NodeyardAIApp: App {
    @StateObject private var state = AppState()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(state)
                .environmentObject(state.manage)
                .frame(minWidth: 920, minHeight: 640)
                .preferredColorScheme(state.defaults.appearance == "system" ? nil : (state.defaults.appearance == "light" ? .light : .dark))
                .task { state.refresh(); await TestSnapshots.runBrowserSelfTestIfRequested(); await TestSnapshots.runTerminalSelfTestIfRequested(state); await TestSnapshots.runIfRequested(state) }
        }
        .windowStyle(.titleBar)

        WindowGroup("Terminal", for: TerminalRequest.self) { $request in
            TerminalWindowView(request: request).environmentObject(state.manage).frame(minWidth: 640, minHeight: 420)
        }

        Window("AI Galaxy", id: "galaxy") {
            GalaxyView().environmentObject(state.manage).frame(minWidth: 900, minHeight: 600).background(Color.black)
        }

        Window("Agent Browser", id: "agent-browser") {
            AgentBrowserView().frame(minWidth: 820, minHeight: 560)
        }
        .commands {
            CommandGroup(replacing: .newItem) {
                Button("New Chat") { state.createChat() }.keyboardShortcut("n", modifiers: .command)
                Button("Import Chat…") { state.importJSON() }.keyboardShortcut("o", modifiers: [.command, .shift])
            }
            CommandMenu("Chat") {
                Button("Send Message") { state.beginSend() }.keyboardShortcut(.return, modifiers: [.command])
                Button("Stop Generation") { state.stop() }.keyboardShortcut(".", modifiers: .command).disabled(!state.isSending)
                Button("Regenerate Answer") { state.regenerate() }.keyboardShortcut("r", modifiers: [.command, .shift])
                Button("Read Last Answer Aloud / Stop") { state.readLastAnswer() }.keyboardShortcut("r", modifiers: [.command, .option])
                Toggle("Read Answers Aloud as They Arrive", isOn: Binding(get: { state.defaults.readAloud ?? false }, set: { state.defaults.readAloud = $0; if !$0 { SpeechReader.shared.stop() } }))
                Divider()
                Button("Refresh Models") { state.refresh() }.keyboardShortcut("r", modifiers: .command)
                Button("Sync Saved Chats") { state.syncNow() }.keyboardShortcut("s", modifiers: [.command, .option])
            }
            CommandGroup(after: .windowList) { OpenAgentBrowserButton(); OpenGalaxyButton() }
            CommandMenu("Manage") {
                Button("Chat") { state.workspace = .chat }.keyboardShortcut("1", modifiers: .command)
                Button("Models") { state.workspace = .models }.keyboardShortcut("2", modifiers: .command)
                Button("Manage Cluster") { state.workspace = .manage }.keyboardShortcut("3", modifiers: .command)
                Divider()
                ForEach(ManageSection.allCases) { section in
                    Button(section.title) { state.workspace = .manage; state.manage.section = section }
                }
            }
        }
    }
}

private struct OpenAgentBrowserButton: View {
    @Environment(\.openWindow) private var openWindow
    var body: some View { Button("Agent Browser") { openWindow(id: "agent-browser") }.keyboardShortcut("b", modifiers: [.command, .shift]) }
}

private struct OpenGalaxyButton: View {
    @Environment(\.openWindow) private var openWindow
    var body: some View { Button("AI Galaxy") { openWindow(id: "galaxy") }.keyboardShortcut("g", modifiers: [.command, .shift]) }
}
