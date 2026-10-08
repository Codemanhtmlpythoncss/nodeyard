import SwiftUI

@main
struct NodeyardAIApp: App {
    @StateObject private var state = AppState()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(state)
                .frame(minWidth: 920, minHeight: 640)
                .preferredColorScheme(state.defaults.appearance == "system" ? nil : (state.defaults.appearance == "light" ? .light : .dark))
                .task { state.refresh() }
        }
        .windowStyle(.titleBar)
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
        }
    }
}
