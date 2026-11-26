import Foundation

/// One page of the dashboard. `id` is its route (the part after # in the web address).
struct DashboardPage: Identifiable, Hashable {
    let id: String
    let title: String
    let symbol: String
    let blurb: String
}

struct PageSection: Identifiable {
    let id: String
    let pages: [DashboardPage]
}

enum Pages {
    /// The four pages on the tab bar. Everything else lives under More.
    static let tabs: [DashboardPage] = [overview, ai, music, terminal]

    static let overview = DashboardPage(id: "overview", title: "Overview", symbol: "square.grid.2x2.fill", blurb: "Your cluster at a glance")
    static let ai = DashboardPage(id: "ai", title: "AI", symbol: "sparkles", blurb: "Models running on your cluster")
    static let music = DashboardPage(id: "music", title: "Music", symbol: "music.note", blurb: "Your library and SoundCloud")
    static let terminal = DashboardPage(id: "terminal", title: "Terminal", symbol: "terminal.fill", blurb: "Shells on every machine")

    static let sections: [PageSection] = [
        PageSection(id: "Cluster", pages: [
            overview,
            DashboardPage(id: "nodes", title: "Nodes", symbol: "server.rack", blurb: "Every machine and its address"),
            DashboardPage(id: "processes", title: "Processes", symbol: "cpu", blurb: "What is running where"),
            DashboardPage(id: "pods", title: "Pods", symbol: "shippingbox.fill", blurb: "Everything running in Kubernetes"),
            DashboardPage(id: "workloads", title: "Workloads", symbol: "square.stack.3d.up.fill", blurb: "Deployments and jobs"),
            DashboardPage(id: "cleanup", title: "Cleanup", symbol: "trash.fill", blurb: "Pods nobody has used lately"),
        ]),
        PageSection(id: "Health", pages: [
            DashboardPage(id: "alerts", title: "Alerts", symbol: "bell.badge.fill", blurb: "Things that need you"),
            DashboardPage(id: "doctor", title: "Doctor", symbol: "stethoscope", blurb: "Checks and fixes"),
            DashboardPage(id: "events", title: "Events", symbol: "list.bullet.rectangle.fill", blurb: "What Kubernetes has done"),
            DashboardPage(id: "logs", title: "Logs", symbol: "doc.text.fill", blurb: "Cluster events over time"),
            DashboardPage(id: "services", title: "Services", symbol: "waveform.path.ecg", blurb: "Uptime and latency"),
        ]),
        PageSection(id: "Network and hardware", pages: [
            DashboardPage(id: "network", title: "Network", symbol: "network", blurb: "Addresses and ports"),
            DashboardPage(id: "access", title: "Network access", symbol: "lock.shield.fill", blurb: "Tailnet and sharing"),
            DashboardPage(id: "storage", title: "Storage", symbol: "externaldrive.fill", blurb: "Disks and volumes"),
            DashboardPage(id: "hardware", title: "Hardware", symbol: "memorychip.fill", blurb: "What every machine has"),
            DashboardPage(id: "updates", title: "Updates", symbol: "arrow.triangle.2.circlepath", blurb: "Outdated software"),
            DashboardPage(id: "tools", title: "Tools", symbol: "wrench.and.screwdriver.fill", blurb: "Network checks and Wake-on-LAN"),
        ]),
        PageSection(id: "AI", pages: [
            ai,
            DashboardPage(id: "galaxy", title: "AI galaxy", symbol: "circle.hexagongrid.fill", blurb: "Your cluster in 3D"),
            DashboardPage(id: "train", title: "Train AI", symbol: "brain", blurb: "Train your own model"),
            DashboardPage(id: "tune", title: "Tuning", symbol: "slider.horizontal.3", blurb: "AI settings and power"),
            DashboardPage(id: "apis", title: "APIs", symbol: "curlybraces", blurb: "Your self-hosted APIs"),
        ]),
        PageSection(id: "Media", pages: [
            music,
            DashboardPage(id: "video", title: "Video", symbol: "film.fill", blurb: "Upload and watch"),
            DashboardPage(id: "desk", title: "Desk", symbol: "photo.on.rectangle.angled", blurb: "Wallpaper and clock"),
        ]),
        PageSection(id: "Server", pages: [
            terminal,
            DashboardPage(id: "commands", title: "Commands", symbol: "command", blurb: "Every nodeyard command"),
            DashboardPage(id: "pwnforge", title: "PwnForge", symbol: "shield.lefthalf.filled", blurb: "Host the PwnForge lab"),
            DashboardPage(id: "setup", title: "Add devices", symbol: "plus.app.fill", blurb: "Set up another machine"),
            DashboardPage(id: "settings", title: "Server settings", symbol: "gearshape.2.fill", blurb: "Passwords, keys and ports"),
            DashboardPage(id: "guide", title: "Setup guide", symbol: "book.fill", blurb: "How everything works"),
        ]),
    ]

    static let all: [DashboardPage] = sections.flatMap(\.pages)

    static func page(_ id: String) -> DashboardPage? { all.first { $0.id == id } }
}
