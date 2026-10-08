import AppKit
import CoreGraphics
import Foundation

guard CommandLine.arguments.count == 2 else {
    fputs("Usage: build-nodeyard-ai-icon.swift OUTPUT.iconset\n", stderr)
    exit(2)
}

let iconset = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
try FileManager.default.createDirectory(at: iconset, withIntermediateDirectories: true)

func renderIcon(pixels: Int, to url: URL) throws {
    guard let bitmap = NSBitmapImageRep(
        bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0
    ), let graphics = NSGraphicsContext(bitmapImageRep: bitmap) else {
        throw NSError(domain: "NodeyardAIIcon", code: 1, userInfo: [NSLocalizedDescriptionKey: "Could not create icon bitmap."])
    }

    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = graphics
    let context = graphics.cgContext
    let scale = CGFloat(pixels) / 1024
    context.scaleBy(x: scale, y: scale)
    context.setAllowsAntialiasing(true)
    context.setShouldAntialias(true)
    context.interpolationQuality = .high

    let colors = [
        CGColor(red: 0.36, green: 0.43, blue: 0.96, alpha: 1),
        CGColor(red: 0.16, green: 0.47, blue: 0.82, alpha: 1),
        CGColor(red: 0.07, green: 0.70, blue: 0.78, alpha: 1),
    ] as CFArray
    let gradient = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(), colors: colors, locations: [0, 0.54, 1])!
    context.drawLinearGradient(gradient, start: CGPoint(x: 70, y: 970), end: CGPoint(x: 940, y: 55), options: [.drawsBeforeStartLocation, .drawsAfterEndLocation])

    context.saveGState()
    context.setFillColor(CGColor(red: 0.85, green: 0.96, blue: 1, alpha: 0.11))
    context.fillEllipse(in: CGRect(x: 245, y: 255, width: 690, height: 690))
    context.setFillColor(CGColor(red: 0.12, green: 0.22, blue: 0.62, alpha: 0.10))
    context.fillEllipse(in: CGRect(x: -255, y: -245, width: 980, height: 980))
    context.restoreGState()

    let nodes = [CGPoint(x: 286, y: 315), CGPoint(x: 740, y: 397), CGPoint(x: 416, y: 741)]
    context.setStrokeColor(NSColor.white.withAlphaComponent(0.88).cgColor)
    context.setLineWidth(38)
    context.setLineCap(.round)
    context.setLineJoin(.round)
    context.addLines(between: [nodes[0], nodes[1]])
    context.addLines(between: [nodes[0], nodes[2]])
    context.addLines(between: [nodes[1], nodes[2]])
    context.strokePath()

    for point in nodes {
        let radius: CGFloat = 72
        context.setShadow(offset: CGSize(width: 0, height: -9), blur: 22, color: NSColor.black.withAlphaComponent(0.22).cgColor)
        context.setFillColor(NSColor.white.cgColor)
        context.fillEllipse(in: CGRect(x: point.x - radius, y: point.y - radius, width: radius * 2, height: radius * 2))
        context.setShadow(offset: .zero, blur: 0, color: nil)
        context.setFillColor(CGColor(red: 0.27, green: 0.40, blue: 0.88, alpha: 1))
        let core = radius * 0.32
        context.fillEllipse(in: CGRect(x: point.x - core, y: point.y - core, width: core * 2, height: core * 2))
    }

    graphics.flushGraphics()
    NSGraphicsContext.restoreGraphicsState()
    guard let png = bitmap.representation(using: .png, properties: [:]) else {
        throw NSError(domain: "NodeyardAIIcon", code: 2, userInfo: [NSLocalizedDescriptionKey: "Could not encode icon PNG."])
    }
    try png.write(to: url, options: .atomic)
}

let variants: [(Int, String)] = [
    (16, "icon_16x16.png"), (32, "icon_16x16@2x.png"),
    (32, "icon_32x32.png"), (64, "icon_32x32@2x.png"),
    (128, "icon_128x128.png"), (256, "icon_128x128@2x.png"),
    (256, "icon_256x256.png"), (512, "icon_256x256@2x.png"),
    (512, "icon_512x512.png"), (1024, "icon_512x512@2x.png"),
]
for (pixels, name) in variants {
    try renderIcon(pixels: pixels, to: iconset.appendingPathComponent(name))
}
