import SwiftUI

struct AITarget: Decodable, Identifiable, Hashable {
    var id: String
    var kind: String?
    var name: String?
    var model: String?
    var detail: String?
    var ready: Bool?
}

struct ChatMessage: Identifiable, Equatable {
    let id = UUID()
    var role: String            // "user" or "assistant"
    var text: String
    var thinking = ""
    var failed = false
}

/// A chat with one of the cluster's models, answered word by word.
@MainActor
final class ChatModel: ObservableObject {
    @Published var targets: [AITarget] = []
    @Published var selected: String = UserDefaults.standard.string(forKey: "nodeyard.aiModel") ?? "" {
        didSet { UserDefaults.standard.set(selected, forKey: "nodeyard.aiModel") }
    }
    @Published var messages: [ChatMessage] = []
    @Published var streaming = false
    @Published var problem: String?
    @Published var loaded = false
    var api: NodeyardAPI?
    private var task: Task<Void, Never>?

    private struct TargetsReply: Decodable { var targets: [AITarget]? }

    var current: AITarget? { targets.first { $0.id == selected } }

    func loadTargets() async {
        guard let api else { return }
        do {
            targets = (try await api.get("/api/v1/targets", as: TargetsReply.self).targets ?? []).filter { $0.ready != false }
            if current == nil { selected = targets.first?.id ?? "" }
            problem = nil
        } catch {
            problem = error.localizedDescription
        }
        loaded = true
    }

    func send(_ text: String) {
        let text = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !streaming, let api else { return }
        guard !selected.isEmpty else { problem = "No model is ready. Start one in the dashboard's AI page."; return }
        messages.append(ChatMessage(role: "user", text: text))
        messages.append(ChatMessage(role: "assistant", text: ""))
        streaming = true
        problem = nil
        Haptics.tap()
        let history = messages.dropLast().filter { !$0.failed }.map { ["role": $0.role, "content": $0.text] }
        let model = selected
        task = Task {
            defer { streaming = false; task = nil }
            do {
                let bytes = try await api.streamLines("/api/v1/chat/completions", body: ["model": model, "messages": history, "stream": true])
                for try await line in bytes.lines {
                    if Task.isCancelled { break }
                    guard line.hasPrefix("data:") else { continue }
                    let chunk = line.dropFirst(5).trimmingCharacters(in: .whitespaces)
                    if chunk == "[DONE]" { break }
                    guard let json = try? JSONSerialization.jsonObject(with: Data(chunk.utf8)) as? [String: Any] else { continue }
                    if let err = json["error"] {
                        let msg = (err as? [String: Any])?["message"] as? String ?? (err as? String) ?? "The model reported an error."
                        throw APIError(message: msg)
                    }
                    for choice in json["choices"] as? [[String: Any]] ?? [] {
                        let delta = choice["delta"] as? [String: Any] ?? [:]
                        if let r = (delta["reasoning_content"] ?? delta["reasoning"]) as? String, !r.isEmpty { append(thinking: r) }
                        if let c = delta["content"] as? String, !c.isEmpty { append(text: c) }
                    }
                }
                if messages.last?.text.isEmpty == true && messages.last?.thinking.isEmpty == true {
                    messages[messages.count - 1].text = "(no answer)"
                }
            } catch is CancellationError {
            } catch {
                if !Task.isCancelled {
                    messages[messages.count - 1].failed = true
                    if messages[messages.count - 1].text.isEmpty { messages[messages.count - 1].text = error.localizedDescription }
                    Haptics.error()
                }
            }
        }
    }

    func stop() { task?.cancel() }

    func reset() {
        stop()
        messages = []
        problem = nil
    }

    private func append(text: String) {
        guard !messages.isEmpty else { return }
        messages[messages.count - 1].text += text
    }
    private func append(thinking: String) {
        guard !messages.isEmpty else { return }
        messages[messages.count - 1].thinking += thinking
    }
}

struct ChatView: View {
    @EnvironmentObject private var model: AppModel
    @StateObject private var chat = ChatModel()
    @State private var draft = ""
    @FocusState private var typing: Bool

