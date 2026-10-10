import Combine
import Foundation
import UniformTypeIdentifiers
import UserNotifications

@MainActor
final class AppState: ObservableObject {
    @Published var chats: [ChatRecord]
    @Published var currentChatID: String? {
        didSet { if let currentChatID, !TestMode.isOn { UserDefaults.standard.set(currentChatID, forKey: "nodeyard.lastChat") } }
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
    @Published var pendingToolApproval: ToolApproval?
    @Published var workspace: Workspace = .chat
    /// Bumped when the AI starts using the Agent Browser, so the window opens where the user can watch.
    @Published var browserRequested = 0
    /// How far the model has read the prompt, per answer still waiting for its first token (llama.cpp only).
    @Published var promptProgress: [String: PromptProgress] = [:]
    struct PromptProgress: Equatable { var processed: Int; var total: Int }

    enum Workspace: String, CaseIterable { case chat, models, manage }
    /// The website's management features (dashboard sign-in), sharing this app's address and key.
    let manage: ManagementState

    let store = ChatStore()
    private var client: NodeyardClient
    private var activeTask: Task<Void, Never>?
    private var streamStarted: Date?
    private var toolApprovalContinuation: CheckedContinuation<Bool, Never>?

    init() {
        defaults = store.loadDefaults()
        let savedAddress = TestMode.isOn ? TestMode.address : (UserDefaults.standard.string(forKey: "nodeyard.serverAddress") ?? "http://localhost:9092")
        let address = NodeyardClient.migratedDashboardAddress(from: savedAddress) ?? savedAddress
        if address != savedAddress && !TestMode.isOn {
            UserDefaults.standard.set(address, forKey: "nodeyard.serverAddress")
            info = "Updated the saved model endpoint to the Nodeyard dashboard on port 9092."
        }
        let key = KeychainStore.read()
        serverAddress = address
        apiKey = key
        client = NodeyardClient(baseAddress: address, apiKey: key)
        manage = ManagementState(address: address, apiKey: key)
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
        if !TestMode.isOn { UserDefaults.standard.set(serverAddress, forKey: "nodeyard.serverAddress") }
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

    func stop() {
        resolveToolApproval(allow: false)
        activeTask?.cancel(); activeTask = nil; isSending = false; info = "Generation stopped."
    }

    func requestComputerUsePermission() {
        let granted = MacComputerUse.requestPermission()
        info = granted ? "Computer use is allowed. Each computer action still asks you first." : "Allow Nodeyard AI under System Settings → Privacy & Security → Accessibility, then enable computer use."
    }

    func resolveToolApproval(allow: Bool) {
        let continuation = toolApprovalContinuation
        toolApprovalContinuation = nil
        pendingToolApproval = nil
        continuation?.resume(returning: allow)
    }

    private func performSend(chatID: String, retry: Bool) async {
        guard let chatIndex = chats.firstIndex(where: { $0.id == chatID }) else { return }   // (valid until the first await)
        // The chat list can change while an answer streams (a new chat goes to the top, another is deleted), so the
        // chat is looked up by its id every time instead of keeping its position. If it was deleted, stop quietly.
        func ci() throws -> Int {
            guard let i = chats.firstIndex(where: { $0.id == chatID }) else { throw CancellationError() }
            return i
        }
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
            var responseID = response.id
            var finalStats = StreamStats()
            var toolRounds = 0
            var actionCount = 0
            while true {
                try Task.checkCancellation()
                let snapshot = chats[try ci()]
                let messages = try openAIMessages(for: snapshot, excludingMessageID: responseID)
                let availableTools = toolRounds < 4 ? modelTools(for: snapshot) : []
                let activeResponseID = responseID
                let stats = try await client.streamChat(model: target, messages: messages, temperature: snapshot.temperature,
                                                        maxTokens: snapshot.maxTokens, tools: availableTools, onContent: { [weak self] piece in
                    self?.appendStream(piece, reasoning: false, to: activeResponseID, chatID: chatID)
                }, onReasoning: { [weak self] piece in
                    self?.appendStream(piece, reasoning: true, to: activeResponseID, chatID: chatID)
                }, onProgress: { [weak self] processed, total in
                    self?.promptProgress[activeResponseID] = PromptProgress(processed: processed, total: total)
                })
                promptProgress[activeResponseID] = nil
                finalStats = stats
                guard !stats.toolCalls.isEmpty else { break }
                guard !availableTools.isEmpty, toolRounds < 4, actionCount + stats.toolCalls.count <= 6 else {
                    appendStream("I stopped at the safe tool-use limit. Ask me to continue if you want another step.", reasoning: false, to: responseID, chatID: chatID)
                    break
                }
                if let i = chats[try ci()].messages.firstIndex(where: { $0.id == responseID }) {
                    chats[try ci()].messages[i].toolCalls = stats.toolCalls
                    if chats[try ci()].messages[i].content.isEmpty {
                        chats[try ci()].messages[i].content = "Using \(stats.toolCalls.map { $0.function.name.replacingOccurrences(of: "_", with: " ") }.joined(separator: ", "))…"
                    }
                }
                for call in stats.toolCalls {
                    try Task.checkCancellation()
                    let result = await executeTool(call)
                    chats[try ci()].messages.append(ChatMessage(role: "tool", content: result, toolCallID: call.id, toolName: call.function.name))
                }
                actionCount += stats.toolCalls.count
                toolRounds += 1
                response = ChatMessage(role: "assistant", content: "")
                responseID = response.id
                chats[try ci()].messages.append(response)
                persist(chatIndex: try ci())
            }
            if let i = chats[try ci()].messages.firstIndex(where: { $0.id == responseID }) {
                chats[try ci()].messages[i].duration = Date().timeIntervalSince(streamStarted ?? Date())
                chats[try ci()].messages[i].tokensPerSecond = finalStats.tokensPerSecond
                chats[try ci()].messages[i].tokenCount = finalStats.tokens
                response = chats[try ci()].messages[i]
            }
            persist(chatIndex: try ci())
            if defaults.syncChats { await sync(chatID: chatID) }
            if defaults.notifications { notify(title: "Nodeyard AI replied", body: chats[try ci()].title) }
        } catch is CancellationError {
            if let i = chats.firstIndex(where: { $0.id == chatID }) {
                info = "Generation stopped. The partial answer is saved."
                persist(chatIndex: i)
            }
        } catch {
            self.error = error.localizedDescription
            if let i = chats.firstIndex(where: { $0.id == chatID }) {
                if chats[i].messages.last?.role == "assistant" && chats[i].messages.last?.content.isEmpty == true && chats[i].messages.last?.toolCalls == nil { chats[i].messages.removeLast() }
                persist(chatIndex: i)
            }
        }
    }

    private func currentTargetIsReady(_ chat: ChatRecord) -> Bool {
        guard let target = targets.first(where: { $0.id == chat.modelTarget }), target.ready else {
            error = "That model is not ready. Choose a ready model or start one in Models."; return true
        }
        return false
    }

    private func openAIMessages(for chat: ChatRecord, excludingMessageID: String? = nil) throws -> [OpenAIMessage] {
        var result: [OpenAIMessage] = []
        if !chat.systemPrompt.isEmpty { result.append(OpenAIMessage(role: "system", content: .text(chat.systemPrompt))) }
        for message in chat.messages {
            // The empty assistant row is a UI placeholder while generation runs; including it
            // in the prompt can leave chat templates continuing from a completed assistant turn.
            if message.id == excludingMessageID { continue }
            if let calls = message.toolCalls {
                result.append(OpenAIMessage(role: "assistant", content: nil, tool_calls: calls))
                continue
            }
            if message.role == "tool" {
                result.append(OpenAIMessage(role: "tool", content: .text(message.content), tool_call_id: message.toolCallID, name: message.toolName))
                continue
            }
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

    private func modelTools(for chat: ChatRecord) -> [[String: Any]] {
        var result: [[String: Any]] = []
        if chat.browserUse ?? defaults.browserUse ?? true {
            result.append(tool("browser_search", "Search the public web through Nodeyard and return sourced results.", ["query": stringParameter("Search query")], required: ["query"]))
            result.append(tool("browser_open", "Read a public web page as text. Use a URL returned by browser_search.", ["url": stringParameter("Public HTTP or HTTPS URL")], required: ["url"]))
        }
        if chat.agentBrowser ?? defaults.agentBrowser ?? false {
            let element: [String: Any] = ["type": "integer", "description": "The element's number from page_read"]
            result.append(tool("page_open", "Open a web page in the Agent Browser on this Mac (a private window the user can watch). Use it for sites that need JavaScript or interaction; use browser_open for simply reading a public page.", ["url": stringParameter("http or https address")], required: ["url"]))
            result.append(tool("page_read", "Read the Agent Browser's current page: its text and every link, button and field, numbered for page_click and page_type.", [:], required: []))
            result.append(tool("page_click", "Click a numbered element from page_read. The user approves each click.", ["element": element], required: ["element"]))
            result.append(tool("page_type", "Type text into a numbered field from page_read, optionally submitting its form. Password fields are blocked. The user approves each entry.",
                               ["element": element, "text": stringParameter("Text to enter"), "submit": ["type": "boolean", "description": "Submit the form after typing"]], required: ["element", "text"]))
            result.append(tool("page_back", "Go back to the previous page in the Agent Browser.", [:], required: []))
            result.append(tool("page_console", "Read JavaScript errors and warnings the current page has logged (for debugging websites).", [:], required: []))
            result.append(tool("page_snapshot", "Take a picture of the current page and show it to the user in the Agent Browser window.", [:], required: []))
        }
        if (chat.computerUse ?? defaults.computerUse ?? false) && MacComputerUse.permissionGranted() {
            result.append(tool("computer_read_screen", "Read visible accessibility text in the frontmost Mac app. Password values are excluded.", [:], required: []))
            result.append(tool("computer_click", "Click one uniquely named button, link, or menu item in the frontmost Mac app. The user must approve each click.", ["label": stringParameter("Exact visible control label")], required: ["label"]))
            result.append(tool("computer_set_text", "Replace the focused regular text field. Password fields are blocked. The user must approve the text first.", ["text": stringParameter("Text to enter")], required: ["text"]))
            result.append(tool("computer_press_key", "Press one safe key: Return, Tab, Escape, Space, an arrow key, or Command+L. The user must approve each press.", ["key": stringParameter("Allowed key name")], required: ["key"]))
            result.append(tool("computer_open_app", "Open an allowed Mac app: Safari, Finder, TextEdit, Notes, Calendar, Calculator, Preview, Mail, Chrome, or Firefox. The user must approve.", ["app": stringParameter("Allowed app name")], required: ["app"]))
        }
        return result
    }

    private func stringParameter(_ description: String) -> [String: Any] {
        ["type": "string", "description": description]
    }

    private func tool(_ name: String, _ description: String, _ properties: [String: Any], required: [String]) -> [String: Any] {
        ["type": "function", "function": ["name": name, "description": description,
         "parameters": ["type": "object", "properties": properties, "required": required, "additionalProperties": false]]]
    }

    private func executeTool(_ call: ModelToolCall) async -> String {
        guard let data = call.function.arguments.data(using: .utf8),
              let args = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
            return "Tool error: arguments weren't valid JSON."
        }
        do {
            switch call.function.name {
            case "browser_search":
                guard let query = args["query"] as? String, !query.isEmpty else { return "Tool error: give me a search query." }
                return try await client.runWebTool("WebSearch", arguments: ["query": String(query.prefix(300))])
            case "browser_open":
                guard let url = args["url"] as? String, !url.isEmpty else { return "Tool error: give me a page URL." }
                return try await client.runWebTool("WebFetch", arguments: ["url": String(url.prefix(2000)), "max_chars": 12000])
            case "page_open":
                guard let url = args["url"] as? String, !url.isEmpty else { return "Tool error: give me a page address." }
                browserRequested += 1
                return try await AgentBrowser.shared.navigate(String(url.prefix(2000)))
            case "page_read":
                return try await AgentBrowser.shared.read()
            case "page_click":
                guard let n = (args["element"] as? Int) ?? Int(args["element"] as? String ?? "") else { return "Tool error: say which element number to click." }
                let what = try await AgentBrowser.shared.describe(n)
                guard await requestApproval(title: "Allow a click in the Agent Browser?", detail: "Nodeyard AI wants to click [\(n)] \(what) on \(AgentBrowser.shared.url).") else { return "The user declined this click." }
                return try await AgentBrowser.shared.click(n)
            case "page_type":
                guard let n = (args["element"] as? Int) ?? Int(args["element"] as? String ?? "") else { return "Tool error: say which field number to type into." }
                let text = String((args["text"] as? String ?? "").prefix(4000)), submit = args["submit"] as? Bool ?? false
                let what = try await AgentBrowser.shared.describe(n)
                guard await requestApproval(title: submit ? "Allow typing and submitting?" : "Allow typing in the Agent Browser?",
                                            detail: "Nodeyard AI wants to type into [\(n)] \(what) on \(AgentBrowser.shared.url)\(submit ? " and submit the form" : ""):\n\n\(text.prefix(300))") else { return "The user declined this text entry." }
                return try await AgentBrowser.shared.type(n, text: text, submit: submit)
            case "page_back":
                return try await AgentBrowser.shared.back()
            case "page_console":
                return try await AgentBrowser.shared.consoleMessages()
            case "page_snapshot":
                browserRequested += 1
                return try await AgentBrowser.shared.takeSnapshot()
            case "computer_read_screen":
                return try MacComputerUse.readFrontmostScreen()
            case "computer_click":
                let label = String((args["label"] as? String ?? "").prefix(120))
                guard await requestApproval(title: "Allow a Mac click?", detail: "Nodeyard AI wants to click “\(label)” in the frontmost app.") else { return "The user declined this computer action." }
                return try MacComputerUse.click(label: label)
            case "computer_set_text":
                let text = String((args["text"] as? String ?? "").prefix(4000))
                guard await requestApproval(title: "Allow text entry?", detail: "Nodeyard AI wants to replace text in the focused regular text field with:\n\n\(text.prefix(300))") else { return "The user declined this computer action." }
                return try MacComputerUse.setFocusedText(text)
            case "computer_press_key":
                let key = String((args["key"] as? String ?? "").prefix(40))
                guard await requestApproval(title: "Allow a key press?", detail: "Nodeyard AI wants to press \(key) in the frontmost app.") else { return "The user declined this computer action." }
                return try MacComputerUse.press(key)
            case "computer_open_app":
                let name = String((args["app"] as? String ?? "").prefix(40))
                guard await requestApproval(title: "Open an app?", detail: "Nodeyard AI wants to open \(name).") else { return "The user declined this computer action." }
                return try await MacComputerUse.openApplication(named: name)
            default:
                return "Tool error: that tool isn't available in Nodeyard AI."
            }
        } catch {
            return "Tool error: \(error.localizedDescription)"
        }
    }

    private func requestApproval(title: String, detail: String) async -> Bool {
        guard toolApprovalContinuation == nil else { return false }
        return await withCheckedContinuation { continuation in
            toolApprovalContinuation = continuation
            pendingToolApproval = ToolApproval(id: UUID().uuidString, title: title, detail: detail)
        }
    }

    private func appendStream(_ piece: String, reasoning: Bool, to messageID: String, chatID: String) {
        if promptProgress[messageID] != nil { promptProgress[messageID] = nil }
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

    private func reconnectClient() {
        client = NodeyardClient(baseAddress: serverAddress, apiKey: apiKey)
        manage.reconnect(address: serverAddress, apiKey: apiKey)
    }
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
