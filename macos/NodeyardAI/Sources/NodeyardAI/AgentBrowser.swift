import AppKit
import SwiftUI
import WebKit

enum AgentBrowserError: LocalizedError {
    case badURL(String)
    case timeout(String)
    case navigation(String)
    case noElement(Int)
    case secureField
    case script(String)

    var errorDescription: String? {
        switch self {
        case .badURL(let url): "“\(url)” isn't an http or https address."
        case .timeout(let url): "\(url) didn't finish loading within 30 seconds (the part that loaded can still be read)."
        case .navigation(let detail): "The page didn't load: \(detail)"
        case .noElement(let n): "There is no element [\(n)] on the page now. Read the page again to get fresh numbers."
        case .secureField: "For safety, the agent browser never types into password fields."
        case .script(let detail): "The page didn't answer: \(detail)"
        }
    }
}

/// The browser the AI drives: one WKWebView with its own, non-persistent website data, so none of your Safari or Chrome
/// sign-ins, cookies or saved passwords are ever visible to it, and everything is forgotten when the app quits. It shows
/// in its own window (Window › Agent Browser) so you can watch, and you can browse in it yourself.
@MainActor
final class AgentBrowser: NSObject, ObservableObject, WKNavigationDelegate {
    static let shared = AgentBrowser()

    @Published var url = ""
    @Published var title = ""
    @Published var isLoading = false
    @Published var lastError: String?
    @Published var log: [String] = []
    @Published var snapshot: NSImage?

    let webView: WKWebView
    private var navigationContinuation: CheckedContinuation<Void, Error>?

    /// Collects console errors and uncaught exceptions, and numbers the elements the AI can use.
    private static let probe = """
    (function () {
      if (window.__ny) return;
      window.__ny = { console: [] };
      const keep = (kind, args) => { try { window.__ny.console.push(kind + ": " + Array.from(args).map(String).join(" ").slice(0, 500)); if (window.__ny.console.length > 100) window.__ny.console.shift(); } catch (e) {} };
      const err = console.error, warn = console.warn;
      console.error = function () { keep("error", arguments); return err.apply(console, arguments); };
      console.warn = function () { keep("warning", arguments); return warn.apply(console, arguments); };
      window.addEventListener("error", (e) => keep("uncaught", [e.message + " (" + (e.filename || "") + ":" + (e.lineno || 0) + ")"]));
      window.addEventListener("unhandledrejection", (e) => keep("unhandled promise", [e.reason]));
    })();
    """

