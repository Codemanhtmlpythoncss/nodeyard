import SwiftUI
import UIKit

// Small building blocks shared by every screen.

/// A raised surface. Corner radius scales with what it holds: big cards are rounder than chips.
struct Panel<Content: View>: View {
    var padding: CGFloat = 16
    var radius: CGFloat = 20
    @ViewBuilder var content: Content
    var body: some View {
        content
            .padding(padding)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Theme.panel, in: RoundedRectangle(cornerRadius: radius, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: radius, style: .continuous).stroke(Theme.line))
    }
}

/// A ring that fills to a fraction, with the number in the middle.
struct Ring: View {
    let fraction: Double
    let label: String
    var detail: String = ""
    var size: CGFloat = 96
    var body: some View {
        VStack(spacing: 8) {
            ZStack {
                Circle().stroke(Color.white.opacity(0.07), lineWidth: size * 0.11)
                Circle()
                    .trim(from: 0, to: max(0.001, fraction))
                    .stroke(Ring.color(fraction), style: StrokeStyle(lineWidth: size * 0.11, lineCap: .round))
                    .rotationEffect(.degrees(-90))
                    .animation(.spring(response: 0.6, dampingFraction: 0.85), value: fraction)
                Text(Format.percent(fraction))
                    .font(.system(size: size * 0.24, weight: .bold, design: .rounded))
                    .monospacedDigit()
                    .contentTransition(.numericText())
            }
            .frame(width: size, height: size)
            VStack(spacing: 2) {
                Text(label).font(.system(size: 14, weight: .semibold, design: .rounded))
                if !detail.isEmpty {
                    Text(detail).font(.system(size: 12, design: .rounded)).foregroundStyle(Theme.mist).lineLimit(1).minimumScaleFactor(0.7)
                }
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(label) \(Format.percent(fraction)) \(detail)")
    }

    static func color(_ f: Double) -> AnyShapeStyle {
        if f >= 0.9 { return AnyShapeStyle(Theme.danger) }
        if f >= 0.75 { return AnyShapeStyle(Color.orange) }
        return AnyShapeStyle(Theme.brand)
    }
}

/// A thin bar for a fraction, used in lists.
struct Meter: View {
    let fraction: Double
    var body: some View {
        GeometryReader { g in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.white.opacity(0.08))
                Capsule().fill(Ring.color(fraction)).frame(width: max(4, g.size.width * fraction))
            }
        }
        .frame(height: 6)
        .animation(.easeOut(duration: 0.4), value: fraction)
    }
}

struct HealthDot: View {
    let health: Health
    var body: some View {
        Circle().fill(HealthDot.color(health)).frame(width: 9, height: 9)
            .shadow(color: HealthDot.color(health).opacity(0.6), radius: 4)
    }
    static func color(_ h: Health) -> Color {
        switch h {
        case .good: return Color(hex: 0x3DDC97)
        case .warning: return .orange
        case .bad: return Theme.danger
        }
    }
}

/// "Nothing here" with a way forward.
struct EmptyNote: View {
    let symbol: String
    let title: String
    var detail: String = ""
    var body: some View {
        VStack(spacing: 10) {
            Image(systemName: symbol).font(.system(size: 34, weight: .light)).foregroundStyle(Theme.mist)
            Text(title).font(Theme.title(18))
            if !detail.isEmpty {
                Text(detail).font(.system(size: 14, design: .rounded)).foregroundStyle(Theme.mist).multilineTextAlignment(.center)
            }
        }
        .padding(32)
        .frame(maxWidth: .infinity)
    }
}

struct ErrorBanner: View {
    let text: String
    var body: some View {
        Label(text, systemImage: "exclamationmark.triangle.fill")
            .font(.system(size: 14, design: .rounded))
            .foregroundStyle(Theme.danger)
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Theme.danger.opacity(0.12), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
    }
}

/// Screen background and list styling every tab shares.
extension View {
    func screen() -> some View {
        self
            .scrollContentBackground(.hidden)
            .background(Theme.ink.ignoresSafeArea())
            .toolbarBackground(Theme.ink.opacity(0.92), for: .navigationBar)
            .toolbarColorScheme(.dark, for: .navigationBar)
    }

    func rowStyle() -> some View { listRowBackground(Theme.panel).listRowSeparatorTint(Theme.line) }
}

/// A field with an icon, used on the sign-in screen and in settings.
struct InputField: View {
    let symbol: String
    let placeholder: String
    @Binding var text: String
    var secure = false
    var failed = false
    var focused: FocusState<Bool>.Binding
    var submit: () -> Void = {}

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: symbol).foregroundStyle(Theme.mist).frame(width: 20)
            Group {
                if secure {
                    SecureField("", text: $text, prompt: Text(placeholder).foregroundColor(Theme.mist.opacity(0.5)))
                        .textContentType(.password)
                } else {
                    TextField("", text: $text, prompt: Text(placeholder).foregroundColor(Theme.mist.opacity(0.5)))
                        .keyboardType(.URL)
                        .textContentType(.URL)
                }
            }
            .textInputAutocapitalization(.never)
            .autocorrectionDisabled()
            .submitLabel(.go)
            .focused(focused)
            .onSubmit(submit)
        }
        .padding(.horizontal, 16)
        .frame(height: 54)
        .background(Theme.panel, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .stroke(failed ? Theme.danger : (focused.wrappedValue ? Theme.indigo : Theme.line), lineWidth: 1.5)
        )
    }
}

// MARK: - Images that need the sign-in cookie (AsyncImage can't send one)

@MainActor
final class ImageCache {
    static let shared = ImageCache()
    private let memory = NSCache<NSURL, UIImage>()
    private var missing = Set<URL>()
    private var running: [URL: Task<UIImage?, Never>] = [:]

    init() { memory.countLimit = 300 }

    func image(_ url: URL, api: NodeyardAPI) async -> UIImage? {
        if let hit = memory.object(forKey: url as NSURL) { return hit }
        if missing.contains(url) { return nil }
        if let task = running[url] { return await task.value }
        let task = Task<UIImage?, Never> {
            var req = URLRequest(url: url)
            let c = api.cookieHeader
            if !c.isEmpty { req.setValue(c, forHTTPHeaderField: "Cookie") }
            req.setValue("1", forHTTPHeaderField: "X-Nodeyard")
            guard let (data, resp) = try? await URLSession.shared.data(for: req),
                  (resp as? HTTPURLResponse)?.statusCode == 200,
                  let img = UIImage(data: data) else { return nil }
            return img.preparingThumbnail(of: CGSize(width: 600, height: 600)) ?? img
        }
        running[url] = task
        let img = await task.value
        running[url] = nil
        if let img { memory.setObject(img, forKey: url as NSURL) } else { missing.insert(url) }
        return img
    }
}

struct RemoteImage<Placeholder: View>: View {
    let url: URL?
    let api: NodeyardAPI?
    @ViewBuilder var placeholder: Placeholder
    @State private var image: UIImage?

    var body: some View {
        ZStack {
            if let image {
                Image(uiImage: image).resizable().scaledToFill().transition(.opacity)
            } else {
                placeholder
            }
        }
        .task(id: url) {
            image = nil
            guard let url, let api else { return }
            let img = await ImageCache.shared.image(url, api: api)
            withAnimation(.easeOut(duration: 0.2)) { image = img }
        }
    }
}
