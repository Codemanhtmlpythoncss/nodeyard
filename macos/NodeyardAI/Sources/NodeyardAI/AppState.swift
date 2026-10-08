import Combine
import Foundation
import UniformTypeIdentifiers
import UserNotifications

@MainActor
final class AppState: ObservableObject {
    @Published var chats: [ChatRecord]
    @Published var currentChatID: String? {
        didSet { if let currentChatID { UserDefaults.standard.set(currentChatID, forKey: "nodeyard.lastChat") } }
    }
    @Published var targets: [ModelTarget] = []
    @Published var inventory: ModelInventory?
    @Published var remoteChats: [RemoteChatSummary] = []
    @Published var composer = ""
    @Published var stagedAttachments: [AppAttachment] = []
    @Published var isSending = false
    @Published var isConnected = false
    @Published var isRefreshing = false
    @Published var isSyncing = false
    @Published var searchText = ""
    @Published var showArchived = false
    @Published var error: String?
    @Published var info: String?
    @Published var defaults: AppDefaults
    @Published var serverAddress: String
    @Published var apiKey: String
    @Published var editingMessageID: String?
    @Published var deletedForUndo: ChatRecord?
    @Published var selectedRemote: RemoteChatSummary?
    @Published var showSettings = false
    @Published var showImport = false
    @Published var showExport = false
    @Published var exportDocument = ChatExportDocument(data: Data())
    @Published var exportType: UTType = .plainText
    @Published var exportFilename = "chat.md"
    @Published var renameChatID: String?
    @Published var renameDraft = ""

    let store = ChatStore()
    private var client: NodeyardClient
    private var activeTask: Task<Void, Never>?
    private var streamStarted: Date?

    init() {
        defaults = store.loadDefaults()
        let address = UserDefaults.standard.string(forKey: "nodeyard.serverAddress") ?? "http://localhost:9092"
        let key = KeychainStore.read()
        serverAddress = address
        apiKey = key
        client = NodeyardClient(baseAddress: address, apiKey: key)
        chats = store.loadChats()
        let last = UserDefaults.standard.string(forKey: "nodeyard.lastChat")
        currentChatID = chats.first(where: { $0.id == last && !$0.archived })?.id ?? chats.first(where: { !$0.archived })?.id
        if chats.isEmpty { createChat() }
    }

    var currentChat: ChatRecord? { chats.first { $0.id == currentChatID } }
    var filteredChats: [ChatRecord] {
        chats.filter { $0.archived == showArchived && (searchText.isEmpty || $0.title.localizedCaseInsensitiveContains(searchText) || $0.messages.contains { $0.content.localizedCaseInsensitiveContains(searchText) }) }
            .sorted { a, b in a.pinned == b.pinned ? a.updated > b.updated : a.pinned }
    }
    var readyTargets: [ModelTarget] { targets.filter(\.ready) }

    func createChat() {
        let preferred = readyTargets.first
        let chat = ChatRecord.fresh(model: preferred, defaults: defaults)
        try? store.save(chat)
        chats.insert(chat, at: 0)
        currentChatID = chat.id
        composer = ""
        stagedAttachments = []
        _ = chat
    }

    func select(_ chat: ChatRecord) { currentChatID = chat.id; composer = ""; stagedAttachments = [] }

    func persistCurrent() {
        guard let index = chats.firstIndex(where: { $0.id == currentChatID }) else { return }
        chats[index].updated = Date()
        do { try store.save(chats[index]) }
        catch { self.error = "Couldn't save this chat locally: \(error.localizedDescription)" }
    }

    func rename(_ chat: ChatRecord, to title: String) {
        guard let i = chats.firstIndex(where: { $0.id == chat.id }) else { return }
        chats[i].title = title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "New chat" : String(title.prefix(120))
        persist(chatIndex: i)
    }

    func beginRename(_ chat: ChatRecord) { renameChatID = chat.id; renameDraft = chat.title }
    func commitRename() {
        guard let chat = chats.first(where: { $0.id == renameChatID }) else { return }
        rename(chat, to: renameDraft); renameChatID = nil
    }

    func togglePinned(_ chat: ChatRecord) { update(chat) { $0.pinned.toggle() } }
    func toggleArchived(_ chat: ChatRecord) { update(chat) { $0.archived.toggle() }; if currentChatID == chat.id { currentChatID = chats.first(where: { !$0.archived })?.id } }

    func duplicate(_ chat: ChatRecord) {
        var copy = chat; copy.id = "mac-" + UUID().uuidString.lowercased(); copy.title += " copy"; copy.created = Date(); copy.updated = Date()
        chats.insert(copy, at: 0); try? store.save(copy); currentChatID = copy.id
    }

