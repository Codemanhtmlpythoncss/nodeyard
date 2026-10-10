import SwiftUI
import UniformTypeIdentifiers

struct ContentView: View {
    @EnvironmentObject private var state: AppState
    @State private var showModels = false
    @State private var showSettings = false
    @State private var showChatSettings = false

    var body: some View {
        NavigationSplitView {
            sidebar
                .navigationSplitViewColumnWidth(min: 240, ideal: 285, max: 360)
        } detail: {
            Group {
                if showModels { ModelsView() }
                else if state.currentChat != nil { ChatDetailView(showSettings: $showChatSettings) }
                else { EmptyState(title: "No chat selected", icon: "bubble.left.and.bubble.right", message: "Create a chat to start talking to your cluster.") }
            }
            .toolbar { toolbar }
        }
        .sheet(isPresented: $showSettings) { AppSettingsView().environmentObject(state).frame(width: 650, height: 650) }
        .sheet(isPresented: $showChatSettings) {
            if let chat = state.currentChat { ChatSettingsView(chat: chat).environmentObject(state).frame(width: 560, height: 560) }
        }
        .fileImporter(isPresented: $state.showImport, allowedContentTypes: [.json], allowsMultipleSelection: false) { result in
            switch result { case .success(let urls): if let url = urls.first { state.importChat(from: url) }; case .failure(let error): state.error = error.localizedDescription }
        }
        .fileExporter(isPresented: $state.showExport, document: state.exportDocument, contentType: state.exportType, defaultFilename: state.exportFilename) { result in
            switch result { case .success: state.info = "Exported chat file."; case .failure(let error): state.error = error.localizedDescription }
        }
        .confirmationDialog(state.pendingToolApproval?.title ?? "Allow this Mac action?",
                            isPresented: Binding(get: { state.pendingToolApproval != nil }, set: { if !$0 { state.resolveToolApproval(allow: false) } }),
                            titleVisibility: .visible) {
            Button("Allow once") { state.resolveToolApproval(allow: true) }
            Button("Don't allow", role: .cancel) { state.resolveToolApproval(allow: false) }
        } message: { Text(state.pendingToolApproval?.detail ?? "") }
        .alert("Nodeyard AI", isPresented: Binding(get: { state.error != nil }, set: { if !$0 { state.error = nil } })) {
            Button("OK", role: .cancel) { state.error = nil }
        } message: { Text(state.error ?? "") }
        .overlay(alignment: .bottom) {
            if state.info != nil || state.deletedForUndo != nil {
                HStack(spacing: 10) {
                    Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
                    Text(state.deletedForUndo == nil ? state.info ?? "" : "Chat deleted.").lineLimit(2)
                    Spacer()
                    if state.deletedForUndo != nil { Button("Undo") { state.undoDelete() }.buttonStyle(.bordered) }
                    Button { state.info = nil; state.deletedForUndo = nil } label: { Image(systemName: "xmark") }.buttonStyle(.plain)
                    Button { state.info = nil } label: { Image(systemName: "xmark") }.buttonStyle(.plain)
                }
                .padding(12).background(.regularMaterial, in: RoundedRectangle(cornerRadius: 12)).shadow(radius: 14)
                .frame(maxWidth: 560).padding(.bottom, 16).transition(.move(edge: .bottom).combined(with: .opacity))
                .onAppear { DispatchQueue.main.asyncAfter(deadline: .now() + 5) { withAnimation { state.info = nil } } }
            }
        }
        .alert("Rename chat", isPresented: Binding(get: { state.renameChatID != nil }, set: { if !$0 { state.renameChatID = nil } })) {
            TextField("Chat name", text: $state.renameDraft)
            Button("Save") { state.commitRename() }.keyboardShortcut(.defaultAction)
            Button("Cancel", role: .cancel) { state.renameChatID = nil }
        } message: { Text("Choose a short name for this conversation.") }
        .animation(.easeInOut(duration: 0.2), value: state.info)
    }

