import Foundation

@MainActor
final class ChatStore {
    private let fm = FileManager.default
    let root: URL
    let chatsDirectory: URL
    let attachmentsDirectory: URL
    private let defaultsURL: URL

    init() {
        let support = fm.urls(for: .applicationSupportDirectory, in: .userDomainMask).first ?? fm.temporaryDirectory
        root = support.appendingPathComponent("NodeyardAI", isDirectory: true)
        chatsDirectory = root.appendingPathComponent("Chats", isDirectory: true)
        attachmentsDirectory = root.appendingPathComponent("Attachments", isDirectory: true)
        defaultsURL = root.appendingPathComponent("defaults.json")
        try? fm.createDirectory(at: chatsDirectory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        try? fm.createDirectory(at: attachmentsDirectory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
    }

    func loadChats() -> [ChatRecord] {
        guard let files = try? fm.contentsOfDirectory(at: chatsDirectory, includingPropertiesForKeys: nil) else { return [] }
        let decoder = JSONDecoder()
        return files.filter { $0.pathExtension == "json" }.compactMap { try? decoder.decode(ChatRecord.self, from: Data(contentsOf: $0)) }
            .sorted { $0.updated > $1.updated }
    }

    func save(_ chat: ChatRecord) throws {
        var value = chat
        value.updated = Date()
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        let data = try encoder.encode(value)
        try data.write(to: chatsDirectory.appendingPathComponent(safeID(value.id) + ".json"), options: .atomic)
    }

    func remove(_ chat: ChatRecord) {
        try? fm.removeItem(at: chatsDirectory.appendingPathComponent(safeID(chat.id) + ".json"))
    }

    func saveDefaults(_ value: AppDefaults) throws {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        try encoder.encode(value).write(to: defaultsURL, options: .atomic)
    }

    func loadDefaults() -> AppDefaults {
        guard let data = try? Data(contentsOf: defaultsURL), let value = try? JSONDecoder().decode(AppDefaults.self, from: data) else { return AppDefaults() }
        return value
    }

    func copyAttachment(from source: URL) throws -> AppAttachment {
        let scoped = source.startAccessingSecurityScopedResource()
        defer { if scoped { source.stopAccessingSecurityScopedResource() } }
        let ext = source.pathExtension.lowercased()
        let mime: String
        switch ext {
        case "png": mime = "image/png"
        case "jpg", "jpeg": mime = "image/jpeg"
        case "webp": mime = "image/webp"
        case "txt", "md", "csv", "json", "swift", "py", "sh", "html", "xml", "yaml", "yml": mime = "text/plain"
        default: throw NodeyardError.server("\(source.lastPathComponent) is not a supported image or text document.")
        }
        let data = try Data(contentsOf: source)
        guard data.count <= 8 * 1024 * 1024 else { throw NodeyardError.server("Each attachment must be 8 MiB or smaller.") }
        var extracted = ""
        if mime == "text/plain" {
            extracted = String(data: data, encoding: .utf8) ?? ""
            if extracted.isEmpty { throw NodeyardError.server("\(source.lastPathComponent) isn't a UTF-8 text file.") }
            extracted = String(extracted.prefix(100_000))
        }
        let id = UUID().uuidString.lowercased()
        let rel = id + (ext.isEmpty ? ".dat" : "." + ext)
        try data.write(to: attachmentsDirectory.appendingPathComponent(rel), options: .atomic)
        return AppAttachment(id: id, name: source.lastPathComponent, mime: mime, relativePath: rel, extractedText: extracted)
    }

    func attachmentData(_ attachment: AppAttachment) -> Data? {
        try? Data(contentsOf: attachmentsDirectory.appendingPathComponent(attachment.relativePath))
    }

    func importChat(from url: URL) throws -> ChatRecord {
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        let chat = try JSONDecoder().decode(ChatRecord.self, from: Data(contentsOf: url))
        var copy = chat
        copy.id = "mac-" + UUID().uuidString.lowercased()
        copy.title = copy.title + " (imported)"
        copy.created = Date(); copy.updated = Date()
        try save(copy)
        return copy
    }

    func exportData(_ chat: ChatRecord) throws -> Data { try JSONEncoder().encode(chat) }
    func attachmentURL(_ attachment: AppAttachment) -> URL { attachmentsDirectory.appendingPathComponent(attachment.relativePath) }
    private func safeID(_ id: String) -> String { String(id.filter { $0.isLetter || $0.isNumber || $0 == "-" || $0 == "_" }.prefix(64)) }
}
