import Charts
import SwiftUI

struct OverviewView: View {
    @EnvironmentObject private var cluster: ClusterStore
    @State private var settings = false

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    header
                    if let problem = cluster.problem, !cluster.live { ErrorBanner(text: problem) }
                    if let snap = cluster.snapshot, let t = snap.totals {
                        gauges(t)
                        if !cluster.alerts.isEmpty { alerts }
                        stats(t)
                        if cluster.cpuHistory.count > 2 { chart }
                        nodes
                    } else if cluster.problem == nil {
                        VStack(spacing: 14) {
                            NodeMark(size: 72)
                            Text("Reaching your cluster…").font(.system(size: 15, design: .rounded)).foregroundStyle(Theme.mist)
                        }
                        .frame(maxWidth: .infinity)
                        .padding(.top, 80)
                    }
                }
                .padding(.horizontal, 16)
                .padding(.bottom, 24)
            }
            .screen()
            .navigationTitle(cluster.state?.cluster?.name ?? "Overview")
            .toolbar {
                ToolbarItem(placement: .navigationBarTrailing) {
                    Button { settings = true } label: { Image(systemName: "gearshape") }
                        .accessibilityLabel("Settings")
                }
            }
            .sheet(isPresented: $settings) { SettingsView() }
            .navigationDestination(for: Node.self) { NodeDetail(name: $0.name) }
            .navigationDestination(for: Pod.self) { PodDetail(pod: $0) }
        }
    }

    private var header: some View {
        HStack(spacing: 8) {
            Circle().fill(cluster.live ? Color(hex: 0x3DDC97) : .orange).frame(width: 8, height: 8)
            Text(cluster.live ? "Live" : "Reconnecting")
                .font(.system(size: 13, weight: .semibold, design: .rounded))
            if let v = cluster.state?.cluster?.k3s_version {
                Text(v).font(.system(size: 13, design: .rounded)).foregroundStyle(Theme.mist)
            }
            Spacer()
        }
        .animation(.easeInOut, value: cluster.live)
    }

    private func gauges(_ t: Totals) -> some View {
        Panel {
            HStack {
                Ring(fraction: t.cpuFraction, label: "CPU", detail: "\(Format.cores(t.cpu_used)) of \(Format.cores(t.cpu_total)) cores")
                Spacer(minLength: 4)
                Ring(fraction: t.memFraction, label: "Memory", detail: "\(Format.bytes(t.mem_used)) of \(Format.bytes(t.mem_total))")
                Spacer(minLength: 4)
                Ring(fraction: t.diskFraction, label: "Disk", detail: "\(Format.bytes(t.disk_used)) of \(Format.bytes(t.disk_total))")
            }
            .frame(maxWidth: .infinity)
        }
    }

    private var alerts: some View {
        VStack(spacing: 8) {
            ForEach(cluster.alerts.prefix(6)) { a in
                HStack(alignment: .top, spacing: 12) {
                    Image(systemName: a.critical ? "exclamationmark.octagon.fill" : "exclamationmark.triangle.fill")
                        .foregroundStyle(a.critical ? Theme.danger : .orange)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(a.title ?? "").font(.system(size: 15, weight: .semibold, design: .rounded))
                        if let d = a.detail, !d.isEmpty {
                            Text(d).font(.system(size: 13)).foregroundStyle(Theme.mist).lineLimit(3)
                        }
                    }
                    Spacer(minLength: 0)
                }
                .padding(12)
                .background((a.critical ? Theme.danger : Color.orange).opacity(0.1), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
            }
        }
    }

    private func stats(_ t: Totals) -> some View {
        let columns = [GridItem(.flexible(), spacing: 10), GridItem(.flexible(), spacing: 10)]
        return LazyVGrid(columns: columns, spacing: 10) {
            Stat(symbol: "server.rack", value: "\(t.nodes_ready ?? 0)/\(t.nodes ?? 0)", label: "Nodes ready")
            Stat(symbol: "shippingbox", value: "\(t.pods_running ?? 0)/\(t.pods ?? 0)", label: "Pods running")
            Stat(symbol: "arrow.down", value: Format.rate(t.net_rx_rate), label: "Network in")
            Stat(symbol: "arrow.up", value: Format.rate(t.net_tx_rate), label: "Network out")
            Stat(symbol: "square.stack.3d.up", value: "\(t.workloads ?? 0)", label: "Workloads")
            Stat(symbol: "arrow.clockwise", value: "\(t.restarts ?? 0)", label: "Restarts")
        }
    }

    private var chart: some View {
        Panel {
            VStack(alignment: .leading, spacing: 12) {
                HStack {
                    Text("Recent usage").font(.system(size: 15, weight: .semibold, design: .rounded))
                    Spacer()
                    Legend(color: Theme.indigo, text: "CPU")
                    Legend(color: Theme.teal, text: "Memory")
                }
                Chart {
                    ForEach(cluster.cpuHistory) { s in
                        LineMark(x: .value("Time", s.time), y: .value("Used", s.value), series: .value("Kind", "CPU"))
                            .foregroundStyle(Theme.indigo)
                            .interpolationMethod(.monotone)
                    }
                    ForEach(cluster.memHistory) { s in
                        LineMark(x: .value("Time", s.time), y: .value("Used", s.value), series: .value("Kind", "Memory"))
                            .foregroundStyle(Theme.teal)
                            .interpolationMethod(.monotone)
                    }
                }
                .chartYScale(domain: 0...1)
                .chartYAxis {
                    AxisMarks(values: [0, 0.5, 1]) { v in
                        AxisGridLine().foregroundStyle(Theme.line)
                        AxisValueLabel { Text(Format.percent(v.as(Double.self) ?? 0)).foregroundStyle(Theme.mist) }
                    }
                }
                .chartXAxis {
                    AxisMarks(values: .automatic(desiredCount: 4)) { _ in
                        AxisValueLabel(format: .dateTime.hour().minute()).foregroundStyle(Theme.mist)
                    }
                }
                .frame(height: 150)
            }
        }
    }

    private var nodes: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Nodes").font(Theme.title(20)).padding(.top, 6)
            ForEach(cluster.nodes) { n in
                NavigationLink(value: n) { NodeCard(node: n) }.buttonStyle(.plain)
            }
        }
    }
}