    private var sidebar: some View {
        VStack(spacing: 0) {
            HStack {
                HStack(spacing: 9) {
                    NodeyardMark(size: 25)
                    Text("Nodeyard AI").font(.headline)
                }
                .accessibilityElement(children: .combine)
                Spacer()
                Button { state.createChat(); showModels = false } label: { Image(systemName: "square.and.pencil") }
                    .help("New chat (⌘N)").keyboardShortcut("n", modifiers: .command)
                Button { state.syncNow() } label: { Image(systemName: state.isSyncing ? "arrow.triangle.2.circlepath" : "arrow.clockwise") }
                    .help("Sync server chats")
            }.padding(.horizontal, 14).padding(.vertical, 12)
            TextField("Search chats", text: $state.searchText).textFieldStyle(.roundedBorder).padding(.horizontal, 12).padding(.bottom, 10)
                .help("Search your saved conversations")
            Picker("Chat list", selection: $state.showArchived) {
                Text("Chats").tag(false); Text("Archive").tag(true)
            }.pickerStyle(.segmented).padding(.horizontal, 12).padding(.bottom, 8)
            List(selection: $state.currentChatID) {
                Section(state.showArchived ? "Archived" : "Saved on this Mac") {
                    ForEach(state.filteredChats) { chat in
                        ChatRow(chat: chat)
                            .tag(chat.id)
                            .contextMenu { chatMenu(chat) }
                    }
                }
                if !state.remoteChats.isEmpty {
                    Section("Saved on the server") {
                        ForEach(state.remoteChats) { chat in
                            Button { state.importRemote(chat) } label: {
                                VStack(alignment: .leading, spacing: 3) {
                                    Text(chat.title).lineLimit(1)
                                    Text("\(chat.count) messages · \(chat.model)").font(.caption).foregroundStyle(.secondary).lineLimit(1)
                                }.frame(maxWidth: .infinity, alignment: .leading)
                            }.buttonStyle(.plain).contextMenu {
                                Button("Open on this Mac") { state.importRemote(chat) }
                                Button("Delete from server", role: .destructive) { state.deleteRemote(chat) }
                            }
                        }
                    }
                }
            }
            .listStyle(.sidebar)
            HStack(spacing: 8) {
                Circle().fill(state.isConnected ? .green : .orange).frame(width: 8, height: 8)
                Text(state.isConnected ? "Connected" : "Not connected").font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button { showModels = true } label: { Label("Models", systemImage: "cpu") }.buttonStyle(.plain)
            }.padding(12)
        }
        .toolbar { ToolbarItem(placement: .automatic) { Button { showSettings = true } label: { Image(systemName: "gearshape") }.help("Settings") } }
    }

    @ViewBuilder private func chatMenu(_ chat: ChatRecord) -> some View {
        Button("Rename…") { state.beginRename(chat) }
        Button(chat.pinned ? "Unpin" : "Pin") { state.togglePinned(chat) }
        Button(chat.archived ? "Restore from Archive" : "Archive") { state.toggleArchived(chat) }
        Button("Duplicate") { state.duplicate(chat) }
        Button("Export as Markdown…") { state.exportMarkdown(chat) }
        Button("Export as JSON…") { state.exportJSON(chat) }
        Divider()
        Button("Delete", role: .destructive) { state.delete(chat) }
    }

