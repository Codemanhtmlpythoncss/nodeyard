import SwiftUI

/// The app's own settings (the dashboard's settings are a dashboard page).
struct SettingsView: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var web: WebController
    @Environment(\.dismiss) private var dismiss
    @State private var address = ""
    @State private var checking = false
    @State private var problem: String?
    @State private var confirmSignOut = false
    @State private var confirmForget = false

    private var version: String {
        let info = Bundle.main.infoDictionary
        let v = info?["CFBundleShortVersionString"] as? String ?? "1.0"
        let b = info?["CFBundleVersion"] as? String ?? "1"
        return "\(v) (\(b))"
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("my-mac.tailnet.ts.net", text: $address)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .submitLabel(.done)
                        .onSubmit(save)
                    Button(action: save) {
                        HStack {
                            Text("Save address")
                            if checking { Spacer(); ProgressView() }
                        }
                    }
                    .disabled(checking || AppModel.normalize(address) == model.server)
                } header: {
                    Text("Dashboard address")
                } footer: {
                    if let problem { Text(problem).foregroundColor(Theme.danger) }
                }
                .listRowBackground(Theme.panel)

                Section {
                    Toggle(isOn: Binding(get: { model.faceIDEnabled }, set: { on in Task { await model.setFaceID(on) } })) {
                        Label("Lock with \(model.biometryName)", systemImage: model.biometrySymbol)
                    }
                    .disabled(!model.canUseLock)
                } header: {
                    Text("Privacy")
                } footer: {
                    Text(model.canUseLock
                         ? "Asks for \(model.biometryName) each time you come back to the app. Music keeps playing while it's locked."
                         : "Set a passcode on this iPhone to use the lock.")
                }
                .listRowBackground(Theme.panel)

                Section {
                    Button("Sign out of the dashboard") { confirmSignOut = true }
                        .foregroundColor(Theme.danger)
                    Button("Forget this dashboard") { confirmForget = true }
                        .foregroundColor(Theme.danger)
                }
                .listRowBackground(Theme.panel)

                Section {
                    LabeledContent("Version", value: version)
                    Link(destination: URL(string: "https://tailscale.com/download/ios")!) {
                        Label("Get Tailscale for iPhone", systemImage: "arrow.up.right.square")
                    }
                } header: {
                    Text("About")
                }
                .listRowBackground(Theme.panel)
            }
            .scrollContentBackground(.hidden)
            .background(Theme.ink)
            .navigationTitle("App settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
            }
            .confirmationDialog("Sign out?", isPresented: $confirmSignOut, titleVisibility: .visible) {
                Button("Sign out", role: .destructive) {
                    Task { await web.signOut(); dismiss() }
                }
            } message: {
                Text("You'll need the dashboard password to sign back in.")
            }
            .confirmationDialog("Forget this dashboard?", isPresented: $confirmForget, titleVisibility: .visible) {
                Button("Forget", role: .destructive) {
                    Task {
                        await web.signOut()
                        web.clear()
                        dismiss()
                        model.setServer(nil)
                    }
                }
            } message: {
                Text("The app goes back to the start screen and signs out.")
            }
        }
        .onAppear { address = model.server?.absoluteString ?? "" }
    }

    private func save() {
        guard let url = AppModel.normalize(address) else { problem = "That doesn't look like a web address."; return }
        checking = true
        problem = nil
        Task {
            let result = await AppModel.check(url)
            checking = false
            switch result {
            case .ok:
                Haptics.success()
                model.setServer(url)
                web.start(server: url, route: model.lastPage)
                dismiss()
            case .notNodeyard:
                problem = "Something answered, but it isn't a nodeyard dashboard."
            case .unreachable(let why):
                problem = why
            }
        }
    }
}