private struct Legend: View {
    let color: Color
    let text: String
    var body: some View {
        HStack(spacing: 5) {
            Circle().fill(color).frame(width: 7, height: 7)
            Text(text).font(.system(size: 12, design: .rounded)).foregroundStyle(Theme.mist)
        }
    }
}

struct Stat: View {
    let symbol: String
    let value: String
    let label: String
    var body: some View {
        Panel(padding: 14, radius: 16) {
            VStack(alignment: .leading, spacing: 6) {
                Image(systemName: symbol).font(.system(size: 14, weight: .semibold)).foregroundStyle(Theme.teal)
                Text(value).font(.system(size: 20, weight: .bold, design: .rounded)).monospacedDigit().lineLimit(1).minimumScaleFactor(0.6)
                Text(label).font(.system(size: 12, design: .rounded)).foregroundStyle(Theme.mist)
            }
        }
        .accessibilityElement(children: .combine)
    }
}

struct NodeCard: View {
    let node: Node
    var body: some View {
        Panel(padding: 14, radius: 18) {
            VStack(alignment: .leading, spacing: 12) {
                HStack(spacing: 10) {
                    HealthDot(health: node.isReady ? .good : .bad)
                    Text(node.name).font(.system(size: 17, weight: .semibold, design: .rounded))
                    Text(node.role).font(.system(size: 12, design: .rounded)).foregroundStyle(Theme.mist)
                        .padding(.horizontal, 8).padding(.vertical, 3)
                        .background(Color.white.opacity(0.06), in: Capsule())
                    Spacer()
                    if let temp = node.hw?.temp_c {
                        Label("\(Int(temp))°", systemImage: "thermometer.medium")
                            .font(.system(size: 13, design: .rounded)).foregroundStyle(temp > 75 ? .orange : Theme.mist)
                    }
                    Image(systemName: "chevron.right").font(.system(size: 13, weight: .semibold)).foregroundStyle(Theme.mist)
                }
                HStack(spacing: 14) {
                    MeterLabel(title: "CPU", fraction: node.cpuFraction)
                    MeterLabel(title: "Mem", fraction: node.memFraction)
                    MeterLabel(title: "Disk", fraction: node.diskFraction)
                }
            }
        }
    }
}

struct MeterLabel: View {
    let title: String
    let fraction: Double
    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack {
                Text(title).foregroundStyle(Theme.mist)
                Spacer()
                Text(Format.percent(fraction)).monospacedDigit()
            }
            .font(.system(size: 12, weight: .medium, design: .rounded))
            Meter(fraction: fraction)
        }
    }
}