    @ToolbarContentBuilder private var toolbar: some ToolbarContent {
        ToolbarItem(placement: .principal) {
            Picker("Workspace", selection: $showModels) { Text("Chat").tag(false); Text("Models").tag(true) }.pickerStyle(.segmented).frame(width: 170)
        }
        if !showModels, let chat = state.currentChat {
            ToolbarItem(placement: .automatic) {
                Menu {
                    if state.readyTargets.isEmpty { Text("No ready models").foregroundStyle(.secondary) }
                    ForEach(state.readyTargets) { target in
                        Button {
                            state.chooseTarget(target)
                        } label: { if target.id == chat.modelTarget { Label(target.name, systemImage: "checkmark") } else { Text(target.name) } }
                    }
                    Divider()
                    Button("Manage models…") { showModels = true }
                } label: {
                    Label(chat.modelName.isEmpty ? "Choose model" : chat.modelName, systemImage: "cpu").lineLimit(1)
                }.help("Select a ready model")
            }
            ToolbarItem(placement: .automatic) {
                Button { showChatSettings = true } label: { Image(systemName: "slider.horizontal.3") }.help("Chat settings")
            }
        }
        ToolbarItem(placement: .automatic) {
            Button { state.refresh() } label: { Image(systemName: state.isRefreshing ? "arrow.triangle.2.circlepath" : "arrow.clockwise") }.help("Refresh model status")
        }
    }
}

private struct ChatRow: View {
    let chat: ChatRecord
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 5) {
                if chat.pinned { Image(systemName: "pin.fill").foregroundStyle(.orange).font(.caption2) }
                Text(chat.title).font(.subheadline.weight(.medium)).lineLimit(1)
            }
            Text(chat.messages.last?.content.isEmpty == false ? chat.messages.last!.content : "No messages yet")
                .font(.caption).foregroundStyle(.secondary).lineLimit(2)
            HStack { Text(chat.modelName.isEmpty ? "No model" : chat.modelName); Spacer(); Text(chat.updated, style: .relative) }
                .font(.caption2).foregroundStyle(.tertiary).lineLimit(1)
        }.padding(.vertical, 3).contentShape(Rectangle())
    }
}

private struct ChatDetailView: View {
    @EnvironmentObject private var state: AppState
    @Binding var showSettings: Bool
    @State private var showImporter = false
    @State private var dropTarget = false
    @FocusState private var composerFocused: Bool
    @State private var scrollToBottom = false

