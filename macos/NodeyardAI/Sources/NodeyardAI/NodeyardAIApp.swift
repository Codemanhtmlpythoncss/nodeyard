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
                .task { state.refresh(); await TestSnapshots.runBrowserSelfTestIfRequested(); await TestSnapshots.runIfRequested(state) }
        }
        .windowStyle(.titleBar)

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
                Divider()
                Button("Refresh Models") { state.refresh() }.keyboardShortcut("r", modifiers: .command)
                Button("Sync Saved Chats") { state.syncNow() }.keyboardShortcut("s", modifiers: [.command, .option])
            }
            CommandGroup(after: .windowList) { OpenAgentBrowserButton() }
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
