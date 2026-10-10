import SwiftUI

/// Guide A: setting up and using Nodeyard AI on a Mac. Kept in the app so it matches the version you run.
struct SetupGuideView: View {
    private struct Step: Identifiable { let id = UUID(); let title: String; let body: String }

    private let steps: [Step] = [
        Step(title: "1. Install or update the app", body: """
        From the Nodeyard repository on this Mac:
            sh scripts/install-macos-ai-app.sh
        It runs the tests that matter, builds the app, checks the bundle, and only then replaces /Applications/Nodeyard AI.app (the old copy is kept as a backup until the new one is in place). Chats, settings and Keychain items are not touched. To build without installing: make build-macos-ai-app (the app lands in dist/).
        """),
        Step(title: "2. Point it at your dashboard", body: """
        Settings › Server address is the Nodeyard dashboard, normally http://YOUR-SERVER:9092 (over Tailscale or your own network). It is not the model API on port 31435: that one only answers chat requests, while the dashboard's control API also lists models, loads them and keeps chats. The app moves an old :31435 address to :9092 by itself.
        Port 9092 speaks plain HTTP. Use it on your own network or Tailscale; for access from the internet use the dashboard's public HTTPS address.
        """),
        Step(title: "3. Add the server API key", body: """
        One key is shared by yardcode, the control API and every model. Find it on the website under Settings › One shared API key, or on the server with: sudo nodeyard ai key --show
        Paste it in Settings › Server API key. It is stored in this Mac's Keychain (this device only), never in a file. It is not the dashboard password.
        """),
        Step(title: "4. Sign in to manage the cluster (optional)", body: """
        Manage › any section asks for the dashboard password, the one on the website's sign-in page. With it this app can do what the website does: run, switch, unload and delete models, restart Kubernetes, run Doctor fixes. Tick "Remember" to keep it in Keychain so the app signs in again by itself; Settings › Management › Forget removes it. Without the password, chat, model status and automatic unloading still work with the API key.
        """),
        Step(title: "5. Choose a model and chat", body: """
        The model menu in the toolbar lists models that are ready. A model that isn't loaded can be started from Chat › Models or Manage › AI models. The first answer from a big model on CPU nodes can take minutes while it reads the conversation; the chat shows how long it has waited. Stop (⌘.) cancels a request.
        """),
        Step(title: "6. Automatic model unloading", body: """
        Manage › AI models › Automatic model unloading (the same setting as the website's Models tab, stored on the server). Off by default. When on, a model that hasn't been used for the chosen time (5 minutes to 2 hours, or custom) is unloaded to free memory. A model that is answering is never unloaded, and nothing is loaded again by itself.
        """),
        Step(title: "7. Web search and using this Mac", body: """
        Chat settings › AI tools. Web search uses the Nodeyard server's internet connection and returns page text to the model. "Let AI use this Mac" lets the model read the frontmost app's visible text and, with your approval each time, click a named button, fill the focused text field, press a safe key or open an allowed app. It needs System Settings › Privacy & Security › Accessibility › Nodeyard AI. Password fields are never read or typed into.
        These tools need a model that supports tool calls; the split model (llama.cpp with --jinja) does.
        """),
        Step(title: "8. The Agent Browser", body: """
        Chat settings › AI tools › "Let AI use the Agent Browser". The AI can then open pages in a private browser window (Window › Agent Browser, ⇧⌘B), read their text and numbered links, buttons and fields, click, type, go back, read the page's JavaScript errors and take snapshots. Use it for sites that need JavaScript or interaction, and for testing your own sites. It has its own website data, forgets everything when the app quits, and never sees your Safari or Chrome sign-ins. Every click and text entry asks you first; password fields are blocked; only http and https pages open.
        """),
        Step(title: "Troubleshooting", body: """
        • "HTTP 401: That isn't this server's API key": paste the key again (step 3). If it works with the model but not the dashboard, choose Settings › One shared API key › Use the running model's key on the website.
        • "HTTP 403 … control API is off": the server has no API key yet. Make one on the website (Settings) or with sudo nodeyard ai key --rotate.
        • "HTTP 429 Too many wrong keys": wait five minutes.
        • A certificate or TLS error: the address uses https:// on port 9092 (use http://) or the model port 31435 (use 9092).
        • "Not connected": check the address in a browser; the dashboard's /api/health answers {"ok":true}. Over Tailscale, check that this Mac is connected.
        • A model keeps reloading: look at Manage › Alerts for "ran out of memory" (OOMKilled) and at the restarts in Manage › AI models. Use a smaller context or quant, or more machines.
        • Management says "Sign in": the dashboard password changed or the session ended; sign in again.
        """),
        Step(title: "What is not in the app yet", body: """
        The website's Terminal, Commands and Settings pages (passwords, keys, public access, background) are not built into the app; open the dashboard in a browser for those. Research Mode runs on the website (AI › Research). The Agent Browser can't use your existing browser's signed-in sessions, by design.
        """),
    ]

    var body: some View {
        Scrolling {
            VStack(alignment: .leading, spacing: 18) {
                SectionHeader(title: "Setup guide", subtitle: "Nodeyard AI for macOS")
                ForEach(steps) { step in
                    VStack(alignment: .leading, spacing: 6) {
                        Text(step.title).font(.headline)
                        Text(step.body).font(.callout).foregroundStyle(.secondary).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                    }
                }
            }.padding(24).frame(maxWidth: 820, alignment: .leading)
        }
    }
}
