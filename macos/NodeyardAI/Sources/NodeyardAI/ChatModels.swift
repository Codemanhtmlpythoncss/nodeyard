import Foundation
import SwiftUI
import UniformTypeIdentifiers

struct ModelTarget: Codable, Identifiable, Hashable {
    var id: String
    var kind: String
    var name: String
    var model: String
    var detail: String
    var ready: Bool
    var size: Int64?
}

struct AppAttachment: Codable, Identifiable, Hashable {
    var id: String
    var name: String
    var mime: String
    var relativePath: String
    var extractedText: String = ""
}

struct ChatMessage: Codable, Identifiable, Hashable {
    var id: String = UUID().uuidString
    var role: String
    var content: String
    var reasoning: String = ""
    var created: Date = Date()
    var attachments: [AppAttachment] = []
    var duration: Double?
    var tokensPerSecond: Double?
    var tokenCount: Int?
    var toolCalls: [ModelToolCall]? = nil
    var toolCallID: String? = nil
    var toolName: String? = nil
}

struct ModelToolCall: Codable, Identifiable, Hashable {
    struct Function: Codable, Hashable {
        var name: String
        var arguments: String
    }
    var id: String
    var type: String = "function"
    var function: Function
}

struct ToolApproval: Identifiable {
    var id: String
    var title: String
    var detail: String
}

struct ChatRecord: Codable, Identifiable, Hashable {
    var id: String
    var title: String
    var modelTarget: String
    var modelName: String
    var systemPrompt: String
    var temperature: Double
    var maxTokens: Int
    var contextLength: Int
    var browserUse: Bool? = nil
    var computerUse: Bool? = nil
    var pinned: Bool = false
    var archived: Bool = false
    var created: Date = Date()
    var updated: Date = Date()
    var messages: [ChatMessage] = []

    static func fresh(model: ModelTarget? = nil, defaults: AppDefaults) -> ChatRecord {
        ChatRecord(id: "mac-" + UUID().uuidString.lowercased(), title: "New chat", modelTarget: model?.id ?? "",
                   modelName: model?.name ?? "", systemPrompt: defaults.systemPrompt, temperature: defaults.temperature,
                   maxTokens: defaults.maxTokens, contextLength: defaults.contextLength,
                   browserUse: defaults.browserUse, computerUse: defaults.computerUse)
    }
}

struct AppDefaults: Codable, Equatable {
    var systemPrompt: String = ""
    var temperature: Double = 0.7
    var maxTokens: Int = 1024
    var contextLength: Int = 8192
    var appearance: String = "system"
    var fontSize: Double = 14
    var syncChats: Bool = true
    var notifications: Bool = true
    var browserUse: Bool? = true
    var computerUse: Bool? = false
}

struct RemoteChatSummary: Codable, Identifiable {
    var id: String
    var title: String
    var source: String
    var model: String
    var updated: Double
    var count: Int
}

struct APIEnvelope<T: Decodable>: Decodable {
    var ok: Bool?
    var error: String?
    var data: T?
}

struct TargetResponse: Decodable { var targets: [ModelTarget] }
struct ChatListResponse: Decodable { var chats: [RemoteChatSummary] }
struct ModelInventory: Decodable { var models: [ModelInventoryRow]; var downloads: [DownloadItem] = [] }
struct ModelInventoryRow: Decodable, Identifiable {
    var id: String
    var kind: String
    var name: String
    var file: String
    var size: Int64
    var nodes: [String]
    var loaded: Bool
    var ready: Bool
}
struct DownloadItem: Decodable, Identifiable {
    var file: String
    var node: String
    var state: String
    var progress: String?
    var id: String { "\(file)-\(node)" }
}

struct StreamDelta: Decodable {
    struct Choice: Decodable {
        struct Delta: Decodable {
            struct ToolCall: Decodable {
                struct Function: Decodable { var name: String?; var arguments: String? }
                var index: Int
                var id: String?
                var type: String?
                var function: Function?
            }
            var content: String?
            var reasoning_content: String?
            var reasoning: String?
            var tool_calls: [ToolCall]?
        }
        var delta: Delta
    }
    var choices: [Choice]?
    var usage: Usage?
    var timings: Timings?
    struct Usage: Decodable { var completion_tokens: Int? }
    struct Timings: Decodable { var predicted_per_second: Double? }
}

struct OpenAIMessage: Encodable {
    var role: String
    var content: EncodableContent?
    var tool_calls: [ModelToolCall]? = nil
    var tool_call_id: String? = nil
    var name: String? = nil
}

enum EncodableContent: Encodable {
    case text(String)
    case parts([Part])
    struct Part: Encodable {
        var type: String
        var text: String?
        var image_url: ImageURL?
        struct ImageURL: Encodable { var url: String }
    }
    func encode(to encoder: Encoder) throws {
        switch self {
        case .text(let text): try text.encode(to: encoder)
        case .parts(let parts): try parts.encode(to: encoder)
        }
    }
}

struct ChatExportDocument: FileDocument {
    static var readableContentTypes: [UTType] { [.plainText, .json] }
    var data: Data

    init(data: Data) { self.data = data }
    init(configuration: ReadConfiguration) throws { data = configuration.file.regularFileContents ?? Data() }
    func fileWrapper(configuration: WriteConfiguration) throws -> FileWrapper { FileWrapper(regularFileWithContents: data) }
}
