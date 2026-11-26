import SwiftUI

/// The dashboard: a native header, the web page, and a docked tab bar.
struct MainView: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var web: WebController
    @State private var showMore = false
    @State private var showSettings = false

    private var page: DashboardPage? { Pages.page(web.route) }

    var body: some View {
        VStack(spacing: 0) {
            if !web.onLoginPage {
                header
            }
            ZStack(alignment: .top) {
                DashboardWebView(controller: web)
                    .ignoresSafeArea(.container, edges: web.onLoginPage ? [.top, .bottom] : [])
                if web.loading && web.progress < 1 {
                    ProgressBar(value: web.progress)
                        .transition(.opacity)
                }
                if let error = web.loadError {
                    ErrorCard(message: error, retry: web.reload, settings: { showSettings = true })
                        .transition(.opacity)
                }
            }
            if !web.onLoginPage {
                TabBar(current: web.route, more: { showMore = true }) { route in
                    Haptics.tap()
                    web.go(route)
                }
            }
        }
        .background(Theme.ink.ignoresSafeArea())
        .animation(.easeInOut(duration: 0.2), value: web.onLoginPage)
        .animation(.easeInOut(duration: 0.2), value: web.loadError)
        .onAppear {
            if !web.started, let server = model.server { web.start(server: server, route: model.lastPage) }
        }
        .onChange(of: web.route) { model.lastPage = $0 }
        .sheet(isPresented: $showMore) {
            MoreView(current: web.route) { route in
                showMore = false
                web.go(route)
            } openSettings: {
                showMore = false
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) { showSettings = true }
            }
        }
        .sheet(isPresented: $showSettings) { SettingsView() }
    }

    private var header: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 1) {
                Text(page?.title ?? "Nodeyard")
                    .font(Theme.title(26))
                    .lineLimit(1)
                    .contentTransition(.opacity)
                Text(model.server?.host ?? "")
                    .font(.system(size: 12, weight: .medium, design: .rounded))
                    .foregroundStyle(Theme.mist)
                    .lineLimit(1)
            }
            Spacer()
            Menu {
                Button { web.reload() } label: { Label("Reload", systemImage: "arrow.clockwise") }
                if let url = web.currentURL {
                    Button { UIApplication.shared.open(url) } label: { Label("Open in Safari", systemImage: "safari") }
                    ShareLink(item: url) { Label("Share link", systemImage: "square.and.arrow.up") }
                }
                Divider()
                Button { showSettings = true } label: { Label("App settings", systemImage: "gearshape") }
            } label: {
                Image(systemName: "ellipsis")
                    .font(.system(size: 17, weight: .semibold))
                    .frame(width: 40, height: 40)
                    .background(Theme.panel, in: Circle())
                    .overlay(Circle().stroke(Theme.line))
            }
            .accessibilityLabel("Page options")
        }
        .padding(.horizontal, 20)
        .padding(.top, 6)
        .padding(.bottom, 10)
        .animation(.easeInOut(duration: 0.15), value: web.route)
    }
}

// MARK: - Tab bar

private struct TabBar: View {
    let current: String
    let more: () -> Void
    let go: (String) -> Void

    private var onTab: Bool { Pages.tabs.contains { $0.id == current } }

    var body: some View {
        HStack(spacing: 0) {
            ForEach(Pages.tabs) { page in
                item(page.title, page.symbol, selected: current == page.id) { go(page.id) }
            }
            item("More", "square.grid.3x3.fill", selected: !onTab, action: more)
        }
        .padding(.top, 8)
        .padding(.horizontal, 8)
        .background(alignment: .top) {
            Theme.panel.opacity(0.96)
                .overlay(alignment: .top) { Rectangle().fill(Theme.line).frame(height: 1) }
                .ignoresSafeArea(edges: .bottom)
        }
    }

