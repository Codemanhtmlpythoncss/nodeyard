import AppKit
import ApplicationServices
import CoreGraphics

enum MacComputerUseError: LocalizedError {
    case permission
    case noFrontmostApp
    case noUniqueControl(String)
    case unsupportedControl(String)
    case secureField
    case unsupportedKey(String)
    case unsupportedApp(String)
    case appNotInstalled(String)
    case actionFailed(String)

    var errorDescription: String? {
        switch self {
        case .permission:
            "Computer use needs Accessibility permission. Allow Nodeyard AI in System Settings → Privacy & Security → Accessibility, then try again."
        case .noFrontmostApp: "There is no frontmost app to inspect."
        case .noUniqueControl(let label): "I couldn't find one unique clickable control named “\(label)”."
        case .unsupportedControl(let role): "The focused control is \(role), which I won't edit. Focus a regular text field first."
        case .secureField: "For safety, Nodeyard AI never reads or types into password fields."
        case .unsupportedKey(let key): "The key “\(key)” isn't in the safe key list."
        case .unsupportedApp(let name): "Opening “\(name)” isn't in the allowed app list."
        case .appNotInstalled(let name): "\(name) isn't installed on this Mac."
        case .actionFailed(let detail): detail
        }
    }
}

@MainActor
enum MacComputerUse {
    private static let applications: [String: String] = [
        "safari": "com.apple.Safari", "finder": "com.apple.finder", "textedit": "com.apple.TextEdit",
        "notes": "com.apple.Notes", "calendar": "com.apple.iCal", "calculator": "com.apple.calculator",
        "preview": "com.apple.Preview", "mail": "com.apple.mail", "chrome": "com.google.Chrome",
        "firefox": "org.mozilla.firefox",
    ]
    private static let keyCodes: [String: CGKeyCode] = [
        "return": 36, "tab": 48, "escape": 53, "space": 49,
        "up": 126, "down": 125, "left": 123, "right": 124,
        "command+l": 37,
    ]

    static func permissionGranted() -> Bool { AXIsProcessTrusted() }

    static func requestPermission() -> Bool {
        let options = ["AXTrustedCheckOptionPrompt": true] as CFDictionary
        return AXIsProcessTrustedWithOptions(options)
    }

    static func readFrontmostScreen() throws -> String {
        try requirePermission()
        guard let app = NSWorkspace.shared.frontmostApplication else { throw MacComputerUseError.noFrontmostApp }
        let root = AXUIElementCreateApplication(app.processIdentifier)
        var rows = ["App: \(app.localizedName ?? "Unknown")"]
        var visited = 0
        walk(root, depth: 0, rows: &rows, visited: &visited)
        return rows.joined(separator: "\n").prefix(8000).description
    }

    static func click(label: String) throws -> String {
        try requirePermission()
        let needle = label.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !needle.isEmpty, needle.count <= 120 else { throw MacComputerUseError.noUniqueControl(label) }
        guard let app = NSWorkspace.shared.frontmostApplication else { throw MacComputerUseError.noFrontmostApp }
        let root = AXUIElementCreateApplication(app.processIdentifier)
        var matches: [AXUIElement] = []
        findClickable(root, label: needle, depth: 0, matches: &matches)
        guard matches.count == 1 else { throw MacComputerUseError.noUniqueControl(needle) }
        let status = AXUIElementPerformAction(matches[0], kAXPressAction as CFString)
        guard status == .success else { throw MacComputerUseError.actionFailed("macOS couldn't activate “\(needle)” (Accessibility status \(status.rawValue)).") }
        return "Clicked “\(needle)” in \(app.localizedName ?? "the frontmost app")."
    }

