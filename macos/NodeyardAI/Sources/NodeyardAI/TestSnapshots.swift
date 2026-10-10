import AppKit
import SwiftUI

/// Test mode only (see TestMode): with NODEYARD_AI_SNAPSHOT_DIR set, renders every management screen to a PNG in
/// that folder with the live data from the test dashboard, then quits. Lets automated checks look at the screens
/// without screen-recording access. Never runs in normal use.
@MainActor
enum TestSnapshots {
    static var rendering = false

    static func runIfRequested(_ app: AppState) async {
        guard TestMode.isOn, let dir = ProcessInfo.processInfo.environment["NODEYARD_AI_SNAPSHOT_DIR"], !dir.isEmpty else { return }
        let folder = URL(fileURLWithPath: dir, isDirectory: true)
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        await app.manage.refresh()
        await app.manage.refreshModels(scan: false)
        await app.manage.loadDoctor(fresh: false)
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
