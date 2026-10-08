import SwiftUI

struct NodeyardMark: View {
    var size: CGFloat = 26

    var body: some View {
        Canvas { context, canvasSize in
            let rect = CGRect(origin: .zero, size: canvasSize)
            let corner = RoundedRectangle(cornerRadius: size * 0.27, style: .continuous).path(in: rect)
            context.fill(corner, with: .linearGradient(
                Gradient(colors: [Color(red: 0.47, green: 0.54, blue: 1), Color(red: 0.11, green: 0.72, blue: 0.83)]),
                startPoint: .zero,
                endPoint: CGPoint(x: size, y: size)
            ))

            let nodes = [
                CGPoint(x: size * 0.29, y: size * 0.28),
                CGPoint(x: size * 0.72, y: size * 0.36),
                CGPoint(x: size * 0.40, y: size * 0.73),
            ]
            var links = Path()
            links.move(to: nodes[0]); links.addLine(to: nodes[1])
            links.move(to: nodes[0]); links.addLine(to: nodes[2])
            links.move(to: nodes[1]); links.addLine(to: nodes[2])
            context.stroke(links, with: .color(.white.opacity(0.82)), style: StrokeStyle(lineWidth: size * 0.075, lineCap: .round))

            for point in nodes {
                let diameter = size * 0.23
                let node = CGRect(x: point.x - diameter / 2, y: point.y - diameter / 2, width: diameter, height: diameter)
                context.fill(Path(ellipseIn: node), with: .color(.white))
                let core = node.insetBy(dx: diameter * 0.34, dy: diameter * 0.34)
                context.fill(Path(ellipseIn: core), with: .color(Color(red: 0.24, green: 0.42, blue: 0.94)))
            }
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }
}
