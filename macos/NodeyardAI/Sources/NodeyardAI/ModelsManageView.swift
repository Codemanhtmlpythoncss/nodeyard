import SwiftUI

/// The website's AI > Models tab: the split model, automatic unloading, downloaded models and Ollama.
struct ModelsManageView: View {
    @EnvironmentObject private var mgmt: ManagementState
    @State private var confirm: Confirm?
    @State private var switchTarget: String?
    @State private var purgeOld = false
    @State private var switchContext = 8192

    var body: some View {
        Scrolling {
            VStack(alignment: .leading, spacing: 16) {
                SectionHeader(title: "AI models", subtitle: "Models running on your cluster", trailing: AnyView(
                    Button { Task { await mgmt.refreshModels(scan: true); await mgmt.refreshLifecycle() } } label: { Label("Rescan disks", systemImage: "arrow.clockwise") }))
                splitCard
                LifecycleCard()
                downloadedCard
                ollamaCard
            }.padding(24)
        }
        .task { if mgmt.disk == nil { await mgmt.refreshModels(scan: false) } }
        .confirm($confirm)
        .sheet(isPresented: Binding(get: { switchTarget != nil }, set: { if !$0 { switchTarget = nil } })) { switchSheet }
    }

    // MARK: split model

    private var splitCard: some View {
        Card("Split model") {
            VStack(alignment: .leading, spacing: 10) {
                SplitSummary()
                let sp = mgmt.split
                if !sp.isNull {
                    if let load = Optional(sp["load"]), !load.isNull, load["phase"].text == "loading" || load["phase"].text == "warming up" {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("\(load["phase"].text == "loading" ? "Loading into memory" : "Warming up") · \(load["pct"].int ?? 0)%").font(.caption)
                            Meter(fraction: (load["pct"].double ?? 0) / 100)
                        }
                    }
                    HStack(spacing: 8) {
                        if sp["loaded"].bool == false {
                            Button("Load into memory") { Task { await mgmt.run("split-load", title: "Load the split model") } }
                        } else {
                            Button("Unload (free the memory)") {
                                confirm = Confirm(title: "Unload the split model?", message: "Every machine gets its memory back. The downloaded file stays, so loading again takes a minute or two. Chat stops until it is loaded.", button: "Unload", destructive: false) {
                                    Task { await mgmt.run("split-unload", title: "Unload the split model (free its memory)") }
                                }
                            }
                        }
                        Button("Check progress") { Task { await mgmt.run("status", title: "Model status") } }
                        if sp["ready"].bool == true { Button("Speed test") { Task { await mgmt.run("test", title: "Speed test") } } }
                        Spacer()
                        Button("Force stop", role: .destructive) {
                            confirm = Confirm(title: "Force stop the split model?", message: "Its servers stop at once and downloads in progress are cancelled. Downloaded files and weight caches stay.", button: "Force stop") {
                                Task { await mgmt.run("force-stop", title: "Force stop model and cancel downloads") }
                            }
                        }
                        Button("Remove…", role: .destructive) {
                            confirm = Confirm(title: "Remove the split model?", message: "It is unloaded, then removed from the cluster. Its downloaded file stays on disk; delete it under Downloaded models if you want the space back.", button: "Remove") {
                                Task { await mgmt.run("undeploy", title: "Stop and remove the split model") }
                            }
                        }
                    }.disabled(mgmt.busyJob != nil)
                }
            }.frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    // MARK: downloaded models (from the disk scan)

    private var downloaded: [(file: String, size: Double, nodes: [String], stale: [String])] {
        var byFile: [String: (Double, [String], [String])] = [:]
        for node in mgmt.disk?["nodes"].array ?? [] {
            for item in node["items"].array where item["kind"].text == "model" {
                var entry = byFile[item["name"].text] ?? (0, [], [])
                entry.0 = max(entry.0, item["bytes"].double ?? 0)
                entry.1.append(node["node"].text)
                if !node["scan_error"].text.isEmpty { entry.2.append(node["node"].text) }
                byFile[item["name"].text] = entry
            }
        }
        return byFile.map { (file: $0.key, size: $0.value.0, nodes: $0.value.1.sorted(), stale: $0.value.2) }.sorted { $0.file < $1.file }
    }

    private var downloadedCard: some View {
        Card("Downloaded models") {
            VStack(alignment: .leading, spacing: 10) {
                if let disk = mgmt.disk {
                    if disk["scanning"].bool == true { Label("Refreshing the disk inventory…", systemImage: "arrow.triangle.2.circlepath").font(.caption).foregroundStyle(.secondary) }
                    if !disk["scan_error"].text.isEmpty { Text("The inventory is partial: \(disk["scan_error"].text)").font(.caption).foregroundStyle(.orange) }
                    ForEach(rows2(disk["downloads"].array.filter { $0["state"].text != "done" })) { d in
                        VStack(alignment: .leading, spacing: 3) {
                            Text(d.v["file"].text).font(.caption.monospaced())
                            Meter(fraction: Format.ratio(d.v["got"].double, of: d.v["size"].double))
                            Text("\(Format.bytes(d.v["got"].double)) of \(Format.bytes(d.v["size"].double)) · \(d.v["node"].text) · \(d.v["state"].text)").font(.caption2)
                        }
                    }
                    let inUse = disk["in_use"].text
                    if downloaded.isEmpty { Text("No GGUF models saved on the nodes.").foregroundStyle(.secondary) }
                    ForEach(downloaded, id: \.file) { m in
                        HStack(spacing: 10) {
                            Image(systemName: "cpu").foregroundStyle(.tint).frame(width: 32, height: 32).background(.tint.opacity(0.1), in: RoundedRectangle(cornerRadius: 8))
                            VStack(alignment: .leading, spacing: 2) {
                                HStack { Text(m.file).fontWeight(.medium).textSelection(.enabled); if m.file == inUse || m.file == mgmt.split["model"].text { StatusPill(text: "In use", tone: .green) } }
                                Text("\(Format.bytes(m.size)) · on \(m.nodes.map { m.stale.contains($0) ? "\($0) (last seen)" : $0 }.joined(separator: ", "))").font(.caption).foregroundStyle(.secondary)
                            }
                            Spacer()
                            if m.file != inUse && m.file != mgmt.split["model"].text {
                                Button("Run model") { switchContext = mgmt.split["ctx"].int ?? 8192; purgeOld = false; switchTarget = m.file }
                                Button("Delete", role: .destructive) {
                                    confirm = Confirm(title: "Delete \(m.file)?", message: "The file, any unfinished parts of it and its weight caches are deleted from every machine. A download of it that is still running is stopped.", button: "Delete") {
                                        Task { await mgmt.run("split-rm", params: ["file": m.file], title: "Delete \(m.file) from every node") }
                                    }
                                }
                            }
                        }.disabled(mgmt.busyJob != nil)
                    }
                    HStack {
                        Button("Free up space") { Task { await mgmt.run("clean", title: "Free up space") } }
                        Button("…including unused models", role: .destructive) {
                            confirm = Confirm(title: "Free up space, including models?", message: "This also deletes every downloaded model file that isn't running now. The running model stays.", button: "Delete them") {
                                Task { await mgmt.run("clean", params: ["models": true], title: "Free up space (models too)") }
                            }
                        }
                    }.disabled(mgmt.busyJob != nil)
                } else {
                    ProgressView("Checking model storage…")
                }
            }.frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var switchSheet: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(mgmt.split.isNull ? "Run this model across your machines" : "Switch the AI model").font(.headline)
            Text(switchTarget ?? "").font(.callout.monospaced())
            if !mgmt.split.isNull {
                Text("First, \(mgmt.split["alias"].string ?? "the running model") is unloaded: its servers stop and every machine gets its memory back. Its file stays on disk unless you tick the box.")
                    .font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                Toggle("Also delete its file and weight caches from every machine", isOn: $purgeOld)
            }
            Stepper("Context length: \(switchContext.formatted()) tokens", value: $switchContext, in: 512...131072, step: 512)
            HStack {
                Spacer()
                Button("Cancel") { switchTarget = nil }
                Button(mgmt.split.isNull ? "Run it" : "Switch") {
                    let file = switchTarget ?? ""
                    let action = mgmt.split.isNull ? "deploy" : "switch"
                    let params: [String: Any] = ["local": true, "file": file, "ctx": switchContext, "nodes": [String](), "keep_old": !purgeOld,
                                                 "alias": String(file.replacingOccurrences(of: ".gguf", with: "").lowercased().map { $0.isLetter || $0.isNumber || "._-".contains($0) ? $0 : "-" }.prefix(40))]
                    switchTarget = nil
                    Task { await mgmt.run(action, params: params, title: (action == "switch" ? "Switch to: " : "Run: ") + file) }
                }.keyboardShortcut(.defaultAction)
            }
        }.padding(20).frame(width: 520)
    }

    // MARK: Ollama

    private var ollamaCard: some View {
        Card("Ollama (one model per machine)") {
            VStack(alignment: .leading, spacing: 8) {
                let pods = mgmt.ollama?["pods"].array ?? []
                if pods.isEmpty { Text("Ollama isn't set up, or isn't answering. Set it up from the website's AI page.").foregroundStyle(.secondary) }
                ForEach(rows2(pods.flatMap { pod in pod["models"].array.map { m -> JSON in .object(["pod": pod["pod"], "node": pod["node"], "m": m]) } })) { r in
                    let m = r.v["m"], pod = r.v["pod"].text
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(m["name"].text).fontWeight(.medium)
                            Text("\(r.v["node"].text) · \(Format.bytes(m["size"].double))\(m["params"].text.isEmpty ? "" : " · \(m["params"].text)")").font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        if m["loaded"].bool == true {
                            StatusPill(text: "In memory · \(Format.bytes(m["memory"].double))", tone: .green)
                            Button("Unload") { Task { await mgmt.setOllama(pod: pod, model: m["name"].text, load: false) } }
                        } else {
                            Button("Load") { Task { await mgmt.setOllama(pod: pod, model: m["name"].text, load: true) } }
                        }
                        Button("Delete", role: .destructive) {
                            confirm = Confirm(title: "Delete \(m["name"].text)?", message: "It is unloaded, then deleted from every machine that runs Ollama. You can download it again later.", button: "Delete") {
                                Task { await mgmt.run("ollama-rm", params: ["name": m["name"].text], title: "Delete \(m["name"].text) from every Ollama node") }
                            }
                        }.disabled(mgmt.busyJob != nil)
                    }
                }
                ForEach(rows2(pods.filter { !$0["error"].text.isEmpty })) { p in Text("\(p.v["node"].text): \(p.v["error"].text)").font(.caption).foregroundStyle(.orange) }
            }.frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func rows2(_ items: [JSON]) -> [Row] { items.enumerated().map { Row(id: "\($0.offset)-\($0.element.hashValue)", v: $0.element) } }
}

/// Automatic model unloading: the same server setting as the website's Models tab.
struct LifecycleCard: View {
    @EnvironmentObject private var mgmt: ManagementState
    @State private var custom = false
    @State private var customMinutes = 30

    var body: some View {
        Card {
            VStack(alignment: .leading, spacing: 10) {
                if let life = mgmt.lifecycle {
                    let settings = life["settings"], enabled = settings["enabled"].bool ?? false, idle = settings["idle_seconds"].int ?? 1800
                    let presets = life["presets"].array.compactMap(\.int)
                    Toggle(isOn: Binding(get: { enabled }, set: { value in Task { await mgmt.saveLifecycle(enabled: value) } })) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text("Automatically unload idle models").fontWeight(.medium)
                            Text("Automatically unload models from memory after they have not been used for a specified period.").font(.caption).foregroundStyle(.secondary)
                        }
                    }.disabled(mgmt.lifecycleSaving)
                    HStack {
                        Picker("Unload after", selection: Binding(get: { custom || !presets.contains(idle) ? -1 : idle }, set: { value in
                            if value == -1 { custom = true; customMinutes = max(1, idle / 60) } else { custom = false; Task { await mgmt.saveLifecycle(idleSeconds: value) } }
                        })) {
                            ForEach(presets, id: \.self) { Text(Format.duration(Double($0))).tag($0) }
                            Text("Custom…").tag(-1)
                        }.frame(width: 240).disabled(mgmt.lifecycleSaving)
                        if custom || !presets.contains(idle) {
                            Stepper("\(customMinutes) min", value: $customMinutes, in: 1...10080).frame(width: 130)
                            Button("Apply") { Task { await mgmt.saveLifecycle(idleSeconds: customMinutes * 60); custom = false } }.disabled(mgmt.lifecycleSaving)
                        }
                        Spacer()
                        Text("Now: \(Format.duration(Double(idle)))\(enabled ? "" : " (off)")").font(.caption).foregroundStyle(.secondary)
                    }
                    if !life["split"].isNull { Text(splitText(life["split"], enabled: enabled)).font(.callout).foregroundStyle(life["split"]["state"].text == "unload_failed" || life["split"]["state"].text == "activity_unknown" ? .orange : .secondary).fixedSize(horizontal: false, vertical: true) }
                    ForEach(life["ollama"].array.indices, id: \.self) { i in
                        let m = life["ollama"][i]
                        Text("\(m["model"].text) on \(m["node"].text): " + (m["busy"].bool == true ? "answering now" : enabled ? "unloads after \(Format.duration(Double(idle))) idle" : m["pinned"].bool == true ? "stays loaded (loaded from Nodeyard)" : "Ollama's own timer"))
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Text("Never unloads a model that is answering or has queued work, and never loads one back by itself. The website shows the same setting.").font(.caption2).foregroundStyle(.tertiary)
                } else if let error = mgmt.lifecycleError {
                    Text("Couldn't read this setting: \(error)").foregroundStyle(.orange)
                } else { ProgressView() }
                if let error = mgmt.lifecycleError, mgmt.lifecycle != nil { Text("Not saved: \(error)").font(.caption).foregroundStyle(.red) }
            }.frame(maxWidth: .infinity, alignment: .leading)
        } label: {
            HStack { Text("Automatic model unloading"); if mgmt.lifecycleSaving { ProgressView().controlSize(.small) } }
        }
    }

    private func splitText(_ s: JSON, enabled: Bool) -> String {
        let name = s["alias"].string ?? s["model"].text
        switch s["state"].text {
        case "processing": return "\(name) is answering a request now, so it stays loaded."
        case "idle": return enabled ? "\(name) has been idle for \(Format.duration(s["idle_for"].double)); it unloads in \(Format.duration(s["unload_in"].double)) unless it is used." : "\(name) has been idle for \(Format.duration(s["idle_for"].double))."
        case "unload_pending": return "\(name) is past its idle time and unloads at the next check."
        case "unloading": return "Unloading \(name) now…"
        case "auto_unloaded": return "\(name) was unloaded automatically after being idle."
        case "unloaded": return "\(name) is unloaded."
        case "loading": return "\(name) is loading; idle time starts when it is ready."
        case "unload_failed": return "The last automatic unload failed: \(s["error"].text)"
        case "activity_unknown": return "Nodeyard can't see whether \(name) is busy (\(s["activity_error"].text)), so it never unloads it automatically."
        default: return "Checking whether \(name) is busy…"
        }
    }
}
