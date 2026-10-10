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
                    if let suggestion = NodeyardClient.suggestedDashboardAddress(from: address) {
                        Label("This looks like a model endpoint. Nodeyard AI needs the dashboard address.", systemImage: "exclamationmark.triangle.fill")
                            .font(.caption).foregroundStyle(.orange)
                        Button {
                            address = suggestion
                            saveConnection()
                            state.refresh()
                        } label: { Label("Use dashboard address · \(suggestion)", systemImage: "arrow.triangle.turn.up.right.diamond") }
                    }
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
                        Label("HTTP has no TLS protection. Use it only on a private LAN or Tailscale. Use a valid HTTPS dashboard address on public networks.", systemImage: "lock.open.fill")
                            .font(.caption).foregroundStyle(.orange)
                    }
                }
                Section("Management") {
                    ManagementSettingsRow()
                }
                Section("About") {
                    let info = Bundle.main.infoDictionary ?? [:]
                    Text("Nodeyard AI \(info["CFBundleShortVersionString"] as? String ?? "?") (build \(info["CFBundleVersion"] as? String ?? "?"), source \(info["NodeyardSourceCommit"] as? String ?? "unknown"))")
                        .font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                    Text("Setup guide: Manage › Setup guide (⌘3).").font(.caption).foregroundStyle(.secondary)
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
                Section("AI tools") {
                    Toggle("Let AI search the web and read pages", isOn: Binding(get: { state.defaults.browserUse ?? true }, set: { state.defaults.browserUse = $0 }))
                    Text("The model chooses when to search. Requests use the Nodeyard server's web connection and return page text to the model.")
                        .font(.caption).foregroundStyle(.secondary)
                    Toggle("Let AI use this Mac", isOn: Binding(get: { state.defaults.computerUse ?? false }, set: { state.defaults.computerUse = $0 }))
                    Text("Shares visible text from the frontmost app with the selected model. Password values are excluded. Every click, text entry, key press and app launch asks you first.")
                        .font(.caption).foregroundStyle(.secondary)
                    HStack {
                        Label(MacComputerUse.permissionGranted() ? "Accessibility access is on" : "Accessibility access is off",
                              systemImage: MacComputerUse.permissionGranted() ? "checkmark.circle.fill" : "lock.fill")
                            .foregroundStyle(MacComputerUse.permissionGranted() ? Color.green : Color.secondary)
                        Spacer()
                        Button("Request access") { state.requestComputerUsePermission() }
                    }
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
    @State private var browserUse = true
    @State private var computerUse = false

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
                Section("AI tools for this chat") {
                    Toggle("Let AI search the web and read pages", isOn: $browserUse)
                    Toggle("Let AI use this Mac", isOn: $computerUse)
                    Text("Computer actions need macOS Accessibility access and your approval each time. Password fields are blocked.")
                        .font(.caption).foregroundStyle(.secondary)
                    if !MacComputerUse.permissionGranted() {
                        Button("Request Accessibility access") { state.requestComputerUsePermission() }
                    }
                }
            }.formStyle(.grouped).padding(.horizontal, 12)
            Spacer()
            HStack { Spacer(); Button("Cancel") { dismiss() }; Button("Save chat settings") { save() }.keyboardShortcut(.defaultAction) }.padding(16)
        }.onAppear {
            systemPrompt = chat.systemPrompt; temperature = chat.temperature; maxTokens = chat.maxTokens
            noLimit = chat.maxTokens == 0; contextLength = chat.contextLength
            browserUse = chat.browserUse ?? state.defaults.browserUse ?? true
            computerUse = chat.computerUse ?? state.defaults.computerUse ?? false
        }
    }

    private func save() {
        state.updateCurrentChat { chat in
            chat.systemPrompt = systemPrompt; chat.temperature = temperature; chat.maxTokens = noLimit ? 0 : maxTokens
            chat.contextLength = contextLength; chat.browserUse = browserUse; chat.computerUse = computerUse
        }
        dismiss()
    }
}

/// Sign-in state for the website's management features, and a way to forget the saved password.
private struct ManagementSettingsRow: View {
    @EnvironmentObject private var mgmt: ManagementState
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Label(mgmt.signedIn ? "Signed in to the dashboard" : (mgmt.authEnabled ? "Not signed in" : "This dashboard has no sign-in"),
                      systemImage: mgmt.signedIn ? "checkmark.circle.fill" : "person.crop.circle.badge.questionmark")
                    .foregroundStyle(mgmt.signedIn ? Color.green : Color.secondary)
                Spacer()
                if mgmt.signedIn { Button("Sign out") { Task { await mgmt.signOut(forget: false) } } }
                if mgmt.savedPassword { Button("Forget saved password", role: .destructive) { Task { await mgmt.signOut(forget: true) } } }
            }
            Text("Managing the cluster (Manage, ⌘3) uses the dashboard password from the website's sign-in page. \(mgmt.savedPassword ? "It is saved in this Mac's Keychain." : "It is not saved; you sign in when you open Manage.") The server API key above is separate and is never used as a password.")
                .font(.caption).foregroundStyle(.secondary)
        }
    }
}
