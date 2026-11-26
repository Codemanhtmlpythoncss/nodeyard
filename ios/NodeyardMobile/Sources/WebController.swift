import Combine
import SwiftUI
import UIKit
import WebKit

/// Owns the one web view that shows the dashboard. It lives for the whole app, so switching tabs only changes the
/// page's #route: music keeps playing, terminals stay open and nothing reloads.
@MainActor
final class WebController: NSObject, ObservableObject {
    @Published private(set) var route = Pages.overview.id     // the dashboard page on screen
    @Published private(set) var onLoginPage = false
    @Published private(set) var progress: Double = 0
    @Published private(set) var loading = false
    @Published private(set) var loadError: String?

    let webView: WKWebView
    private var base: URL?
    private var observers: Set<AnyCancellable> = []
    private let refresh = UIRefreshControl()

    /// Reports every route change to the app (the dashboard changes #route itself, e.g. from its own links).
    private static let routeScript = """
    (function () {
      function send() {
        try {
          window.webkit.messageHandlers.nodeyard.postMessage({
            path: location.pathname, route: decodeURIComponent(location.hash.slice(1)).split("!")[0]
          });
        } catch (e) {}
      }
      window.addEventListener("hashchange", send);
      send();
    })();
    """

    /// Small fixes for a phone screen inside the app: no text-size jumps on rotate, no long-press link previews.
    private static let styleScript = """
    (function () {
      var s = document.createElement("style");
      s.textContent = "html{-webkit-text-size-adjust:100%}a,img{-webkit-touch-callout:none}";
      document.documentElement.appendChild(s);
    })();
    """

    override init() {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()              // the sign-in cookie survives restarts
        config.allowsInlineMediaPlayback = true
        config.mediaTypesRequiringUserActionForPlayback = []
        config.allowsPictureInPictureMediaPlayback = true
        config.defaultWebpagePreferences.preferredContentMode = .mobile
        let content = WKUserContentController()
        content.addUserScript(WKUserScript(source: Self.routeScript, injectionTime: .atDocumentEnd, forMainFrameOnly: true))
        content.addUserScript(WKUserScript(source: Self.styleScript, injectionTime: .atDocumentEnd, forMainFrameOnly: true))
        config.userContentController = content

        webView = WKWebView(frame: .zero, configuration: config)
        super.init()

        content.add(WeakMessageHandler(self), name: "nodeyard")
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.isOpaque = false
        webView.backgroundColor = Theme.uiInk
        webView.underPageBackgroundColor = Theme.uiInk
        webView.scrollView.backgroundColor = Theme.uiInk
        webView.scrollView.contentInsetAdjustmentBehavior = .never
        webView.scrollView.keyboardDismissMode = .interactive
        webView.allowsLinkPreview = false
        if #available(iOS 16.4, *) { webView.isInspectable = true }

        refresh.tintColor = UIColor(Theme.mist)
        refresh.addTarget(self, action: #selector(pulledToRefresh), for: .valueChanged)
        webView.scrollView.refreshControl = refresh

        webView.publisher(for: \.estimatedProgress).receive(on: RunLoop.main)
            .sink { [weak self] in self?.progress = $0 }.store(in: &observers)
        webView.publisher(for: \.isLoading).receive(on: RunLoop.main)
            .sink { [weak self] in self?.loading = $0 }.store(in: &observers)
    }

    // MARK: - Navigation

    /// Open a dashboard (or reopen it after the address changed).
    func start(server: URL, route: String) {
        base = server
        self.route = route
        loadError = nil
        webView.load(URLRequest(url: pageURL(route)))
    }

    /// Show another dashboard page. Inside the running dashboard this only changes #route (instant, keeps state).
    func go(_ route: String) {
        guard let base else { return }
        loadError = nil
        self.route = route
        if let url = webView.url, url.host == base.host, url.path == "/" || url.path.isEmpty {
            let js = "location.hash = " + jsString("#" + route)
            webView.evaluateJavaScript(js, completionHandler: nil)
        } else {
            webView.load(URLRequest(url: pageURL(route)))
        }
    }

    func reload() {
        loadError = nil
        if webView.url == nil, base != nil { webView.load(URLRequest(url: pageURL(route))) } else { webView.reload() }
    }

    var started: Bool { base != nil }

    var currentURL: URL? { webView.url ?? base.map { pageURL(route, base: $0) } }

    /// Sign out everywhere in the app: forget the dashboard's cookies and stored data, then show the sign-in page.
    func signOut() async {
        let store = WKWebsiteDataStore.default()
        await store.removeData(ofTypes: WKWebsiteDataStore.allWebsiteDataTypes(), modifiedSince: .distantPast)
        webView.evaluateJavaScript("try{navigator.mediaSession.metadata=null}catch(e){}", completionHandler: nil)
        reload()
    }

    /// Stop everything (used when the dashboard address is removed).
    func clear() {
        base = nil
        webView.stopLoading()
        webView.loadHTMLString("", baseURL: nil)
    }

    private func pageURL(_ route: String, base: URL? = nil) -> URL {
        var parts = URLComponents(url: base ?? self.base!, resolvingAgainstBaseURL: false)!
        parts.path = "/"
        parts.queryItems = [URLQueryItem(name: "embed", value: "1")]   // the page without the website's own sidebar
        parts.fragment = route
        return parts.url!
    }

    private func jsString(_ s: String) -> String {
        let data = (try? JSONSerialization.data(withJSONObject: [s])) ?? Data("[\"\"]".utf8)
        let array = String(decoding: data, as: UTF8.self)
        return String(array.dropFirst().dropLast())
    }

    @objc private func pulledToRefresh() {
        reload()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { [weak self] in self?.refresh.endRefreshing() }
    }

    fileprivate func received(_ dict: [String: String]) {
        let path = dict["path"] ?? "/"
        onLoginPage = path.hasPrefix("/login")
        if let r = dict["route"], !r.isEmpty, !onLoginPage { route = r }
    }

    private func isDashboard(_ url: URL) -> Bool {
        guard let base else { return false }
        return url.host == base.host && url.port == base.port
    }

    private func topController() -> UIViewController? {
        let scene = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first
        var top = scene?.windows.first(where: \.isKeyWindow)?.rootViewController
        while let next = top?.presentedViewController { top = next }
        return top
    }
}

// MARK: - WKNavigationDelegate

extension WebController: WKNavigationDelegate {
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping @MainActor @Sendable (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { return decisionHandler(.cancel) }
        let scheme = url.scheme?.lowercased() ?? ""
        if ["about", "blob", "data"].contains(scheme) { return decisionHandler(.allow) }
        guard action.targetFrame?.isMainFrame ?? false else { return decisionHandler(.allow) }   // iframes (3D galaxy)
        guard isDashboard(url) else {
            // links to other sites (SoundCloud, docs, GitHub) open in their own app or Safari
            if ["http", "https", "mailto", "tel", "ssh"].contains(scheme) { UIApplication.shared.open(url) }
            return decisionHandler(.cancel)
        }
        // After signing in the website goes to "/": put it back in app mode, on the page the app was showing.
        if url.path == "/" || url.path.isEmpty, !(url.query ?? "").contains("embed=1") {
            decisionHandler(.cancel)
            let wanted = url.fragment.flatMap { $0.isEmpty ? nil : $0 } ?? route
            webView.load(URLRequest(url: pageURL(wanted)))
            return
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        loadError = nil
        refresh.endRefreshing()
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) { failed(error) }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) { failed(error) }

