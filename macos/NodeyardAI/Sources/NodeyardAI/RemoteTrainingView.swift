import AppKit
import SwiftUI

// Remote training: runs the bundled nodeyard job runner (jobs/nyjobs.py) with the system Python.
// The runner needs only the standard library; it reaches the machines over SSH (key-only), using
// the hosts in ~/.config/nodeyard/jobs.json. Output streams into the log below.
@MainActor
final class RemoteTrainingModel: ObservableObject {
    @Published var log = ""
    @Published var running = false
    @Published var dataPath = ""
    @Published var copies = 2
    @Published var steps = 3000
    @Published var assistantOnly = false
    private var process: Process?

    static var runnerURL: URL? {
        Bundle.main.resourceURL?.appendingPathComponent("jobs/nyjobs.py")
    }

    static var hasRunner: Bool {
        guard let url = runnerURL else { return false }
        return FileManager.default.fileExists(atPath: url.path)
    }

    func chooseDataFile() {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        panel.message = "Choose a UTF-8 text file to train on (plain text or User:/Assistant: lines)."
        if panel.runModal() == .OK, let url = panel.url {
            dataPath = url.path
        }
    }

    func probe() { run(["hosts"]) }
    func checkMaster() { run(["probe"]) }
    func plan() { run(["plan", "--copies", String(copies)]) }

    func startTraining() {
        guard !dataPath.isEmpty else {
            append("Choose a training file first.\n")
            return
        }
        let job = "app-" + ISO8601DateFormatter().string(from: Date()).replacingOccurrences(of: ":", with: "-")
        var arguments = ["run", "--data", dataPath, "--copies", String(copies), "--steps", String(steps), "--job", job]
        if assistantOnly {
            arguments.append("--assistant-only")
        }
        run(arguments)
    }

    func stop() {
        process?.terminate()
        append("Stop requested. Copies already running on the machines keep going until they finish.\n")
    }

    private func run(_ arguments: [String]) {
        guard !running else {
            append("A job is already running.\n")
            return
        }
        guard let runner = Self.runnerURL, FileManager.default.fileExists(atPath: runner.path) else {
            append("The job runner is missing from this app. Rebuild Nodeyard AI with scripts/build-macos-ai-app.sh.\n")
            return
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = [runner.path] + arguments
        var environment = ProcessInfo.processInfo.environment
        environment["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"
        process.environment = environment
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty {
                handle.readabilityHandler = nil
                return
            }
            let text = String(decoding: data, as: UTF8.self)
            Task { @MainActor in self?.append(text) }
        }
        process.terminationHandler = { [weak self] finished in
            Task { @MainActor in
                self?.running = false
                self?.process = nil
                self?.append("\nFinished with exit code \(finished.terminationStatus).\n")
            }
        }
        do {
            try process.run()
            self.process = process
            running = true
            append("$ nyjobs \(arguments.joined(separator: " "))\n")
        } catch {
            append("Could not start the job runner: \(error.localizedDescription)\n")
        }
    }

    private func append(_ text: String) {
        log += text
        if log.count > 200_000 {
            log = String(log.suffix(150_000))
        }
    }
}

struct RemoteTrainingView: View {
    @StateObject private var model = RemoteTrainingModel()

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Remote training")
                .font(.title2.bold())
            Text("Trains copies of one model on the machines in your nodeyard tailnet and keeps the copy with the lowest validation loss. Machines are set up with nodeyard jobs add-host and the master with nodeyard jobs access setup.")
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            if !RemoteTrainingModel.hasRunner {
                Text("The job runner is missing from this build. Rebuild Nodeyard AI from the repository.")
                    .foregroundStyle(.red)
            }

            GroupBox("Training") {
                VStack(alignment: .leading, spacing: 10) {
                    HStack {
                        TextField("Training file", text: $model.dataPath)
                            .textFieldStyle(.roundedBorder)
                        Button("Choose…") { model.chooseDataFile() }
                    }
                    Stepper("Copies at once: \(model.copies)", value: $model.copies, in: 1...10)
                    Stepper("Steps per copy: \(model.steps)", value: $model.steps, in: 100...200_000, step: 100)
                    Toggle("Learn only from assistant replies (User:/Assistant: text)", isOn: $model.assistantOnly)
                }
                .padding(6)
            }

            HStack {
                Button("Check machines") { model.checkMaster() }
                Button("List hosts") { model.probe() }
                Button("Plan placement") { model.plan() }
                Spacer()
                if model.running {
                    Button("Stop", role: .destructive) { model.stop() }
                } else {
                    Button("Start training") { model.startTraining() }
                        .buttonStyle(.borderedProminent)
                        .disabled(!RemoteTrainingModel.hasRunner)
                }
            }

            GroupBox("Output") {
                ScrollView {
                    Text(model.log.isEmpty ? "Output appears here." : model.log)
                        .font(.system(.caption, design: .monospaced))
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .textSelection(.enabled)
                        .padding(6)
                }
                .frame(minHeight: 220)
            }
        }
        .padding(20)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}
