import Foundation

/// Any JSON the dashboard sends. The management screens read it with defaults, so a field that is missing,
/// renamed or null in one server version shows as "–" instead of failing the whole screen.
enum JSON: Decodable, Sendable, Hashable {
    case null, bool(Bool), number(Double), string(String), array([JSON]), object([String: JSON])

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let b = try? c.decode(Bool.self) { self = .bool(b) }
        else if let n = try? c.decode(Double.self) { self = .number(n) }
        else if let s = try? c.decode(String.self) { self = .string(s) }
        else if let a = try? c.decode([JSON].self) { self = .array(a) }
        else { self = .object(try c.decode([String: JSON].self)) }
    }

    static func parse(_ data: Data) -> JSON? { try? JSONDecoder().decode(JSON.self, from: data) }

    subscript(key: String) -> JSON { if case .object(let o) = self { return o[key] ?? .null }; return .null }
    subscript(index: Int) -> JSON { if case .array(let a) = self, a.indices.contains(index) { return a[index] }; return .null }

    var isNull: Bool { if case .null = self { return true }; return false }
    var string: String? {
        switch self {
        case .string(let s): s
        case .number(let n): n == n.rounded() && abs(n) < 1e15 ? String(Int(n)) : String(n)
        case .bool(let b): b ? "true" : "false"
        default: nil
        }
    }
    var text: String { string ?? "" }
    var double: Double? {
        switch self {
        case .number(let n): n
        case .string(let s): Double(s)
        case .bool(let b): b ? 1 : 0
        default: nil
        }
    }
    var int: Int? { double.map { Int($0) } }
    var bool: Bool? {
        switch self {
        case .bool(let b): b
        case .number(let n): n != 0
        case .string(let s): ["true", "yes", "1"].contains(s.lowercased())
        default: nil
        }
    }
    var array: [JSON] { if case .array(let a) = self { return a }; return [] }
    var object: [String: JSON] { if case .object(let o) = self { return o }; return [:] }

    /// Back to Foundation types, for request bodies.
    var foundation: Any {
        switch self {
        case .null: NSNull()
        case .bool(let b): b
        case .number(let n): n
        case .string(let s): s
        case .array(let a): a.map(\.foundation)
        case .object(let o): o.mapValues(\.foundation)
        }
    }
}

enum Format {
    static func bytes(_ value: Double?) -> String {
        guard let value, value.isFinite else { return "–" }
        return ByteCountFormatter.string(fromByteCount: Int64(value), countStyle: .binary)
    }
    static func percent(_ part: Double?, of whole: Double?) -> String {
        guard let part, let whole, whole > 0 else { return "–" }
        return String(format: "%.0f%%", 100 * part / whole)
    }
    static func ratio(_ part: Double?, of whole: Double?) -> Double {
        guard let part, let whole, whole > 0 else { return 0 }
        return min(1, max(0, part / whole))
    }
    static func ago(_ epoch: Double?) -> String {
        guard let epoch, epoch > 0 else { return "–" }
        let seconds = max(0, Date().timeIntervalSince1970 - epoch)
        if seconds < 90 { return "\(Int(seconds)) s ago" }
        if seconds < 5400 { return "\(Int(seconds / 60)) min ago" }
        if seconds < 172800 { return "\(Int(seconds / 3600)) h ago" }
        return "\(Int(seconds / 86400)) days ago"
    }
    static func duration(_ seconds: Double?) -> String {
        guard let seconds, seconds.isFinite else { return "–" }
        let s = Int(max(0, seconds))
        if s < 90 { return "\(s) s" }
        if s < 5400 { return "\(Int((Double(s) / 60).rounded())) min" }
        let minutes = Int((Double(s) / 60).rounded())
        return "\(minutes / 60) h" + (minutes % 60 == 0 ? "" : " \(minutes % 60) min")
    }
}