    private func item(_ title: String, _ symbol: String, selected: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            VStack(spacing: 4) {
                ZStack {
                    if selected {
                        Capsule().fill(Theme.indigo.opacity(0.22)).frame(width: 56, height: 30)
                    }
                    Image(systemName: symbol)
                        .font(.system(size: 18, weight: .semibold))
                        .foregroundStyle(selected ? AnyShapeStyle(Theme.brand) : AnyShapeStyle(Theme.mist))
                }
                .frame(height: 30)
                Text(title)
                    .font(.system(size: 11, weight: selected ? .bold : .medium, design: .rounded))
                    .foregroundStyle(selected ? .white : Theme.mist)
            }
            .frame(maxWidth: .infinity)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(selected ? .isSelected : [])
        .animation(.spring(response: 0.3, dampingFraction: 0.8), value: selected)
    }
}

// MARK: - Loading and errors

private struct ProgressBar: View {
    let value: Double
    var body: some View {
        GeometryReader { geo in
            Rectangle().fill(Theme.brand)
                .frame(width: max(24, geo.size.width * value), height: 2)
                .animation(.easeOut(duration: 0.2), value: value)
        }
        .frame(height: 2)
    }
}

private struct ErrorCard: View {
    let message: String
    let retry: () -> Void
    let settings: () -> Void

    var body: some View {
        ZStack {
            Theme.ink.ignoresSafeArea()
            VStack(spacing: 14) {
                Image(systemName: "wifi.exclamationmark")
                    .font(.system(size: 40, weight: .semibold))
                    .foregroundStyle(Theme.brand)
                Text("Can't reach your dashboard")
                    .font(Theme.title(22))
                Text(message)
                    .font(.system(size: 15, design: .rounded))
                    .foregroundStyle(Theme.mist)
                    .multilineTextAlignment(.center)
                Button("Try again", action: retry)
                    .buttonStyle(BrandButtonStyle())
                    .padding(.top, 10)
                Button("Change address", action: settings)
                    .font(.system(size: 15, weight: .semibold, design: .rounded))
                    .padding(.top, 4)
            }
            .padding(28)
            .frame(maxWidth: 420)
        }
    }
}

// MARK: - More

struct MoreView: View {
    let current: String
    let go: (String) -> Void
    let openSettings: () -> Void

    var body: some View {
        NavigationStack {
            List {
                ForEach(Pages.sections) { section in
                    Section(section.id) {
                        ForEach(section.pages) { page in
                            Button { go(page.id) } label: { row(page) }
                                .listRowBackground(Theme.panel)
                        }
                    }
                }
                Section {
                    Button(action: openSettings) {
                        row(DashboardPage(id: "", title: "App settings", symbol: "iphone.gen3", blurb: "Address, lock and sign out"))
                    }
                    .listRowBackground(Theme.panel)
                }
            }
            .scrollContentBackground(.hidden)
            .background(Theme.ink)
            .navigationTitle("Everything")
            .navigationBarTitleDisplayMode(.large)
        }
        .presentationDragIndicator(.visible)
    }

    private func row(_ page: DashboardPage) -> some View {
        HStack(spacing: 14) {
            Image(systemName: page.symbol)
                .font(.system(size: 16, weight: .semibold))
                .foregroundStyle(.white)
                .frame(width: 34, height: 34)
                .background(page.id == current ? AnyShapeStyle(Theme.brand) : AnyShapeStyle(Theme.indigo.opacity(0.25)),
                            in: RoundedRectangle(cornerRadius: 9, style: .continuous))
            VStack(alignment: .leading, spacing: 2) {
                Text(page.title)
                    .font(.system(size: 16, weight: .semibold, design: .rounded))
                    .foregroundStyle(.white)
                Text(page.blurb)
                    .font(.system(size: 13, design: .rounded))
                    .foregroundStyle(Theme.mist)
            }
            Spacer()
            if page.id == current {
                Image(systemName: "checkmark").foregroundStyle(Theme.teal).font(.system(size: 14, weight: .bold))
            }
        }
        .padding(.vertical, 2)
    }
}