    func delete(_ chat: ChatRecord) {
        deletedForUndo = chat; store.remove(chat); chats.removeAll { $0.id == chat.id }
        if currentChatID == chat.id { currentChatID = chats.first(where: { !$0.archived })?.id }
        if currentChatID == nil { createChat() }
        info = "Chat deleted. Use Undo to restore it."
    }

    func undoDelete() {
        guard let chat = deletedForUndo else { return }
        deletedForUndo = nil; chats.insert(chat, at: 0); try? store.save(chat); currentChatID = chat.id
    }

    func setServerAddress(_ address: String) {
        serverAddress = address.trimmingCharacters(in: .whitespacesAndNewlines).trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        UserDefaults.standard.set(serverAddress, forKey: "nodeyard.serverAddress")
        reconnectClient()
    }

    func setAPIKey(_ value: String) {
        do { try KeychainStore.write(value); apiKey = value; reconnectClient() }
        catch { self.error = "Couldn't save the server key in Keychain: \(error.localizedDescription)" }
    }

    func saveDefaults() {
        defaults.temperature = min(2, max(0, defaults.temperature))
        defaults.fontSize = min(22, max(12, defaults.fontSize))
        defaults.contextLength = min(131072, max(512, defaults.contextLength))
        do { try store.saveDefaults(defaults); info = "Your defaults are saved on this Mac." }
        catch { self.error = error.localizedDescription }
    }

    func refresh() {
        guard !isRefreshing else { return }
        isRefreshing = true
        Task {
            defer { isRefreshing = false }
            do {
                targets = try await client.targets()
                inventory = try await client.models()
                isConnected = true; error = nil
                if let current = currentChat, current.modelTarget.isEmpty, let target = readyTargets.first { update(current) { $0.modelTarget = target.id; $0.modelName = target.name } }
            } catch { isConnected = false; self.error = error.localizedDescription }
        }
    }

    func loadModel(_ row: ModelInventoryRow) {
        Task {
            do {
                try await client.loadModel(row, context: currentChat?.contextLength ?? defaults.contextLength)
                info = "Starting \(row.name). Refreshing status will show when it is ready."
                try await Task.sleep(for: .seconds(1)); refresh()
            } catch { self.error = error.localizedDescription }
        }
    }

    func beginSend(retry: Bool = false) {
        guard !isSending, let chatID = currentChatID else { return }
        activeTask = Task { await performSend(chatID: chatID, retry: retry) }
    }

    func stop() { activeTask?.cancel(); activeTask = nil; isSending = false; info = "Generation stopped." }

    private func performSend(chatID: String, retry: Bool) async {
        guard let chatIndex = chats.firstIndex(where: { $0.id == chatID }) else { return }
        let text = composer.trimmingCharacters(in: .whitespacesAndNewlines)
        let editing = editingMessageID
        if !retry && text.isEmpty && stagedAttachments.isEmpty { return }
        guard !currentTargetIsReady(chats[chatIndex]) else { return }
        isSending = true; error = nil; streamStarted = Date()
        defer { isSending = false; activeTask = nil }
        if !retry {
            if let editing, let i = chats[chatIndex].messages.firstIndex(where: { $0.id == editing }) {
                chats[chatIndex].messages = Array(chats[chatIndex].messages[..<i])
                editingMessageID = nil
            }
            let user = ChatMessage(role: "user", content: text, attachments: stagedAttachments)
            chats[chatIndex].messages.append(user)
            if chats[chatIndex].title == "New chat" { chats[chatIndex].title = String((text.isEmpty ? stagedAttachments.first?.name ?? "New chat" : text).prefix(72)) }
            composer = ""; stagedAttachments = []
            _ = user
        } else if chats[chatIndex].messages.last?.role == "assistant" { chats[chatIndex].messages.removeLast() }
        var response = ChatMessage(role: "assistant", content: "")
        chats[chatIndex].messages.append(response)
        persist(chatIndex: chatIndex)
        guard let target = chats[chatIndex].modelTarget.nilIfEmpty else {
            error = "Choose a ready model in the model menu first."; chats[chatIndex].messages.removeLast(); persist(chatIndex: chatIndex); return
        }
        do {
            let messages = try openAIMessages(for: chats[chatIndex])
            let stats = try await client.streamChat(model: target, messages: messages, temperature: chats[chatIndex].temperature,
                                                    maxTokens: chats[chatIndex].maxTokens, onContent: { [weak self] piece in
                self?.appendStream(piece, reasoning: false, to: response.id, chatID: chatID)
            }, onReasoning: { [weak self] piece in
                self?.appendStream(piece, reasoning: true, to: response.id, chatID: chatID)
            })
            if let i = chats[chatIndex].messages.firstIndex(where: { $0.id == response.id }) {
                chats[chatIndex].messages[i].duration = Date().timeIntervalSince(streamStarted ?? Date())
                chats[chatIndex].messages[i].tokensPerSecond = stats.tokensPerSecond
                chats[chatIndex].messages[i].tokenCount = stats.tokens
                response = chats[chatIndex].messages[i]
            }
            persist(chatIndex: chatIndex)
            if defaults.syncChats { await sync(chatID: chatID) }
            if defaults.notifications { notify(title: "Nodeyard AI replied", body: chats[chatIndex].title) }
        } catch is CancellationError {
            info = "Generation stopped. The partial answer is saved."
            persist(chatIndex: chatIndex)
        } catch {
            self.error = error.localizedDescription
            if chats[chatIndex].messages.last?.id == response.id && chats[chatIndex].messages.last?.content.isEmpty == true { chats[chatIndex].messages.removeLast() }
            persist(chatIndex: chatIndex)
        }
    }

