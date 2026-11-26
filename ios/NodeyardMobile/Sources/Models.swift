import Foundation

// What /api/state and /api/stream send. Everything is optional: older and newer servers add and drop fields.

struct Snapshot: Decodable {
    var state: ClusterState?
    var totals: Totals?
    var alerts: [Alert]?
    var error: String?
    var updated: Double?
    var mode: String?
    var host: String?
    var build: String?
}

struct ClusterState: Decodable {
    var cluster: ClusterInfo?
    var nodes: [Node]?
    var pods: [Pod]?
    var workloads: [Workload]?
    var services: [Service]?
    var events: [KubeEvent]?
    var namespaces: [String]?
}

struct ClusterInfo: Decodable {
    var name: String?
    var k3s_version: String?
    var api_server: String?
    var created: Double?
}

struct Totals: Decodable {
    var nodes: Int?
    var nodes_ready: Int?
    var cpu_total: Double?
    var cpu_used: Double?
    var mem_total: Double?
    var mem_used: Double?
    var disk_total: Double?
    var disk_used: Double?
    var net_rx_rate: Double?
    var net_tx_rate: Double?
    var pods: Int?
    var pods_running: Int?
    var pod_states: [String: Int]?
    var namespaces: Int?
    var services: Int?
    var workloads: Int?
    var restarts: Int?

    var cpuFraction: Double { fraction(cpu_used, cpu_total) }
    var memFraction: Double { fraction(mem_used, mem_total) }
    var diskFraction: Double { fraction(disk_used, disk_total) }
}

func fraction(_ used: Double?, _ total: Double?) -> Double {
    guard let used, let total, total > 0 else { return 0 }
    return min(1, max(0, used / total))
}

struct Node: Decodable, Identifiable, Hashable {
    var name: String
    var ready: Bool?
    var status: String?
    var roles: [String]?
    var internal_ip: String?
    var os: String?
    var kernel: String?
    var arch: String?
    var kubelet: String?
    var runtime: String?
    var cpu_cores: Double?
    var cpu_used: Double?
    var mem_total: Double?
    var mem_used: Double?
    var disk_total: Double?
    var disk_used: Double?
    var net_rx_rate: Double?
    var net_tx_rate: Double?
    var pods_running: Int?
    var pods_total: Int?
    var pods_capacity: Int?
    var conditions: [String: String]?
    var unschedulable: Bool?
    var created: Double?
    var hw: Hardware?

    var id: String { name }
    var isReady: Bool { ready ?? (status == "Ready") }
    var cpuFraction: Double { fraction(cpu_used, cpu_cores) }
    var memFraction: Double { fraction(mem_used, mem_total) }
    var diskFraction: Double { fraction(disk_used, disk_total) }
    var role: String {
        let r = roles ?? []
        return r.contains("control-plane") || r.contains("master") ? "Control plane" : "Worker"
    }
    static func == (a: Node, b: Node) -> Bool { a.name == b.name }
    func hash(into h: inout Hasher) { h.combine(name) }
}

struct Hardware: Decodable {
    var cpu_model: String?
    var cores: Double?
    var temp_c: Double?
    var freq_mhz: Double?
    var freq_max: Double?
    var load: [Double]?
    var uptime: Double?
    var processes: Double?
    var undervoltage: Bool?
    var swap_total: Double?
    var swap_free: Double?
}

struct Pod: Decodable, Identifiable, Hashable {
    var namespace: String
    var name: String
    var status: String?
    var ready: String?
    var restarts: Int?
    var node: String?
    var ip: String?
    var created: Double?
    var started: Double?
    var owner: String?
    var qos: String?
    var cpu: Double?
    var mem: Double?
    var containers: [Container]?

    var id: String { namespace + "/" + name }
    var health: Health {
        switch status ?? "" {
        case "Running", "Succeeded", "Completed": return (ready.map { r in
            let p = r.split(separator: "/"); return p.count == 2 && p[0] != p[1] } ?? false) ? .warning : .good
        case "Pending", "ContainerCreating", "Terminating", "PodInitializing": return .warning
        default: return .bad
        }
    }
    static func == (a: Pod, b: Pod) -> Bool { a.id == b.id }
    func hash(into h: inout Hasher) { h.combine(id) }
}

struct Container: Decodable, Hashable {
    var name: String
    var image: String?
    var ready: Bool?
    var restarts: Int?
    var state: String?
    var reason: String?
}

struct Workload: Decodable, Identifiable {
    var kind: String?
    var namespace: String
    var name: String
    var desired: Int?
    var ready: Int?
    var available: Int?
    var images: [String]?
    var id: String { (kind ?? "") + "/" + namespace + "/" + name }
    var healthy: Bool { (ready ?? 0) >= (desired ?? 0) }
}

struct Service: Decodable, Identifiable {
    var namespace: String
    var name: String
    var type: String?
    var cluster_ip: String?
    var ports: [String]?          // "80/TCP (node 30080)"
    var node_ports: [Int]?
    var endpoints: Int?
    var id: String { namespace + "/" + name }
}

struct KubeEvent: Decodable, Identifiable {
    var type: String?
    var reason: String?
    var message: String?
    var object: String?
    var namespace: String?
    var count: Int?
    var last: Double?
    var id: String { "\(object ?? "")|\(reason ?? "")|\(last ?? 0)" }
    var warning: Bool { type == "Warning" }
}

struct Alert: Decodable, Identifiable {
    var level: String?
    var title: String?
    var detail: String?
    var kind: String?
    var ref: String?
    var id: String { "\(kind ?? "")|\(ref ?? "")|\(title ?? "")" }
    var critical: Bool { level == "critical" }
}

enum Health { case good, warning, bad }

// MARK: - /api/history

struct HistoryReply: Decodable {
    var interval: Double?
    var points: [HistoryPoint]?
}

struct HistoryPoint: Decodable {
    var t: Double
    var pods: Int?
    var nodes: [String: [Double?]]?
}

/// One value on a chart.
struct Sample: Identifiable {
    let time: Date
    let value: Double
    var id: Date { time }
}

// MARK: - Formatting

enum Format {
    static func bytes(_ v: Double?) -> String {
        guard let v, v > 0 else { return "0 B" }
        return ByteCountFormatter.string(fromByteCount: Int64(v), countStyle: .binary)
    }

    static func rate(_ v: Double?) -> String { bytes(v) + "/s" }

    static func percent(_ f: Double) -> String { "\(Int((f * 100).rounded()))%" }

    static func cores(_ v: Double?) -> String {
        guard let v else { return "–" }
        return v < 10 ? String(format: "%.1f", v) : String(format: "%.0f", v)
    }

    /// "3d", "5h", "12m" since a Unix time.
    static func age(_ since: Double?) -> String {
        guard let since, since > 0 else { return "–" }
        return duration(Date().timeIntervalSince1970 - since)
    }

    static func duration(_ s: Double) -> String {
        let s = max(0, s)
        if s < 60 { return "\(Int(s))s" }
        if s < 3600 { return "\(Int(s / 60))m" }
        if s < 86400 { return "\(Int(s / 3600))h" }
        return "\(Int(s / 86400))d"
    }

    static func clock(_ seconds: Double) -> String {
        guard seconds.isFinite, seconds >= 0 else { return "0:00" }
        let s = Int(seconds)
        return s >= 3600 ? String(format: "%d:%02d:%02d", s / 3600, s / 60 % 60, s % 60) : String(format: "%d:%02d", s / 60, s % 60)
    }
}
