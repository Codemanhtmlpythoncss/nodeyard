import Foundation
import Security

/// Secrets this app keeps: the server API key (chat, models) and, optionally, the dashboard sign-in password
/// (management: the same actions as the website). Both live in the login Keychain, readable only on this Mac.
enum KeychainStore {
    private static let service = "com.nodeyard.ai"
    private static let keyAccount = "server-api-key"
    private static let passwordAccount = "dashboard-password"

    static func read() -> String { TestMode.isOn ? TestMode.key : read(account: keyAccount) }
    static func write(_ value: String) throws { if !TestMode.isOn { try write(value, account: keyAccount) } }
    static func readDashboardPassword() -> String { TestMode.isOn ? "" : read(account: passwordAccount) }
    static func writeDashboardPassword(_ value: String) throws { if !TestMode.isOn { try write(value, account: passwordAccount) } }

    private static func read(account: String) -> String {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
                                    kSecAttrAccount as String: account, kSecReturnData as String: true, kSecMatchLimit as String: kSecMatchLimitOne]
        var result: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &result) == errSecSuccess, let data = result as? Data else { return "" }
        return String(data: data, encoding: .utf8) ?? ""
    }

    private static func write(_ value: String, account: String) throws {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service, kSecAttrAccount as String: account]
        SecItemDelete(query as CFDictionary)
        guard !value.isEmpty else { return }
        var insert = query
        insert[kSecValueData as String] = Data(value.utf8)
        insert[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
        let status = SecItemAdd(insert as CFDictionary, nil)
        guard status == errSecSuccess else { throw NSError(domain: NSOSStatusErrorDomain, code: Int(status)) }
    }
}

/// For automated checks against a test dashboard (e.g. `server.py --demo`): started with NODEYARD_AI_TEST_ADDRESS set,
/// the app uses that address and NODEYARD_AI_TEST_KEY, and never reads or writes the Keychain, the saved address or the
/// real chats. Nothing else turns it on.
enum TestMode {
    static let address = ProcessInfo.processInfo.environment["NODEYARD_AI_TEST_ADDRESS"] ?? ""
    static let key = ProcessInfo.processInfo.environment["NODEYARD_AI_TEST_KEY"] ?? ""
    static var isOn: Bool { !address.isEmpty }
}