    override init() {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .nonPersistent()
        config.userContentController.addUserScript(WKUserScript(source: Self.probe, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        webView = WKWebView(frame: NSRect(x: 0, y: 0, width: 1100, height: 800), configuration: config)
        webView.customUserAgent = nil
        super.init()
        webView.navigationDelegate = self
    }

    private func note(_ line: String) {
        log.append(line)
        if log.count > 200 { log.removeFirst(log.count - 200) }
    }

    // MARK: actions (each returns text for the model)

    func navigate(_ raw: String) async throws -> String {
        var text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if !text.contains("://") { text = "https://" + text }
        guard let target = URL(string: text), let scheme = target.scheme?.lowercased(), ["http", "https"].contains(scheme), target.host != nil else {
            throw AgentBrowserError.badURL(raw)
        }
        note("Open \(target.absoluteString)")
        try await load { self.webView.load(URLRequest(url: target, timeoutInterval: 30)) }
        let pageTitle = (try? await evaluate("document.title")) ?? webView.title ?? ""
        title = pageTitle
        return "Opened \(webView.url?.absoluteString ?? target.absoluteString) — “\(pageTitle)”. Use page_read to see it."
    }

    func back() async throws -> String {
        guard webView.canGoBack else { return "There is no earlier page." }
        note("Back")
        try await load { self.webView.goBack() }
        return "Back at \(webView.url?.absoluteString ?? "") — “\(webView.title ?? "")”."
    }

    /// The visible text and every link, button and field, numbered [n] for page_click and page_type.
    func read(maxChars: Int = 12000) async throws -> String {
        let script = """
        (function () {
          const vis = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none"; };
          const name = (el) => (el.getAttribute("aria-label") || el.innerText || el.value || el.getAttribute("placeholder") || el.getAttribute("title") || el.getAttribute("name") || el.getAttribute("alt") || "").replace(/\\s+/g, " ").trim().slice(0, 90);
          document.querySelectorAll("[data-ny]").forEach((el) => el.removeAttribute("data-ny"));
          const items = []; let n = 0;
          document.querySelectorAll("a[href], button, input, textarea, select, [role=button], [role=link], [role=tab], [role=checkbox], [contenteditable=true]").forEach((el) => {
            if (!vis(el) || n >= 150) return;
            n += 1; el.setAttribute("data-ny", String(n));
            const tag = el.tagName.toLowerCase(), type = (el.getAttribute("type") || "").toLowerCase();
            const kind = tag === "a" ? "link" : tag === "input" ? (type || "text") + " field" : tag === "textarea" ? "text area" : tag === "select" ? "menu" : (el.getAttribute("role") || tag);
            items.push("[" + n + "] " + kind + ": " + (type === "password" ? "(password field)" : name(el)) + (tag === "a" ? " → " + (el.getAttribute("href") || "").slice(0, 120) : ""));
          });
          const text = (document.body ? document.body.innerText : "").replace(/\\n{3,}/g, "\\n\\n").trim();
          return JSON.stringify({ title: document.title, url: location.href, text: text, items: items });
        })();
        """
        let raw = try await evaluate(script)
        guard let data = raw.data(using: .utf8), let page = JSON.parse(data) else { throw AgentBrowserError.script("unreadable page summary") }
        let items = page["items"].array.map(\.text)
        var body = page["text"].text
        if body.count > maxChars { body = String(body.prefix(maxChars)) + "\n… [page text cut at \(maxChars) characters]" }
        note("Read \(page["url"].text)")
        return "Page: \(page["title"].text)\nURL: \(page["url"].text)\n\nText:\n\(body)\n\nInteractive elements (use their numbers):\n\(items.joined(separator: "\n"))"
    }

    func describe(_ n: Int) async throws -> String {
        let raw = try await evaluate("""
        (function () { const el = document.querySelector('[data-ny="\(n)"]'); if (!el) return ""; const t = (el.getAttribute("type") || "").toLowerCase();
          return JSON.stringify({ tag: el.tagName.toLowerCase(), type: t, name: (el.getAttribute("aria-label") || el.innerText || el.value || el.getAttribute("placeholder") || el.getAttribute("name") || "").replace(/\\s+/g, " ").trim().slice(0, 90) }); })();
        """)
        guard let data = raw.data(using: .utf8), let info = JSON.parse(data) else { throw AgentBrowserError.noElement(n) }
        if info["type"].text == "password" { throw AgentBrowserError.secureField }
        return "\(info["tag"].text)\(info["type"].text.isEmpty ? "" : " (\(info["type"].text))") “\(info["name"].text)”"
    }

    func click(_ n: Int) async throws -> String {
        let before = webView.url
        let ok = try await evaluate("""
        (function () { const el = document.querySelector('[data-ny="\(n)"]'); if (!el) return "missing"; el.scrollIntoView({block: "center"}); el.click(); return "ok"; })();
        """)
        guard ok == "ok" else { throw AgentBrowserError.noElement(n) }
        note("Click [\(n)]")
        try? await Task.sleep(nanoseconds: 1_200_000_000)        // let the page react (navigation, scripts)
        while webView.isLoading { try? await Task.sleep(nanoseconds: 300_000_000) }
        let moved = webView.url != before
        return "Clicked [\(n)]." + (moved ? " The page is now \(webView.url?.absoluteString ?? "") — “\(webView.title ?? "")”." : " The page address didn't change.") + " Read the page again to see the result."
    }

    func type(_ n: Int, text: String, submit: Bool) async throws -> String {
        let payload = String(data: try JSONSerialization.data(withJSONObject: [text], options: []), encoding: .utf8) ?? "[\"\"]"
        let result = try await evaluate("""
        (function () { const el = document.querySelector('[data-ny="\(n)"]'); if (!el) return "missing";
          if ((el.getAttribute("type") || "").toLowerCase() === "password") return "secure";
          const value = \(payload)[0]; el.focus();
          if (el.isContentEditable) { el.textContent = value; } else {
            const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : el.tagName === "SELECT" ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, "value"); if (setter && setter.set) setter.set.call(el, value); else el.value = value; }
          el.dispatchEvent(new Event("input", { bubbles: true })); el.dispatchEvent(new Event("change", { bubbles: true }));
          if (\(submit ? "true" : "false")) { if (el.form && el.form.requestSubmit) el.form.requestSubmit(); else el.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })); }
          return "ok"; })();
        """)
        if result == "secure" { throw AgentBrowserError.secureField }
        guard result == "ok" else { throw AgentBrowserError.noElement(n) }
        note("Type into [\(n)]" + (submit ? " and submit" : ""))
        if submit {
            try? await Task.sleep(nanoseconds: 1_200_000_000)
            while webView.isLoading { try? await Task.sleep(nanoseconds: 300_000_000) }
        }
        return "Entered the text in [\(n)]" + (submit ? " and submitted it. The page is now \(webView.url?.absoluteString ?? "")." : ".")
    }

    func consoleMessages() async throws -> String {
        let raw = try await evaluate("JSON.stringify((window.__ny && window.__ny.console) || [])")
        let list = (raw.data(using: .utf8).flatMap { JSON.parse($0) }?.array ?? []).map(\.text)
        note("Read the console")
        return list.isEmpty ? "No console errors or warnings on \(webView.url?.absoluteString ?? "this page")." : "Console errors and warnings (oldest first):\n" + list.joined(separator: "\n")
    }

    /// A picture of the page (shown in the browser window; its size is reported to the model, which may not see images).
    func takeSnapshot() async throws -> String {
        let image: NSImage
        do { image = try await webView.takeSnapshot(configuration: nil) }
        catch { throw AgentBrowserError.script(error.localizedDescription) }
        snapshot = image
        note("Snapshot")
        return "Took a snapshot of \(webView.url?.absoluteString ?? "the page") (\(Int(image.size.width))×\(Int(image.size.height))). It is shown in the Agent Browser window."
    }

    // MARK: plumbing

    private func evaluate(_ script: String) async throws -> String {
        guard webView.url != nil else { throw AgentBrowserError.script("no page is open: use page_open first") }
        do {
            let value = try await webView.evaluateJavaScript(script)
            return (value as? String) ?? ""
        } catch { throw AgentBrowserError.script(error.localizedDescription) }
    }

    private func load(_ start: @escaping () -> Void) async throws {
        isLoading = true
        lastError = nil
        defer { isLoading = false; url = webView.url?.absoluteString ?? url; title = webView.title ?? "" }
        let timeout = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 30_000_000_000)
            guard !Task.isCancelled, let self, let pending = self.navigationContinuation else { return }
            self.navigationContinuation = nil
            pending.resume(throwing: AgentBrowserError.timeout(self.webView.url?.absoluteString ?? "The page"))
        }
        defer { timeout.cancel() }
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            navigationContinuation?.resume(throwing: CancellationError())
            navigationContinuation = continuation
            start()
        }
    }

    private func finish(_ error: Error?) {
        url = webView.url?.absoluteString ?? url
        title = webView.title ?? ""
        guard let continuation = navigationContinuation else { return }
        navigationContinuation = nil
        if let error { lastError = error.localizedDescription; continuation.resume(throwing: AgentBrowserError.navigation(error.localizedDescription)) }
        else { continuation.resume() }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) { finish(nil) }
    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) { finish(error) }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) { finish(error) }
    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping @MainActor @Sendable (WKNavigationActionPolicy) -> Void) {
        // Only web pages: no file://, custom app schemes or downloads from the agent browser.
        let scheme = navigationAction.request.url?.scheme?.lowercased() ?? ""
        decisionHandler(["http", "https", "about", "data", "blob"].contains(scheme) ? .allow : .cancel)
    }
}

