import AppKit
import SwiftUI

/// Test mode only (see TestMode): with NODEYARD_AI_SNAPSHOT_DIR set, renders every management screen to a PNG in
/// that folder with the live data from the test dashboard, then quits. Lets automated checks look at the screens
/// without screen-recording access. Never runs in normal use.
@MainActor
enum TestSnapshots {
    static var rendering = false

    /// Test mode only: NODEYARD_AI_BROWSER_SELFTEST=URL drives the Agent Browser through a test page and writes what
    /// each action returned to NODEYARD_AI_SNAPSHOT_DIR/browser-selftest.txt, then quits.
    static func runBrowserSelfTestIfRequested() async {
        let env = ProcessInfo.processInfo.environment
        guard TestMode.isOn, let page = env["NODEYARD_AI_BROWSER_SELFTEST"], let dir = env["NODEYARD_AI_SNAPSHOT_DIR"] else { return }
        let browser = AgentBrowser.shared
        var out: [String] = []
        func record(_ label: String, _ action: () async throws -> String) async {
            do { out.append("## \(label)\n" + (try await action())) } catch { out.append("## \(label)\nERROR: \(error.localizedDescription)") }
        }
        await record("open") { try await browser.navigate(page) }
        await record("read") { try await browser.read() }
        await record("type") { try await browser.type(1, text: "hello from the agent", submit: false) }
        await record("password") { try await browser.type(2, text: "secret", submit: false) }
        await record("click") { try await browser.click(3) }
        await record("read after click") { try await browser.read(maxChars: 400) }
        await record("console") { try await browser.consoleMessages() }
        await record("snapshot") { try await browser.takeSnapshot() }
        await record("follow link") { try await browser.click(4) }
        await record("back") { try await browser.back() }
        await record("bad url") { try await browser.navigate("file:///etc/passwd") }
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        try? out.joined(separator: "\n\n").write(toFile: dir + "/browser-selftest.txt", atomically: true, encoding: .utf8)
        NSApp.terminate(nil)
    }

    static func runIfRequested(_ app: AppState) async {
        guard TestMode.isOn, let dir = ProcessInfo.processInfo.environment["NODEYARD_AI_SNAPSHOT_DIR"], !dir.isEmpty else { return }
        let folder = URL(fileURLWithPath: dir, isDirectory: true)
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        await app.manage.refresh()
        await app.manage.refreshModels(scan: false)
        await app.manage.loadDoctor(fresh: false)
        await app.manage.refreshConnections()
        await app.manage.refreshPlugins()
        await app.manage.refreshResearch()
        if let latest = app.manage.researchSessions.first?["id"].string {
            app.manage.openResearch(latest)
            try? await Task.sleep(nanoseconds: 1_500_000_000)
        }
        rendering = true
        for section in ManageSection.allCases {
            app.manage.section = section
            let view = ManageDetail().environmentObject(app.manage).environmentObject(app)
                .frame(width: 1180, height: 860).background(Color(nsColor: .windowBackgroundColor))
            let renderer = ImageRenderer(content: view)
            renderer.scale = 1
            if let image = renderer.nsImage, let tiff = image.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
               let png = rep.representation(using: .png, properties: [:]) {
                try? png.write(to: folder.appendingPathComponent("\(section.rawValue).png"))
            }
        }
        NSApp.terminate(nil)
    }
}

/// A vertical ScrollView, except while TestSnapshots renders (ImageRenderer can't draw AppKit-backed scroll views).
struct Scrolling<Content: View>: View {
    @ViewBuilder var content: () -> Content
    var body: some View {
        if TestSnapshots.rendering { VStack(alignment: .leading, spacing: 0) { content() }.frame(maxHeight: .infinity, alignment: .top) }
        else { ScrollView { content() } }
    }
}

/// HSplitView, except while TestSnapshots renders (an HStack then, for the same reason as Scrolling).
struct SplitOrStack<Content: View>: View {
    @ViewBuilder var content: () -> Content
    var body: some View {
        if TestSnapshots.rendering { HStack(alignment: .top, spacing: 0) { content() } } else { HSplitView { content() } }
    }
}