    private func failed(_ error: Error) {
        refresh.endRefreshing()
        let ns = error as NSError
        if ns.domain == NSURLErrorDomain && ns.code == NSURLErrorCancelled { return }
        if ns.domain == "WebKitErrorDomain" && ns.code == 102 { return }     // frame load interrupted by our own redirect
        switch ns.code {
        case NSURLErrorCannotFindHost, NSURLErrorDNSLookupFailed:
            loadError = "Can't find your dashboard. If it's on your tailnet, turn on Tailscale."
        case NSURLErrorTimedOut, NSURLErrorCannotConnectToHost, NSURLErrorNetworkConnectionLost:
            loadError = "Your dashboard isn't answering. It may be restarting, or Tailscale is off."
        case NSURLErrorNotConnectedToInternet:
            loadError = "This phone is offline."
        default:
            loadError = error.localizedDescription
        }
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        reload()       // iOS reclaimed the page's memory while in the background: bring it back
    }
}

// MARK: - WKUIDelegate (the dashboard's confirm/alert/prompt dialogs, new windows, microphone)

extension WebController: WKUIDelegate {
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url {
            if isDashboard(url) { webView.load(action.request) } else { UIApplication.shared.open(url) }
        }
        return nil
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping @MainActor @Sendable () -> Void) {
        let alert = UIAlertController(title: nil, message: message, preferredStyle: .alert)
        alert.addAction(UIAlertAction(title: "OK", style: .default) { _ in completionHandler() })
        present(alert, orElse: completionHandler)
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping @MainActor @Sendable (Bool) -> Void) {
        let alert = UIAlertController(title: nil, message: message, preferredStyle: .alert)
        alert.addAction(UIAlertAction(title: "Cancel", style: .cancel) { _ in completionHandler(false) })
        alert.addAction(UIAlertAction(title: "OK", style: .default) { _ in completionHandler(true) })
        present(alert) { completionHandler(false) }
    }

    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String, defaultText: String?,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping @MainActor @Sendable (String?) -> Void) {
        let alert = UIAlertController(title: nil, message: prompt, preferredStyle: .alert)
        alert.addTextField { $0.text = defaultText }
        alert.addAction(UIAlertAction(title: "Cancel", style: .cancel) { _ in completionHandler(nil) })
        alert.addAction(UIAlertAction(title: "OK", style: .default) { _ in completionHandler(alert.textFields?.first?.text) })
        present(alert) { completionHandler(nil) }
    }

    @available(iOS 15.0, *)
    func webView(_ webView: WKWebView, requestMediaCapturePermissionFor origin: WKSecurityOrigin, initiatedByFrame frame: WKFrameInfo,
                 type: WKMediaCaptureType, decisionHandler: @escaping @MainActor @Sendable (WKPermissionDecision) -> Void) {
        decisionHandler(origin.host == base?.host ? .grant : .deny)   // iOS still asks the user once for the microphone
    }

    private func present(_ alert: UIAlertController, orElse fallback: @escaping () -> Void) {
        guard let top = topController() else { return fallback() }
        top.present(alert, animated: true)
    }
}

/// WKUserContentController keeps its handlers strongly; this breaks the cycle back to the controller.
private final class WeakMessageHandler: NSObject, WKScriptMessageHandler {
    weak var target: WebController?
    init(_ target: WebController) { self.target = target }
    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        let body = message.body as? [String: String] ?? [:]
        Task { @MainActor [weak target] in target?.received(body) }
    }
}

/// Puts the shared web view into SwiftUI.
struct DashboardWebView: UIViewRepresentable {
    let controller: WebController
    func makeUIView(context: Context) -> WKWebView { controller.webView }
    func updateUIView(_ view: WKWebView, context: Context) {}
}