/// The Agent Browser window: the page the AI is using, its address, what it has done, and the latest snapshot.
struct AgentBrowserView: View {
    @ObservedObject private var browser = AgentBrowser.shared
    @State private var address = ""

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Button { Task { _ = try? await browser.back() } } label: { Image(systemName: "chevron.left") }.disabled(!browser.webView.canGoBack)
                Button { browser.webView.reload() } label: { Image(systemName: "arrow.clockwise") }
                TextField("Address", text: $address).textFieldStyle(.roundedBorder).onSubmit { let a = address; Task { _ = try? await browser.navigate(a) } }
                if browser.isLoading { ProgressView().controlSize(.small) }
                Label("Private, forgets everything on quit", systemImage: "lock.shield").font(.caption).foregroundStyle(.secondary)
            }.padding(8)
            Divider()
            HSplitView {
                WebViewHost(webView: browser.webView).frame(minWidth: 520, minHeight: 420)
                VStack(alignment: .leading, spacing: 8) {
                    Text("What the AI did").font(.headline)
                    if let error = browser.lastError { Text(error).font(.caption).foregroundStyle(.orange) }
                    List(Array(browser.log.enumerated().reversed()), id: \.offset) { Text($0.element).font(.caption) }.frame(minHeight: 160)
                    if let shot = browser.snapshot {
                        Text("Latest snapshot").font(.headline)
                        Image(nsImage: shot).resizable().scaledToFit().frame(maxHeight: 220).border(Color.secondary.opacity(0.3))
                    }
                }.padding(10).frame(minWidth: 230, idealWidth: 260, maxWidth: 340)
            }
        }
        .onReceive(browser.$url) { address = $0 }
    }
}

private struct WebViewHost: NSViewRepresentable {
    let webView: WKWebView
    func makeNSView(context: Context) -> WKWebView { webView }
    func updateNSView(_ nsView: WKWebView, context: Context) {}
}