    private func currentTargetIsReady(_ chat: ChatRecord) -> Bool {
        guard let target = targets.first(where: { $0.id == chat.modelTarget }), target.ready else {
            error = "That model is not ready. Choose a ready model or start one in Models."; return true
        }
        return false
    }

    private func openAIMessages(for chat: ChatRecord) throws -> [OpenAIMessage] {
        var result: [OpenAIMessage] = []
        if !chat.systemPrompt.isEmpty { result.append(OpenAIMessage(role: "system", content: .text(chat.systemPrompt))) }
        for message in chat.messages {
            if message.role != "user" || message.attachments.isEmpty {
                result.append(OpenAIMessage(role: message.role, content: .text(message.content))); continue
            }
            var parts: [EncodableContent.Part] = []
            if !message.content.isEmpty { parts.append(.init(type: "text", text: message.content)) }
            for attachment in message.attachments {
                if attachment.mime.hasPrefix("image/"), let data = store.attachmentData(attachment) {
                    let url = "data:\(attachment.mime);base64,\(data.base64EncodedString())"
                    parts.append(.init(type: "image_url", image_url: .init(url: url)))
                } else if !attachment.extractedText.isEmpty {
                    parts.append(.init(type: "text", text: "\n<file name=\"\(attachment.name)\">\n\(attachment.extractedText)\n</file>"))
                }
            }
            result.append(OpenAIMessage(role: "user", content: .parts(parts)))
        }
        return result
    }

    private func appendStream(_ piece: String, reasoning: Bool, to messageID: String, chatID: String) {
        guard let ci = chats.firstIndex(where: { $0.id == chatID }), let mi = chats[ci].messages.firstIndex(where: { $0.id == messageID }) else { return }
        if reasoning { chats[ci].messages[mi].reasoning += piece } else { chats[ci].messages[mi].content += piece }
        if chats[ci].messages[mi].content.count % 160 == 0 { try? store.save(chats[ci]) }
    }

    func edit(_ message: ChatMessage) { composer = message.content; stagedAttachments = message.attachments; editingMessageID = message.id }

    func regenerate() {
        guard let chat = currentChat, chat.messages.last?.role == "assistant" else { return }
        beginSend(retry: true)
    }

    func stageFiles(_ urls: [URL]) {
        guard stagedAttachments.count + urls.count <= 8 else { error = "Attach up to eight files to one message."; return }
        do {
            for url in urls {
                let item = try store.copyAttachment(from: url)
                let currentBytes = stagedAttachments.compactMap(store.attachmentData).reduce(0) { $0 + $1.count }
                guard currentBytes + (store.attachmentData(item)?.count ?? 0) <= 8 * 1024 * 1024 else { error = "Attachments total more than 8 MiB."; return }
                stagedAttachments.append(item)
            }
        } catch { self.error = error.localizedDescription }
    }

    func removeAttachment(_ attachment: AppAttachment) { stagedAttachments.removeAll { $0.id == attachment.id } }

    func stageImageData(_ data: Data) {
        do {
            guard data.count <= 8 * 1024 * 1024, stagedAttachments.count < 8 else { throw NodeyardError.server("The clipboard image exceeds the attachment limit.") }
            let id = UUID().uuidString.lowercased(), name = "Pasted image.png"
            try data.write(to: store.attachmentsDirectory.appendingPathComponent(id + ".png"), options: .atomic)
            stagedAttachments.append(AppAttachment(id: id, name: name, mime: "image/png", relativePath: id + ".png"))
        } catch { self.error = error.localizedDescription }
    }

