import AVFoundation
import Foundation

/// Reads answers aloud with the Mac's own voices (nothing leaves the Mac). Any answer can be read with its speaker
/// button; with Settings › Read aloud on, each finished sentence is spoken while the answer is still being written.
@MainActor
final class SpeechReader: NSObject, ObservableObject {
    static let shared = SpeechReader()

    /// The answer being read right now (for its button), if any.
    @Published private(set) var speakingID: String?
    var voiceIdentifier = ""
    var rate = 1.0

    private let synth = AVSpeechSynthesizer()
    private let relay = Relay()
    private var current = Set<ObjectIdentifier>()   // utterances of the current reading still to finish
    private var live: String?
    private var spoken: [String: Int] = [:]   // answer -> characters of its speakable text already queued

    override init() {
        super.init()
        relay.finished = { [weak self] id in self?.utteranceEnded(id) }
        synth.delegate = relay
    }

    static var voices: [AVSpeechSynthesisVoice] {
        AVSpeechSynthesisVoice.speechVoices().sorted { ($0.language, $0.name) < ($1.language, $1.name) }
    }

    /// Markdown as it should sound: no code, links, tables or formatting marks; paragraphs become pauses.
    static func speakable(_ markdown: String) -> String {
        var t = markdown
        let rules: [(String, String)] = [
            (#"<think>[\s\S]*?(</think>|$)"#, " "),
            ("```[\\s\\S]*?(```|$)", " (code) "),
            (#"<file\b[\s\S]*?(</file>|$)"#, " (a file) "),
            (#"!\[[^\]]*\]\([^)]*\)?"#, " "),
            (#"\[([^\]]+)\]\([^)\s]*\)?"#, "$1"),
            ("`([^`]*)`", "$1"),
            (#"https?://\S+"#, " a link "),
            (#"(?m)^\s*\|.*$"#, " "),
            (#"(?m)^\s{0,3}#{1,6}\s+"#, ""),
            (#"(?m)^\s*[-*+]\s+"#, ""),
            (#"(?m)^\s*>\s?"#, ""),
            ("[*_~#|`\\[\\]]+", ""),
            (#"\n{2,}"#, ".\n"),
            (#"([.!?:])\.\n"#, "$1\n"),
            (#"[ \t]+"#, " "),
        ]
        for (pattern, template) in rules {
            t = t.replacingOccurrences(of: pattern, with: template, options: .regularExpression)
        }
        return t
    }

    func toggle(_ message: ChatMessage) {
        if speakingID == message.id { stop() } else { read(message.content, id: message.id) }
    }

    func read(_ text: String, id: String) {
        stop()
        let words = Self.speakable(text).trimmingCharacters(in: .whitespacesAndNewlines)
        guard !words.isEmpty else { return }
        speakingID = id
        say(words)
    }

    func stop() {
        live = nil
        current.removeAll()
        spoken.removeAll()
        if synth.isSpeaking { synth.stopSpeaking(at: .immediate) }
        speakingID = nil
    }

    /// An answer streaming in: speak the sentences that are complete (all of it when final).
    func feed(id: String, text: String, final: Bool) {
        if live != id {
            if live == nil && speakingID != nil && speakingID != id { stop() }
            live = id
            spoken[id] = 0
            speakingID = id
        }
        let words = Self.speakable(text)
        let chars = Array(words)
        let from = min(spoken[id] ?? 0, chars.count)
        var end = chars.count
        if !final {
            end = -1
            var i = 0
            while i < chars.count {
                let c = chars[i]
                if c == "\n" { end = i + 1 } else if ".!?".contains(c), i + 1 < chars.count, chars[i + 1] == " " || chars[i + 1] == "\n" { end = i + 1 }
                i += 1
            }
        }
        if end > from {
            spoken[id] = end
            let piece = String(chars[from..<end])
            if !piece.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { say(piece) }
        }
        if final {
            live = nil
            if current.isEmpty { speakingID = nil }
        }
    }

    private func say(_ text: String) {
        let voice = AVSpeechSynthesisVoice(identifier: voiceIdentifier) ?? AVSpeechSynthesisVoice(language: Locale.preferredLanguages.first)
        for piece in Self.pieces(text) {
            let u = AVSpeechUtterance(string: piece)
            u.voice = voice
            u.rate = Float(min(2, max(0.5, rate))) * AVSpeechUtteranceDefaultSpeechRate
            current.insert(ObjectIdentifier(u))
            synth.speak(u)
        }
    }

    private func utteranceEnded(_ id: ObjectIdentifier) {
        guard current.remove(id) != nil else { return }   // (one from a reading that was stopped)
        if current.isEmpty && live == nil { speakingID = nil }
    }

    /// At most ~300 characters per utterance, split at sentence ends, commas or spaces.
    static func pieces(_ text: String) -> [String] {
        var out: [String] = []
        var rest = text.replacingOccurrences(of: #"\s+"#, with: " ", options: .regularExpression).trimmingCharacters(in: .whitespaces)
        while rest.count > 300 {
            let head = String(rest.prefix(300))
            var cut = [". ", "! ", "? "].compactMap { head.range(of: $0, options: .backwards)?.upperBound }.max()
            if cut == nil || head.distance(from: head.startIndex, to: cut!) < 100 { cut = head.range(of: ", ", options: .backwards)?.upperBound }
            if cut == nil || head.distance(from: head.startIndex, to: cut!) < 100 { cut = head.range(of: " ", options: .backwards)?.upperBound }
            let n = cut.map { head.distance(from: head.startIndex, to: $0) } ?? 300
            out.append(String(rest.prefix(n)))
            rest = String(rest.dropFirst(n)).trimmingCharacters(in: .whitespaces)
        }
        if !rest.isEmpty { out.append(rest) }
        return out
    }
}

/// AVSpeechSynthesizer calls its delegate off the main actor: this hops back.
private final class Relay: NSObject, AVSpeechSynthesizerDelegate, @unchecked Sendable {
    var finished: (@MainActor (ObjectIdentifier) -> Void)?

    func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) { done(utterance) }
    func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didCancel utterance: AVSpeechUtterance) { done(utterance) }

    private func done(_ utterance: AVSpeechUtterance) {
        let f = finished, id = ObjectIdentifier(utterance)
        Task { @MainActor in f?(id) }
    }
}
