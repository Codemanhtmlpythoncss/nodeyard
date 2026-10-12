import AppKit
import SwiftUI

/// An answer drawn block by block. SwiftUI's Text only lays out inline markdown (bold, code, links), so a whole answer
/// in one Text ran its paragraphs, list items and code together. Here each paragraph, heading, list item and quote is
/// its own line, and each fenced code block is a box with its language, Copy and Save (still growing while it streams).
struct MarkdownText: View {
    let markdown: String
    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            ForEach(Array(MarkdownBlock.parse(markdown).enumerated()), id: \.offset) { _, block in
                switch block {
                case let .code(language, code): CodeBlockView(language: language, code: code)
                case let .heading(level, text):
                    inline(text).font(level <= 1 ? .title2.bold() : level == 2 ? .title3.bold() : .headline).padding(.top, 4)
                case let .item(marker, text, depth):
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text(marker).foregroundStyle(.secondary).monospacedDigit()
                        inline(text)
                    }.padding(.leading, CGFloat(depth) * 16)
                case let .quote(text):
                    inline(text).foregroundStyle(.secondary).padding(.leading, 10)
                        .overlay(alignment: .leading) { Rectangle().fill(.tertiary).frame(width: 3) }
                case .rule: Divider()
                case let .paragraph(text): inline(text)
                }
            }
        }
    }

    private func inline(_ text: String) -> Text {
        if let value = try? AttributedString(markdown: text, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)) { return Text(value) }
        return Text(text)
    }
}

enum MarkdownBlock {
    case code(String, String)
    case heading(Int, String)
    case item(String, String, Int)
    case quote(String)
    case rule
    case paragraph(String)

    static func parse(_ markdown: String) -> [MarkdownBlock] {
        var blocks: [MarkdownBlock] = []
        var paragraph: [String] = []
        var fence: String?                       // the fence that opened the code block we are in ("```" or "~~~")
        var language = ""
        var code: [String] = []
        func flush() {
            if !paragraph.isEmpty { blocks.append(.paragraph(paragraph.joined(separator: "\n"))); paragraph = [] }
        }
        for line in markdown.replacingOccurrences(of: "\r\n", with: "\n").components(separatedBy: "\n") {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if let open = fence {
                if trimmed.hasPrefix(open) && trimmed.drop(while: { $0 == open.first }).trimmingCharacters(in: .whitespaces).isEmpty {
                    blocks.append(.code(language, code.joined(separator: "\n"))); fence = nil; code = []
                } else { code.append(line) }
                continue
            }
            if trimmed.hasPrefix("```") || trimmed.hasPrefix("~~~") {
                flush()
                fence = String(trimmed.prefix(3)); language = String(trimmed.dropFirst(3)).trimmingCharacters(in: .whitespaces)
                continue
            }
            if trimmed.isEmpty { flush(); continue }
            let indent = line.prefix(while: { $0 == " " || $0 == "\t" }).reduce(0) { $0 + ($1 == "\t" ? 4 : 1) }
            if let match = trimmed.firstMatch(of: /^(#{1,6})\s+(.*)$/) {
                flush(); blocks.append(.heading(match.1.count, String(match.2))); continue
            }
            if trimmed.wholeMatch(of: /(-\s*){3,}|(\*\s*){3,}|(_\s*){3,}/) != nil { flush(); blocks.append(.rule); continue }
            if let match = trimmed.firstMatch(of: /^[-*+•]\s+(.*)$/) {
                flush(); blocks.append(.item("•", String(match.1), indent / 2)); continue
            }
            if let match = trimmed.firstMatch(of: /^(\d{1,3})[.)]\s+(.*)$/) {
                flush(); blocks.append(.item(String(match.1) + ".", String(match.2), indent / 2)); continue
            }
            if let match = trimmed.firstMatch(of: /^>\s?(.*)$/) {
                flush(); blocks.append(.quote(String(match.1))); continue
            }
            paragraph.append(line)
        }
        flush()
        if fence != nil { blocks.append(.code(language, code.joined(separator: "\n"))) }   // still being written
        return blocks
    }
}

/// A code block from an answer: its language, Copy, Save… and (for Python/Bash/JavaScript) Run, plus the code in a box
/// that scrolls sideways. Run sends the code to the dashboard's sandbox and shows its output; if the code reads input,
/// a stdin box appears to fill first.
struct CodeBlockView: View {
    let language: String
    let code: String
    @EnvironmentObject private var state: AppState
    @State private var copied = false
    @State private var running = false
    @State private var output: (rc: Int?, text: String)?
    @State private var showStdin = false
    @State private var stdin = ""