    var body: some View {
        NavigationStack {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 14) {
                        if chat.messages.isEmpty { welcome }
                        ForEach(chat.messages) { m in
                            Bubble(message: m, live: chat.streaming && m.id == chat.messages.last?.id).id(m.id)
                        }
                        if let p = chat.problem { ErrorBanner(text: p) }
                        Color.clear.frame(height: 1).id("bottom")
                    }
                    .padding(16)
                }
                .scrollDismissesKeyboard(.interactively)
                .onChange(of: chat.messages) { _ in proxy.scrollTo("bottom", anchor: .bottom) }
            }
            .safeAreaInset(edge: .bottom) { composer }
            .screen()
            .navigationTitle("AI")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .principal) { modelMenu }
                ToolbarItem(placement: .navigationBarTrailing) {
                    Button { chat.reset(); Haptics.tap() } label: { Image(systemName: "square.and.pencil") }
                        .disabled(chat.messages.isEmpty)
                        .accessibilityLabel("New chat")
                }
            }
        }
        .task {
            chat.api = model.api
            if !chat.loaded { await chat.loadTargets() }
        }
    }

    private var welcome: some View {
        VStack(spacing: 14) {
            NodeTile(size: 64)
            Text("Ask your cluster's AI").font(Theme.title(22))
            Text(chat.current.map { "Running \($0.name ?? $0.id) · \($0.detail ?? "")" } ?? (chat.loaded ? "No model is ready yet." : "Finding models…"))
                .font(.system(size: 14, design: .rounded)).foregroundStyle(Theme.mist).multilineTextAlignment(.center)
            VStack(spacing: 8) {
                ForEach(["Why might a pod keep restarting?", "Write a k3s Deployment for nginx", "Explain this cluster's node roles"], id: \.self) { s in
                    Button { chat.send(s) } label: {
                        Text(s).font(.system(size: 14, design: .rounded)).frame(maxWidth: .infinity, alignment: .leading)
                            .padding(12)
                            .background(Theme.panel, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                            .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).stroke(Theme.line))
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.top, 8)
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 40)
    }

    private var modelMenu: some View {
        Menu {
            Picker("Model", selection: $chat.selected) {
                ForEach(chat.targets) { t in
                    Text("\(t.name ?? t.id) — \(t.detail ?? "")").tag(t.id)
                }
            }
            Button { Task { await chat.loadTargets() } } label: { Label("Refresh", systemImage: "arrow.clockwise") }
        } label: {
            HStack(spacing: 4) {
                Text(chat.current?.name ?? "Choose model").font(.system(size: 16, weight: .semibold, design: .rounded)).lineLimit(1)
                Image(systemName: "chevron.down").font(.system(size: 11, weight: .bold))
            }
            .foregroundStyle(.white)
        }
    }

    private var composer: some View {
        HStack(alignment: .bottom, spacing: 10) {
            TextField("Message", text: $draft, axis: .vertical)
                .lineLimit(1...6)
                .focused($typing)
                .padding(.horizontal, 14)
                .padding(.vertical, 11)
                .background(Theme.panel, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 20, style: .continuous).stroke(typing ? Theme.indigo : Theme.line))
            Button {
                if chat.streaming { chat.stop() } else { chat.send(draft); draft = "" }
            } label: {
                Image(systemName: chat.streaming ? "stop.fill" : "arrow.up")
                    .font(.system(size: 17, weight: .bold))
                    .foregroundStyle(.white)
                    .frame(width: 42, height: 42)
                    .background(Theme.brand, in: Circle())
            }
            .disabled(!chat.streaming && draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            .accessibilityLabel(chat.streaming ? "Stop" : "Send")
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(.ultraThinMaterial)
    }
}

private struct Bubble: View {
    let message: ChatMessage
    let live: Bool
    @State private var showThinking = false

    var body: some View {
        if message.role == "user" {
            HStack {
                Spacer(minLength: 48)
                Text(message.text)
                    .font(.system(size: 16, design: .rounded))
                    .padding(.horizontal, 14).padding(.vertical, 10)
                    .background(Theme.brand, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
                    .textSelection(.enabled)
            }
        } else {
            VStack(alignment: .leading, spacing: 8) {
                if !message.thinking.isEmpty {
                    Button { withAnimation { showThinking.toggle() } } label: {
                        Label(live && message.text.isEmpty ? "Thinking…" : "Thought process", systemImage: "brain")
                            .font(.system(size: 13, weight: .medium, design: .rounded)).foregroundStyle(Theme.mist)
                    }
                    .buttonStyle(.plain)
                    if showThinking {
                        Text(message.thinking).font(.system(size: 13)).foregroundStyle(Theme.mist)
                            .padding(10).background(Color.white.opacity(0.04), in: RoundedRectangle(cornerRadius: 12))
                    }
                }
                if message.text.isEmpty && live && message.thinking.isEmpty {
                    TypingDots()
                } else {
                    Text(Bubble.markdown(message.text))
                        .font(.system(size: 16))
                        .foregroundStyle(message.failed ? Theme.danger : .white)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                if !live && !message.text.isEmpty && !message.failed {
                    Button { UIPasteboard.general.string = message.text; Haptics.success() } label: {
                        Image(systemName: "doc.on.doc").font(.system(size: 13)).foregroundStyle(Theme.mist)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Copy answer")
                }
            }
        }
    }

    static func markdown(_ s: String) -> AttributedString {
        (try? AttributedString(markdown: s, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))) ?? AttributedString(s)
    }
}

private struct TypingDots: View {
    @State private var on = false
    var body: some View {
        HStack(spacing: 5) {
            ForEach(0..<3) { i in
                Circle().fill(Theme.mist).frame(width: 7, height: 7)
                    .opacity(on ? 1 : 0.25)
                    .animation(.easeInOut(duration: 0.6).repeatForever().delay(Double(i) * 0.2), value: on)
            }
        }
        .padding(.vertical, 6)
        .onAppear { on = true }
    }
}