    static func setFocusedText(_ text: String) throws -> String {
        try requirePermission()
        guard text.count <= 4000 else { throw MacComputerUseError.actionFailed("That text is too long to enter at once.") }
        let system = AXUIElementCreateSystemWide()
        var raw: CFTypeRef?
        guard AXUIElementCopyAttributeValue(system, kAXFocusedUIElementAttribute as CFString, &raw) == .success,
              let raw, CFGetTypeID(raw) == AXUIElementGetTypeID() else { throw MacComputerUseError.actionFailed("Focus a regular text field first.") }
        let focused = unsafeDowncast(raw, to: AXUIElement.self)
        let role = attribute(focused, kAXRoleAttribute as CFString) as? String ?? "unknown control"
        if role == (kAXTextFieldRole as String) && attribute(focused, kAXSubroleAttribute as CFString) as? String == "AXSecureTextField" {
            throw MacComputerUseError.secureField
        }
        guard [kAXTextFieldRole as String, kAXTextAreaRole as String, "AXComboBox"].contains(role) else {
            throw MacComputerUseError.unsupportedControl(role)
        }
        let status = AXUIElementSetAttributeValue(focused, kAXValueAttribute as CFString, text as CFTypeRef)
        guard status == .success else { throw MacComputerUseError.actionFailed("macOS couldn't set the focused text field (Accessibility status \(status.rawValue)).") }
        return "Entered text in the focused field."
    }

    static func press(_ key: String) throws -> String {
        try requirePermission()
        let normalized = key.lowercased()
        guard let code = keyCodes[normalized] else { throw MacComputerUseError.unsupportedKey(key) }
        guard let down = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: true),
              let up = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: false) else {
            throw MacComputerUseError.actionFailed("macOS couldn't create that key press.")
        }
        if normalized == "command+l" { down.flags = .maskCommand; up.flags = .maskCommand }
        down.post(tap: .cghidEventTap); up.post(tap: .cghidEventTap)
        return "Pressed \(key)."
    }

    static func openApplication(named name: String) async throws -> String {
        let normalized = name.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard let bundleID = applications[normalized] else { throw MacComputerUseError.unsupportedApp(name) }
        guard let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleID) else {
            throw MacComputerUseError.appNotInstalled(name)
        }
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            NSWorkspace.shared.openApplication(at: url, configuration: NSWorkspace.OpenConfiguration()) { _, error in
                if let error { continuation.resume(throwing: error) }
                else { continuation.resume(returning: ()) }
            }
        }
        return "Opened \(name)."
    }

    private static func requirePermission() throws {
        guard AXIsProcessTrusted() else { throw MacComputerUseError.permission }
    }

    private static func attribute(_ element: AXUIElement, _ name: CFString) -> AnyObject? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, name, &value) == .success else { return nil }
        return value as AnyObject?
    }

    private static func children(_ element: AXUIElement) -> [AXUIElement] {
        (attribute(element, kAXChildrenAttribute as CFString) as? [AXUIElement]) ?? []
    }

    private static func walk(_ element: AXUIElement, depth: Int, rows: inout [String], visited: inout Int) {
        guard depth <= 5, visited < 180, rows.joined(separator: "\n").count < 8000 else { return }
        visited += 1
        let role = attribute(element, kAXRoleAttribute as CFString) as? String ?? ""
        let title = attribute(element, kAXTitleAttribute as CFString) as? String ?? ""
        let description = attribute(element, kAXDescriptionAttribute as CFString) as? String ?? ""
        let value = role == "AXSecureTextField" ? "" : (attribute(element, kAXValueAttribute as CFString) as? String ?? "")
        let text = [title, description, value].map { String($0.prefix(180)).trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        if !text.isEmpty { rows.append("\(role): \(Array(Set(text)).joined(separator: " · "))") }
        for child in children(element) { walk(child, depth: depth + 1, rows: &rows, visited: &visited) }
    }

    private static func findClickable(_ element: AXUIElement, label: String, depth: Int, matches: inout [AXUIElement]) {
        guard depth <= 6, matches.count <= 1 else { return }
        let role = attribute(element, kAXRoleAttribute as CFString) as? String ?? ""
        if [kAXButtonRole as String, "AXLink", kAXMenuItemRole as String].contains(role) {
            let candidates = [kAXTitleAttribute, kAXDescriptionAttribute, kAXHelpAttribute].compactMap {
                attribute(element, $0 as CFString) as? String
            }
            if candidates.contains(where: { $0.localizedCaseInsensitiveCompare(label) == .orderedSame }) { matches.append(element) }
        }
        for child in children(element) { findClickable(child, label: label, depth: depth + 1, matches: &matches) }
    }
}