    /// The sandbox runs these three; anything else has no Run button. Maps the block's label to the runner's name.
    private var runner: String? {
        switch language.lowercased() {
        case "python", "py", "python3": return "python"
        case "bash", "sh", "shell", "zsh": return "bash"
        case "javascript", "js", "node", "nodejs": return "node"
        default: return nil
        }
    }
    /// Code that reads standard input, so the stdin box is offered up front.
    private var readsInput: Bool {
        let c = code.lowercased()
        return c.contains("input(") || c.contains("sys.stdin") || c.contains("readline") || c.range(of: #"(^|\n)\s*read\s"#, options: .regularExpression) != nil
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 10) {
                Text(language.isEmpty ? "code" : language).font(.caption.monospaced()).foregroundStyle(.secondary)
                Spacer()
                if runner != nil {
                    if readsInput || showStdin {
                        Button { withAnimation { showStdin.toggle() } } label: { Label("Input", systemImage: showStdin ? "chevron.down" : "keyboard") }
                            .help("Type the input the program will read")
                    }
                    Button(action: run) { Label(running ? "Running…" : "Run", systemImage: running ? "hourglass" : "play.fill") }
                        .disabled(running).help("Run this in the dashboard sandbox and show the output")
                }
                Button(action: save) { Label("Save…", systemImage: "square.and.arrow.down") }.help("Save this code as a file")
                Button(action: copy) { Label(copied ? "Copied" : "Copy", systemImage: copied ? "checkmark" : "doc.on.doc") }
                    .help("Copy this code")
            }
            .font(.caption).buttonStyle(.borderless)
            .padding(.horizontal, 10).padding(.vertical, 6).background(Color.primary.opacity(0.06))
            Divider()
            ScrollView(.horizontal) {
                Text(code.isEmpty ? " " : code).monospaced().textSelection(.enabled).fixedSize(horizontal: true, vertical: true)
                    .padding(10).frame(maxWidth: .infinity, alignment: .leading)
            }
            if showStdin {
                Divider()
                VStack(alignment: .leading, spacing: 3) {
                    Text("Input (stdin) — one line per prompt").font(.caption2).foregroundStyle(.secondary)
                    TextEditor(text: $stdin).font(.callout.monospaced()).frame(height: 56)
                        .overlay(RoundedRectangle(cornerRadius: 5).strokeBorder(Color.primary.opacity(0.15)))
                        .scrollContentBackground(.hidden)
                }.padding(10)
            }
            if let output {
                Divider()
                VStack(alignment: .leading, spacing: 5) {
                    HStack(spacing: 8) {
                        Image(systemName: output.rc == 0 ? "checkmark.circle.fill" : "xmark.octagon.fill")
                            .foregroundStyle(output.rc == 0 ? .green : .orange)
                        Text(output.rc == nil ? "Couldn't run" : output.rc == 0 ? "Output" : "Exited with code \(output.rc!)")
                            .font(.caption.weight(.medium)).foregroundStyle(.secondary)
                        Spacer()
                        Button { self.output = nil } label: { Image(systemName: "xmark") }.buttonStyle(.borderless).help("Clear output")
                    }
                    ScrollView(.horizontal) {
                        Text(output.text.isEmpty ? "(no output)" : output.text).font(.callout.monospaced()).textSelection(.enabled)
                            .fixedSize(horizontal: true, vertical: true)
                    }
                }.padding(10).background(Color.primary.opacity(0.04))
            }
        }
        .background(Color.primary.opacity(0.035))
        .clipShape(RoundedRectangle(cornerRadius: 8))
        .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(Color.primary.opacity(0.12)))
    }

    private func run() {
        guard let lang = runner else { return }
        running = true
        Task {
            let result = await state.runSnippet(language: lang, code: code, stdin: stdin)
            output = (result.rc, result.out)
            running = false
        }
    }

    private func copy() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(code, forType: .string)
        copied = true
        Task { try? await Task.sleep(for: .seconds(1.6)); copied = false }
    }

    private func save() {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = "code." + Self.fileExtension(language)
        panel.canCreateDirectories = true
        guard panel.runModal() == .OK, let url = panel.url else { return }
        try? (code.hasSuffix("\n") ? code : code + "\n").write(to: url, atomically: true, encoding: .utf8)
    }

    static func fileExtension(_ language: String) -> String {
        let known = ["python": "py", "py": "py", "javascript": "js", "js": "js", "typescript": "ts", "ts": "ts", "swift": "swift",
                     "bash": "sh", "sh": "sh", "shell": "sh", "zsh": "sh", "json": "json", "yaml": "yaml", "yml": "yaml", "html": "html",
                     "css": "css", "go": "go", "rust": "rs", "rs": "rs", "c": "c", "cpp": "cpp", "c++": "cpp", "java": "java", "ruby": "rb",
                     "sql": "sql", "markdown": "md", "md": "md", "toml": "toml", "dockerfile": "Dockerfile", "kotlin": "kt", "php": "php"]
        return known[language.lowercased()] ?? "txt"
    }
}
