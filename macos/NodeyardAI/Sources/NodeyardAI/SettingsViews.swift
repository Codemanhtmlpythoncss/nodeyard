import SwiftUI

struct AppSettingsView: View {
    @EnvironmentObject private var state: AppState
    @Environment(\.dismiss) private var dismiss
    @State private var address = ""
    @State private var key = ""
    @State private var noLimit = false
    @State private var saved = false

    var body: some View {
        VStack(spacing: 0) {
            HStack { Text("Settings").font(.title.bold()); Spacer(); Button { dismiss() } label: { Image(systemName: "xmark") }.buttonStyle(.plain).help("Close settings") }.padding(18)
            Divider()
            Form {
                Section("Nodeyard server") {
                    TextField("Server address", text: $address, prompt: Text("https://nodeyard.example:9092"))
                        .autocorrectionDisabled()
                    SecureField("Server API key", text: $key).textContentType(.password).autocorrectionDisabled()
                    HStack {
                        Label(state.isConnected ? "Connected" : "Not connected", systemImage: state.isConnected ? "checkmark.circle.fill" : "circle.dashed")
                            .foregroundStyle(state.isConnected ? Color.green : Color.secondary)
                        Spacer()
                        Button("Test connection") { saveConnection(); state.refresh() }
                    }
                    Text("Use the shared server API key from Nodeyard Dashboard → Settings. It is stored in macOS Keychain.")
                        .font(.caption).foregroundStyle(.secondary)
                    if address.lowercased().hasPrefix("http://") {
                        Label("HTTP does not encrypt your key in transit. Use HTTPS on shared or public networks.", systemImage: "lock.open.fill")
                            .font(.caption).foregroundStyle(.orange)
                    }
                }
                Section("New chat defaults") {
                    TextField("System prompt", text: $state.defaults.systemPrompt, axis: .vertical).lineLimit(3...6)
                    HStack {
                        Text("Creativity \(state.defaults.temperature, specifier: "%.1f")")
                        Slider(value: $state.defaults.temperature, in: 0...2, step: 0.1)
                    }
                    HStack {
                        Text("Max reply length")
                        TextField("Tokens", value: $state.defaults.maxTokens, format: .number).frame(width: 110).disabled(noLimit)
                        Toggle("No limit", isOn: $noLimit).toggleStyle(.checkbox)
                    }
                    Stepper("Default context: \(state.defaults.contextLength.formatted()) tokens", value: $state.defaults.contextLength, in: 512...131072, step: 512)
                    Picker("Appearance", selection: $state.defaults.appearance) {
                        Text("System").tag("system"); Text("Light").tag("light"); Text("Dark").tag("dark")
                    }
                    HStack { Text("Chat text size"); Slider(value: $state.defaults.fontSize, in: 12...22, step: 1); Text("\(Int(state.defaults.fontSize)) pt").monospacedDigit().frame(width: 52) }
                }
                Section("Chat saving and alerts") {
                    Toggle("Sync completed chats to the Nodeyard server", isOn: $state.defaults.syncChats)
                    Toggle("Notify when a reply is complete", isOn: $state.defaults.notifications)
                    Text("Every conversation is saved locally in your Mac's Application Support folder. Server sync is additional and can be turned off.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            .formStyle(.grouped).padding(.horizontal, 12)
            Divider()
            HStack {
                Button("Import chat JSON…") { state.importJSON() }
                Spacer()
                Button("Save defaults") { saveConnection(); if noLimit { state.defaults.maxTokens = 0 }; state.saveDefaults(); saved = true }
                    .keyboardShortcut(.defaultAction)
            }.padding(16)
        }
        .onAppear { address = state.serverAddress; key = state.apiKey; noLimit = state.defaults.maxTokens == 0 }
        .onChange(of: state.defaults.maxTokens) { value in noLimit = value == 0 }
    }

    private func saveConnection() {
        state.setServerAddress(address)
        state.setAPIKey(key)
        if saved { state.info = "Settings saved." }
    }
}

struct ChatSettingsView: View {
    @EnvironmentObject private var state: AppState
    @Environment(\.dismiss) private var dismiss
    let chat: ChatRecord
    @State private var systemPrompt = ""
    @State private var temperature = 0.7
    @State private var maxTokens = 1024
    @State private var contextLength = 8192
    @State private var noLimit = false

    var body: some View {
        VStack(spacing: 0) {
            HStack { Text("Chat settings").font(.title2.bold()); Spacer(); Button { dismiss() } label: { Image(systemName: "xmark") }.buttonStyle(.plain) }.padding(18)
            Divider()
            Form {
                Section("This conversation") {
                    TextField("System prompt", text: $systemPrompt, axis: .vertical).lineLimit(4...8)
                    HStack { Text("Creativity \(temperature, specifier: "%.1f")"); Slider(value: $temperature, in: 0...2, step: 0.1) }
                    HStack { Text("Max reply length"); TextField("Tokens", value: $maxTokens, format: .number).frame(width: 110).disabled(noLimit); Toggle("No limit", isOn: $noLimit).toggleStyle(.checkbox) }
                    Stepper("Context length: \(contextLength.formatted()) tokens", value: $contextLength, in: 512...131072, step: 512)
                    Text("These values belong to this chat. The model context length takes effect when the model is next started.").font(.caption).foregroundStyle(.secondary)
                }
            }.formStyle(.grouped).padding(.horizontal, 12)
            Spacer()
            HStack { Spacer(); Button("Cancel") { dismiss() }; Button("Save chat settings") { save() }.keyboardShortcut(.defaultAction) }.padding(16)
        }.onAppear { systemPrompt = chat.systemPrompt; temperature = chat.temperature; maxTokens = chat.maxTokens; noLimit = chat.maxTokens == 0; contextLength = chat.contextLength }
    }

    private func save() {
        state.updateCurrentChat { chat in
            chat.systemPrompt = systemPrompt; chat.temperature = temperature; chat.maxTokens = noLimit ? 0 : maxTokens; chat.contextLength = contextLength
        }
        dismiss()
    }
}
