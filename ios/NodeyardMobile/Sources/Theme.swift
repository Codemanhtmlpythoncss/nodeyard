import SwiftUI

/// The app's few colors, taken from the nodeyard icon: indigo → blue → teal on a deep navy.
enum Theme {
    static let ink = Color(hex: 0x070B1A)       // background
    static let panel = Color(hex: 0x101833)     // raised surfaces
    static let line = Color.white.opacity(0.08)
    static let indigo = Color(hex: 0x5C6EF5)
    static let blue = Color(hex: 0x2978D1)
    static let teal = Color(hex: 0x12B3C7)
    static let mist = Color(hex: 0xA9B6D8)      // secondary text
    static let danger = Color(hex: 0xFF6B6B)

    static let brand = LinearGradient(colors: [indigo, blue, teal], startPoint: .topLeading, endPoint: .bottomTrailing)

    static let uiInk = UIColor(red: 7 / 255, green: 11 / 255, blue: 26 / 255, alpha: 1)

    static func title(_ size: CGFloat) -> Font { .system(size: size, weight: .bold, design: .rounded) }
}

extension Color {
    init(hex: UInt32) {
        self.init(red: Double((hex >> 16) & 0xFF) / 255, green: Double((hex >> 8) & 0xFF) / 255, blue: Double(hex & 0xFF) / 255)
    }
}

/// The primary button: full width, brand gradient, busy spinner.
struct BrandButtonStyle: ButtonStyle {
    var busy = false
    func makeBody(configuration: Configuration) -> some View {
        HStack(spacing: 10) {
            if busy { ProgressView().tint(.white) }
            configuration.label
        }
        .font(.system(size: 17, weight: .semibold, design: .rounded))
        .foregroundStyle(.white)
        .frame(maxWidth: .infinity, minHeight: 54)
        .background(Theme.brand, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .opacity(configuration.isPressed ? 0.85 : 1)
        .scaleEffect(configuration.isPressed ? 0.98 : 1)
        .animation(.easeOut(duration: 0.15), value: configuration.isPressed)
    }
}

enum Haptics {
    static func tap() { UIImpactFeedbackGenerator(style: .soft).impactOccurred() }
    static func success() { UINotificationFeedbackGenerator().notificationOccurred(.success) }
    static func error() { UINotificationFeedbackGenerator().notificationOccurred(.error) }
}