    func updateCurrentChat(_ update: (inout ChatRecord) -> Void) {
        guard let i = chats.firstIndex(where: { $0.id == currentChatID }) else { return }
        update(&chats[i]); persist(chatIndex: i)
    }

    func chooseTarget(_ target: ModelTarget) {
        updateCurrentChat { $0.modelTarget = target.id; $0.modelName = target.name }
    }

    func deleteRemote(_ chat: RemoteChatSummary) {
        Task {
            do { try await client.deleteRemote(chat.id); remoteChats.removeAll { $0.id == chat.id }; info = "Deleted the server-saved chat." }
            catch { self.error = error.localizedDescription }
        }
    }

    func syncNow() {
        guard !isSyncing else { return }
        isSyncing = true
        Task {
            defer { isSyncing = false }
            do { remoteChats = try await client.remoteChats(); isConnected = true; info = "Found \(remoteChats.count) server-saved chats." }
            catch { self.error = error.localizedDescription }
        }
    }

    func importRemote(_ item: RemoteChatSummary) {
        Task {
            do {
                let remote = try await client.remoteChat(item.id).chat
                var chat = ChatRecord.fresh(model: readyTargets.first, defaults: defaults)
                chat.title = remote.title; chat.modelName = remote.model
                chat.messages = remote.messages.map { ChatMessage(role: $0.role, content: $0.content) }
                chats.insert(chat, at: 0); try store.save(chat); currentChatID = chat.id
                info = "Imported \(item.title)."
            } catch { self.error = error.localizedDescription }
        }
    }

    func exportMarkdown(_ chat: ChatRecord) {
        let body = chat.messages.map { message in
            let heading = message.role == "assistant" ? "## Assistant" : "## You"
            let attach = message.attachments.map { "\n_Attachment: \($0.name)_" }.joined()
            let thought = message.reasoning.isEmpty ? "" : "\n<details><summary>Model reasoning</summary>\n\n\(message.reasoning)\n</details>\n"
            return "\(heading)\n\n\(message.content)\(attach)\n\(thought)"
        }.joined(separator: "\n---\n\n")
        exportDocument = ChatExportDocument(data: Data(("# \(chat.title)\n\nModel: \(chat.modelName)\n\n" + body).utf8))
        exportType = .plainText; exportFilename = safeFilename(chat.title) + ".md"; showExport = true
    }

    func exportJSON(_ chat: ChatRecord) {
        do { exportDocument = ChatExportDocument(data: try store.exportData(chat)); exportType = .json; exportFilename = safeFilename(chat.title) + ".nodeyard-chat.json"; showExport = true }
        catch { self.error = error.localizedDescription }
    }

    func importJSON() { showImport = true }

    func importChat(from url: URL) {
        do { let chat = try store.importChat(from: url); chats.insert(chat, at: 0); currentChatID = chat.id; info = "Imported \(chat.title)." }
        catch { self.error = "Couldn't import that chat: \(error.localizedDescription)" }
    }

    private func sync(chatID: String) async {
        guard let chat = chats.first(where: { $0.id == chatID }) else { return }
        do { try await client.saveRemote(chat) } catch { info = "Saved on this Mac; server sync will retry after you reconnect." }
    }

    private func reconnectClient() { client = NodeyardClient(baseAddress: serverAddress, apiKey: apiKey) }
    private func persist(chatIndex i: Int) { chats[i].updated = Date(); do { try store.save(chats[i]) } catch { self.error = "Couldn't save chat: \(error.localizedDescription)" } }
    private func update(_ chat: ChatRecord, _ operation: (inout ChatRecord) -> Void) { guard let i = chats.firstIndex(where: { $0.id == chat.id }) else { return }; operation(&chats[i]); persist(chatIndex: i) }
    private func safeFilename(_ text: String) -> String { String(text.map { $0.isLetter || $0.isNumber || $0 == "-" || $0 == "_" ? $0 : "-" }.prefix(80)) }

    private func notify(title: String, body: String) {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { granted, _ in
            guard granted else { return }
            let notification = UNMutableNotificationContent(); notification.title = title; notification.body = body
            UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: UUID().uuidString, content: notification, trigger: nil))
        }
    }
}

private extension String { var nilIfEmpty: String? { isEmpty ? nil : self } }