    var body: some View {
        VStack(spacing: 0) {
            if let chat = state.currentChat {
                if let target = state.targets.first(where: { $0.id == chat.modelTarget }), !target.ready {
                    Label("\(target.name) is not ready. Pick a ready model or start one in Models.", systemImage: "exclamationmark.triangle.fill")
                        .font(.callout).foregroundStyle(.orange).frame(maxWidth: .infinity, alignment: .leading).padding(12).background(.orange.opacity(0.08))
                }
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 18) {
                            if chat.messages.isEmpty { welcome(chat) }
                            ForEach(Array(chat.messages.enumerated()), id: \.element.id) { index, message in
                                MessageCard(message: message, isLatest: index == chat.messages.count - 1, sending: state.isSending)
                                    .id(message.id)
                            }
                            Color.clear.frame(height: 2).id("end")
                        }.frame(maxWidth: 880).padding(.horizontal, 30).padding(.vertical, 26).frame(maxWidth: .infinity)
                    }
                    .onChange(of: chat.messages.count) { _ in withAnimation(.easeOut(duration: 0.2)) { proxy.scrollTo("end", anchor: .bottom) } }
                    .onChange(of: chat.messages.last?.content.count ?? 0) { _ in proxy.scrollTo("end", anchor: .bottom) }
                }
                Divider()
                composer(chat)
            }
        }
        .fileImporter(isPresented: $showImporter, allowedContentTypes: [.image, .plainText, .json], allowsMultipleSelection: true) { result in
            switch result { case .success(let files): state.stageFiles(files); case .failure(let error): state.error = error.localizedDescription }
        }
        .onDrop(of: [UTType.fileURL], isTargeted: $dropTarget) { providers in
            for provider in providers {
                provider.loadItem(forTypeIdentifier: UTType.fileURL.identifier, options: nil) { item, _ in
                    let url: URL?
                    if let data = item as? Data { url = URL(dataRepresentation: data, relativeTo: nil) }
                    else { url = item as? URL }
                    if let url { DispatchQueue.main.async { state.stageFiles([url]) } }
                }
            }
            return true
        }
        .overlay { if dropTarget { RoundedRectangle(cornerRadius: 14).stroke(.tint, style: StrokeStyle(lineWidth: 3, dash: [8])).padding(8).allowsHitTesting(false) } }
    }

    private func welcome(_ chat: ChatRecord) -> some View {
        VStack(spacing: 12) {
            Image(systemName: "sparkles").font(.system(size: 32)).foregroundStyle(.tint)
            Text("What would you like to work on?").font(.title2.weight(.semibold))
            Text(chat.modelName.isEmpty ? "Choose a ready model from the toolbar to begin." : "Connected to \(chat.modelName)")
                .foregroundStyle(.secondary)
            HStack(spacing: 8) {
                suggestion("Explain a concept", icon: "lightbulb") { state.composer = "Explain a concept clearly: " }
                suggestion("Help with code", icon: "chevron.left.forwardslash.chevron.right") { state.composer = "Help me with this code: " }
                suggestion("Summarise a file", icon: "doc.text") { showImporter = true }
            }.padding(.top, 10)
        }.frame(maxWidth: .infinity).padding(.vertical, 100)
    }

    private func suggestion(_ title: String, icon: String, action: @escaping () -> Void) -> some View {
        Button(action: action) { Label(title, systemImage: icon).font(.callout).padding(10).background(.quaternary, in: Capsule()) }.buttonStyle(.plain)
    }

    private func composer(_ chat: ChatRecord) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            if !state.stagedAttachments.isEmpty {
                ScrollView(.horizontal) {
                    HStack(spacing: 8) {
                        ForEach(state.stagedAttachments) { item in
                            HStack(spacing: 6) {
                                Image(systemName: item.mime.hasPrefix("image/") ? "photo" : "doc.text")
                                Text(item.name).lineLimit(1).frame(maxWidth: 190)
                                Button { state.removeAttachment(item) } label: { Image(systemName: "xmark.circle.fill") }.buttonStyle(.plain)
                            }.font(.caption).padding(8).background(.quaternary, in: RoundedRectangle(cornerRadius: 8))
                        }
                    }
                }.frame(height: 36)
            }
            HStack(alignment: .bottom, spacing: 10) {
                TextField(state.editingMessageID == nil ? "Message Nodeyard AI…" : "Edit your message…", text: $state.composer, axis: .vertical)
                    .textFieldStyle(.plain).lineLimit(2...8).focused($composerFocused)
                    .onSubmit { state.beginSend() }
                    .font(.system(size: state.defaults.fontSize)).padding(.vertical, 9)
                    .help("Press ⌘ Return to send. Use Shift Return for a new line.")
                Menu {
                    Button { showImporter = true } label: { Label("Attach images or files…", systemImage: "paperclip") }
                    PasteButton(payloadType: Data.self) { items in if let data = items.first { state.stageImageData(data) } }
                    Divider()
                    Button { showSettings = true } label: { Label("Chat settings…", systemImage: "slider.horizontal.3") }
                } label: { Image(systemName: "plus.circle").font(.title3) }.menuStyle(.borderlessButton).help("Attach a file or change chat settings")
                if state.isSending {
                    Button { state.stop() } label: { Image(systemName: "stop.fill").frame(width: 28, height: 28) }
                        .buttonStyle(.borderedProminent).tint(.red).help("Stop generation (⌘.)")
                } else {
                    Button { state.beginSend() } label: { Image(systemName: "arrow.up").fontWeight(.semibold).frame(width: 28, height: 28) }
                        .buttonStyle(.borderedProminent).disabled(state.composer.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && state.stagedAttachments.isEmpty)
                        .help("Send message (⌘ Return)")
                }
            }
            HStack {
                Text("\(chat.modelName.isEmpty ? "Choose a model" : chat.modelName) · \(chat.contextLength.formatted()) context")
                    .font(.caption2).foregroundStyle(.secondary)
                Spacer()
                Text("Chats save automatically on this Mac\(state.defaults.syncChats ? " and sync to the server" : "")")
                    .font(.caption2).foregroundStyle(.tertiary)
            }
        }
        .padding(14).background(.background)
        .overlay(alignment: .top) { Rectangle().fill(dropTarget ? Color.accentColor : Color.secondary.opacity(0.2)).frame(height: dropTarget ? 2 : 1) }
    }
}

