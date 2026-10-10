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
        CGColor(red: 0.486, green: 0.549, blue: 1, alpha: 1), // website accent #7c8cff
        CGColor(red: 0.133, green: 0.827, blue: 0.933, alpha: 1), // website accent #22d3ee
    ] as CFArray
    let gradient = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(), colors: colors, locations: [0, 1])!
    let tile = CGPath(roundedRect: CGRect(x: 0, y: 0, width: 1024, height: 1024), cornerWidth: 256, cornerHeight: 256, transform: nil)
    context.addPath(tile)
    context.clip()
    context.drawLinearGradient(gradient, start: CGPoint(x: 0, y: 1024), end: CGPoint(x: 1024, y: 0), options: [.drawsBeforeStartLocation, .drawsAfterEndLocation])

    let nodes = [CGPoint(x: 256, y: 768), CGPoint(x: 768, y: 683), CGPoint(x: 384, y: 256)]
    context.setStrokeColor(NSColor.white.withAlphaComponent(0.88).cgColor)
    context.setLineWidth(86)
    context.setLineCap(.round)
    context.setLineJoin(.round)
    context.addLines(between: [CGPoint(x: 341, y: 725), CGPoint(x: 683, y: 683)])
    context.addLines(between: [CGPoint(x: 299, y: 683), CGPoint(x: 384, y: 341)])
    context.addLines(between: [CGPoint(x: 725, y: 597), CGPoint(x: 469, y: 299)])
    context.strokePath()

    for point in nodes {
        let radius: CGFloat = 94
        context.strokeEllipse(in: CGRect(x: point.x - radius, y: point.y - radius, width: radius * 2, height: radius * 2))
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
