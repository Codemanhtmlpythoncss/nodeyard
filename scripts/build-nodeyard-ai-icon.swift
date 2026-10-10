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

    // The dashboard's favicon (share/nodeyard/dashboard/web/index.html), drawn on the macOS icon grid:
    // an 824 px rounded tile centred on the 1024 px canvas, with a soft shadow. SVG units (32 x 32,
    // y down) map onto the tile; CoreGraphics' y axis points up.
    let tileRect = CGRect(x: 100, y: 100, width: 824, height: 824)
    let unit = tileRect.width / 32
    func point(_ x: CGFloat, _ y: CGFloat) -> CGPoint { CGPoint(x: tileRect.minX + x * unit, y: 1024 - (tileRect.minY + y * unit)) }
    let tile = CGPath(roundedRect: tileRect, cornerWidth: 185, cornerHeight: 185, transform: nil)

    context.saveGState()
    context.setShadow(offset: CGSize(width: 0, height: -12), blur: 28, color: NSColor.black.withAlphaComponent(0.28).cgColor)
    context.addPath(tile)
    context.setFillColor(CGColor(red: 0.3, green: 0.6, blue: 0.95, alpha: 1))
    context.fillPath()
    context.restoreGState()

    let colors = [
        CGColor(red: 0.486, green: 0.549, blue: 1, alpha: 1), // website accent #7c8cff
        CGColor(red: 0.133, green: 0.827, blue: 0.933, alpha: 1), // website accent #22d3ee
    ] as CFArray
    let gradient = CGGradient(colorsSpace: CGColorSpaceCreateDeviceRGB(), colors: colors, locations: [0, 1])!
    context.saveGState()
    context.addPath(tile)
    context.clip()
    context.drawLinearGradient(gradient, start: point(0, 0), end: point(32, 32), options: [.drawsBeforeStartLocation, .drawsAfterEndLocation])
    context.restoreGState()

    // <g stroke="white" stroke-width="2" stroke-linecap="round" fill="none">
    context.setStrokeColor(NSColor.white.cgColor)
    context.setLineWidth(2 * unit)
    context.setLineCap(.round)
    context.setLineJoin(.round)
    for (cx, cy) in [(9.0, 10.0), (23.0, 12.0), (13.0, 23.0)] {   // <circle r="2.4">
        let c = point(cx, cy), r = 2.4 * unit
        context.strokeEllipse(in: CGRect(x: c.x - r, y: c.y - r, width: 2 * r, height: 2 * r))
    }
    // <path d="M11 10.5l10 1.2M10 12.4l2 8.2M21.8 14l-7 7.2"/>
    context.addLines(between: [point(11, 10.5), point(21, 11.7)])
    context.addLines(between: [point(10, 12.4), point(12, 20.6)])
    context.addLines(between: [point(21.8, 14), point(14.8, 21.2)])
    context.strokePath()

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
