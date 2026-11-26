import SwiftUI

/// The nodeyard mark: three machines joined into one cluster. When animated, the links draw in and then each node
/// lights up, one after another. It's the app's one flourish, used on first launch and on the lock screen.
struct NodeMark: View {
    var size: CGFloat = 96
    var animated = true
    @State private var linked: CGFloat = 0
    @State private var lit = [false, false, false]
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    // node centres from the app icon (1024 × 1024, y downwards)
    private static let nodes: [CGPoint] = [CGPoint(x: 286, y: 709), CGPoint(x: 740, y: 627), CGPoint(x: 416, y: 283)]

    var body: some View {
        let scale = size / 1024
        ZStack {
            Links().trim(from: 0, to: linked)
                .stroke(Color.white.opacity(0.9), style: StrokeStyle(lineWidth: 38 * scale, lineCap: .round, lineJoin: .round))
            ForEach(0..<3, id: \.self) { i in
                let p = Self.nodes[i]
                ZStack {
                    Circle().fill(.white)
                        .shadow(color: Theme.teal.opacity(lit[i] ? 0.8 : 0), radius: 40 * scale)
                    Circle().fill(Theme.indigo).frame(width: 46 * scale, height: 46 * scale)
                }
                .frame(width: 144 * scale, height: 144 * scale)
                .scaleEffect(lit[i] ? 1 : 0.2)
                .opacity(lit[i] ? 1 : 0)
                .position(x: p.x * scale, y: p.y * scale)
            }
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
        .onAppear(perform: play)
    }

    private func play() {
        guard animated, !reduceMotion else {
            linked = 1
            lit = [true, true, true]
            return
        }
        withAnimation(.easeInOut(duration: 0.7)) { linked = 1 }
        for i in 0..<3 {
            withAnimation(.spring(response: 0.42, dampingFraction: 0.6).delay(0.35 + Double(i) * 0.14)) { lit[i] = true }
        }
    }

    private struct Links: Shape {
        func path(in rect: CGRect) -> Path {
            let s = rect.width / 1024
            let p = NodeMark.nodes.map { CGPoint(x: $0.x * s, y: $0.y * s) }
            var path = Path()
            path.move(to: p[0]); path.addLine(to: p[1]); path.addLine(to: p[2]); path.closeSubpath()
            return path
        }
    }
}

/// The mark on its gradient tile, like the app icon.
struct NodeTile: View {
    var size: CGFloat = 112
    var animated = true
    var body: some View {
        RoundedRectangle(cornerRadius: size * 0.27, style: .continuous)
            .fill(Theme.brand)
            .overlay(NodeMark(size: size, animated: animated))
            .frame(width: size, height: size)
            .shadow(color: Theme.blue.opacity(0.45), radius: 30, y: 12)
    }
}