private struct MessageCard: View {
    @EnvironmentObject private var state: AppState
    let message: ChatMessage
    let isLatest: Bool
    let sending: Bool
    @State private var showReasoning = false
    @State private var showToolResult = false

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: message.role == "assistant" ? "sparkles" : (message.role == "tool" ? "wrench.and.screwdriver" : "person.fill"))
                .font(.caption.weight(.semibold)).foregroundStyle(message.role == "assistant" ? Color.accentColor : (message.role == "tool" ? Color.orange : Color.secondary))
                .frame(width: 28, height: 28).background(.quaternary, in: Circle())
            VStack(alignment: .leading, spacing: 9) {
                HStack {
                    Text(message.role == "assistant" ? "Nodeyard AI" : (message.role == "tool" ? "\(message.toolName?.replacingOccurrences(of: "_", with: " ") ?? "Tool") result" : "You"))
                        .font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                    if let duration = message.duration { Text("\(duration, specifier: "%.1f")s").font(.caption2).foregroundStyle(.tertiary) }
                    if let speed = message.tokensPerSecond { Text("\(speed, specifier: "%.1f") tok/s").font(.caption2).foregroundStyle(.tertiary) }
                    Spacer()
                    if message.role == "assistant" {
                        Text("\(message.content.components(separatedBy: .newlines).count) lines").font(.caption2).foregroundStyle(.tertiary).help("Select this text to copy it")
                        if isLatest && !sending { Button { state.regenerate() } label: { Image(systemName: "arrow.clockwise") }.help("Regenerate answer (⇧⌘R)") }
                    } else if message.role == "user" {
                        Button { state.edit(message) } label: { Image(systemName: "pencil") }.help("Edit and resend this message")
                    }
                }.buttonStyle(.plain)
                if !message.reasoning.isEmpty {
                    DisclosureGroup(isExpanded: $showReasoning) {
                        Text(message.reasoning).font(.system(.callout, design: .monospaced)).textSelection(.enabled).padding(.vertical, 6)
                    } label: { Label("Model reasoning", systemImage: "brain.head.profile").font(.caption).foregroundStyle(.secondary) }
                } else if isLatest && sending && message.content.isEmpty {
                    TimelineView(.periodic(from: message.created, by: 1)) { timeline in
                        let elapsed = max(0, Int(timeline.date.timeIntervalSince(message.created)))
                        VStack(alignment: .leading, spacing: 5) {
                            HStack(spacing: 7) {
                                ProgressView().controlSize(.small)
                                Text(elapsed < 5 ? "Thinking…" : "Waiting for the first token · \(elapsed)s")
                                    .foregroundStyle(.secondary)
                            }.font(.callout)
                            if elapsed >= 30 {
                                Text("The model is taking a while to start. Check that it is ready in Models, or stop and try again.")
                                    .font(.caption).foregroundStyle(.tertiary)
                            }
                        }
                    }
                }
                if message.role != "tool" && !message.content.isEmpty { MarkdownText(markdown: message.content).textSelection(.enabled).font(.system(size: state.defaults.fontSize)).fixedSize(horizontal: false, vertical: true) }
                if message.role == "tool" {
                    DisclosureGroup(isExpanded: $showToolResult) {
                        MarkdownText(markdown: message.content).textSelection(.enabled)
                            .font(.system(.caption, design: .monospaced)).fixedSize(horizontal: false, vertical: true).padding(.vertical, 5)
                    } label: { Text("View tool result").font(.caption).foregroundStyle(.secondary) }
                }
                if !message.attachments.isEmpty {
                    ForEach(message.attachments) { attachment in
                        Label(attachment.name, systemImage: attachment.mime.hasPrefix("image/") ? "photo" : "doc.text").font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
            .padding(13).frame(maxWidth: .infinity, alignment: .leading).background(message.role == "user" ? Color.accentColor.opacity(0.06) : Color.clear,
                                                                                in: RoundedRectangle(cornerRadius: 12))
        }
        .accessibilityElement(children: .contain)
    }
}

