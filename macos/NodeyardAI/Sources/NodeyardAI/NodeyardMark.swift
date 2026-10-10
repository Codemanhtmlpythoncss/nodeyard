import SwiftUI

struct NodeyardMark: View {
    var size: CGFloat = 26

    var body: some View {
        Canvas { context, canvasSize in
            let rect = CGRect(origin: .zero, size: canvasSize)
            let corner = RoundedRectangle(cornerRadius: size * 0.27, style: .continuous).path(in: rect)
            context.fill(corner, with: .linearGradient(
                Gradient(colors: [Color(red: 0.486, green: 0.549, blue: 1), Color(red: 0.133, green: 0.827, blue: 0.933)]),
                startPoint: .zero,
                endPoint: CGPoint(x: size, y: size)
            ))

            let nodes = [
                CGPoint(x: size * 0.25, y: size * 0.25),
                CGPoint(x: size * 0.75, y: size / 3),
                CGPoint(x: size * 0.375, y: size * 0.75),
            ]
            var links = Path()
            links.move(to: CGPoint(x: size / 3, y: size * 7 / 24)); links.addLine(to: CGPoint(x: size * 2 / 3, y: size / 3))
            links.move(to: CGPoint(x: size * 7 / 24, y: size / 3)); links.addLine(to: CGPoint(x: size * 3 / 8, y: size * 2 / 3))
            links.move(to: CGPoint(x: size * 17 / 24, y: size * 5 / 12)); links.addLine(to: CGPoint(x: size * 11 / 24, y: size * 17 / 24))
            let line = max(1, size * 2 / 24)
            context.stroke(links, with: .color(.white.opacity(0.92)), style: StrokeStyle(lineWidth: line, lineCap: .round))

            for point in nodes {
                let diameter = size * 4.4 / 24
                let node = CGRect(x: point.x - diameter / 2, y: point.y - diameter / 2, width: diameter, height: diameter)
                context.stroke(Path(ellipseIn: node), with: .color(.white.opacity(0.92)), lineWidth: line)
            }
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }
}