private struct MarkdownText: View {
    let markdown: String
    var body: some View {
        if let value = try? AttributedString(markdown: markdown, options: .init(interpretedSyntax: .full)) { Text(value) }
        else { Text(markdown) }
    }
}

private struct ModelsView: View {
    @EnvironmentObject private var state: AppState
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                VStack(alignment: .leading, spacing: 4) { Text("Models").font(.largeTitle.bold()); Text("Available across your Nodeyard cluster").foregroundStyle(.secondary) }
                Spacer()
                Button { state.refresh() } label: { Label("Refresh", systemImage: "arrow.clockwise") }.disabled(state.isRefreshing)
            }
            if let inventory = state.inventory {
                if !inventory.downloads.isEmpty {
                    GroupBox("Downloads in progress") {
                        ForEach(inventory.downloads) { item in
                            HStack { ProgressView().controlSize(.small); Text(item.file).lineLimit(1); Spacer(); Text(item.progress ?? item.state).foregroundStyle(.secondary) }.padding(.vertical, 4)
                        }
                    }
                }
                if inventory.models.isEmpty {
                    EmptyState(title: "No downloaded models found", icon: "cpu", message: "Download a GGUF model in the Nodeyard dashboard. This app can run models already saved on the cluster.")
                } else {
                    List(inventory.models) { model in
                        HStack(spacing: 12) {
                            Image(systemName: model.kind == "ollama" ? "shippingbox" : "cpu").font(.title3).foregroundStyle(.tint).frame(width: 42, height: 42).background(.tint.opacity(0.1), in: RoundedRectangle(cornerRadius: 10))
                            VStack(alignment: .leading, spacing: 4) {
                                HStack { Text(model.name).font(.headline); if model.ready { Text("Ready").font(.caption).foregroundStyle(.green) } else if model.loaded { Text("Starting").font(.caption).foregroundStyle(.orange) } }
                                Text(model.file.isEmpty ? model.kind.capitalized : model.file).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                                Text("\(ByteCountFormatStyle().format(model.size)) · on \(model.nodes.joined(separator: ", "))").font(.caption2).foregroundStyle(.tertiary)
                            }
                            Spacer()
                            if model.ready { Label("Running", systemImage: "checkmark.circle.fill").foregroundStyle(.green).font(.callout) }
                            else { Button("Run model") { state.loadModel(model) }.buttonStyle(.borderedProminent).disabled(state.isSending) }
                        }.padding(.vertical, 5)
                    }.listStyle(.inset)
                }
            } else {
                EmptyState(title: "Connect to see models", icon: "network.slash", message: "Set the server address and key in Settings.")
            }
            if let info = state.info { Text(info).font(.caption).foregroundStyle(.secondary) }
            Spacer(minLength: 0)
        }.padding(24)
    }
}

private struct EmptyState: View {
    let title: String
    let icon: String
    let message: String
    var body: some View {
        VStack(spacing: 10) {
            Image(systemName: icon).font(.system(size: 32)).foregroundStyle(.secondary)
            Text(title).font(.title3.weight(.semibold))
            Text(message).foregroundStyle(.secondary).multilineTextAlignment(.center).frame(maxWidth: 430)
        }.frame(maxWidth: .infinity, maxHeight: .infinity).padding(32)
    }
}
