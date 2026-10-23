import UIKit

enum CaptionLayout: String, CaseIterable, Identifiable {
    case single, pair
    var id: String { rawValue }
    var label: String { self == .single ? "One line at a time" : "Word + translation" }
}

/// Everything about how subtitles look. Shared by the preview and the export.
struct CaptionLook {
    var primaryLang: String
    var primaryColor: UIColor
    var secondaryColor: UIColor
    var position: Double   // 0 = bottom edge, 1 = top edge
    var size: Double       // text height as a fraction of the video's short side
    var box: Bool
    var highlight: Bool
    var layout: CaptionLayout

    func color(for lang: String) -> UIColor { lang == primaryLang ? primaryColor : secondaryColor }
}

enum CaptionRenderer {
    private static let pairGap = 1.0

    /// Captions on screen at time t. "pair": a main-language line stays up and its
    /// translation appears under it.
    static func blocks(at t: Double, in caps: [Caption], look: CaptionLook) -> [Caption] {
        let i = caps.firstIndex { t >= $0.start && t < $0.end && !$0.text.isEmpty }
        guard look.layout == .pair else { return i.map { [caps[$0]] } ?? [] }
        func isPair(_ p: Int, _ n: Int) -> Bool {
            p >= 0 && n < caps.count && caps[p].lang == look.primaryLang && caps[n].lang != look.primaryLang
                && caps[n].start - caps[p].end <= pairGap
        }
        if let i {
            if caps[i].lang != look.primaryLang && isPair(i - 1, i) { return [caps[i - 1], caps[i]] }
            return [caps[i]]
        }
        if let p = caps.lastIndex(where: { $0.end <= t }), isPair(p, p + 1), t < caps[p + 1].start { return [caps[p]] }
        return []
    }

    /// Identifies what's drawn, so identical frames can share one image.
    static func key(_ blocks: [Caption], at t: Double, look: CaptionLook) -> String {
        blocks.map { c in
            let active = look.highlight ? (c.timedWords.firstIndex { t >= $0.start && t < $0.end } ?? -1) : -1
            return "\(c.id)|\(c.text)|\(c.lang)|\(active)"
        }.joined(separator: "#")
    }

    /// Draws the blocks into a transparent image as wide as the canvas.
    /// Returns the image and where it goes on the canvas (top-left origin).
    static func render(_ blocks: [Caption], at t: Double, canvas: CGSize, look: CaptionLook,
                       scale: CGFloat = 1) -> (image: UIImage, frame: CGRect)? {
        guard !blocks.isEmpty, canvas.width > 0, canvas.height > 0 else { return nil }
        let px = min(canvas.width, canvas.height) * look.size
        let font = UIFont.systemFont(ofSize: px, weight: .heavy)
        let lh = px * 1.18, maxW = canvas.width * 0.86, blockGap = px * 0.3
        let space = (" " as NSString).size(withAttributes: [.font: font]).width + px * 0.12

        struct LaidWord { let text: String; let width: CGFloat; let start: Double; let end: Double }
        struct Line { var words: [LaidWord] = []; var width: CGFloat = 0 }
        let laid: [(color: UIColor, lines: [Line])] = blocks.map { c in
            var lines: [Line] = [], cur = Line()
            for w in c.timedWords {
                let width = (w.text as NSString).size(withAttributes: [.font: font]).width
                let add = cur.words.isEmpty ? width : space + width
                if !cur.words.isEmpty && cur.width + add > maxW { lines.append(cur); cur = Line() }
                cur.width += cur.words.isEmpty ? width : space + width
                cur.words.append(LaidWord(text: w.text, width: width, start: w.start, end: w.end))
            }
            if !cur.words.isEmpty { lines.append(cur) }
            return (look.color(for: c.lang), lines)
        }
        let lineCount = laid.reduce(0) { $0 + $1.lines.count }
        guard lineCount > 0 else { return nil }
        let pad = px * 0.4
        let total = CGFloat(lineCount) * lh + blockGap * CGFloat(laid.count - 1)
        let size = CGSize(width: canvas.width, height: total + pad * 2)

        let format = UIGraphicsImageRendererFormat()
        format.scale = scale
        format.opaque = false
        let image = UIGraphicsImageRenderer(size: size, format: format).image { ctx in
            let cg = ctx.cgContext
            var y = pad + lh / 2
            for block in laid {
                for line in block.lines {
                    var x = (canvas.width - line.width) / 2
                    if look.box {
                        let padX = px * 0.28, padY = px * 0.1
                        UIColor(white: 0, alpha: 0.62).setFill()
                        UIBezierPath(roundedRect: CGRect(x: x - padX, y: y - lh / 2 - padY / 2,
                                                         width: line.width + padX * 2, height: lh + padY),
                                     cornerRadius: px * 0.22).fill()
                    }
                    for w in line.words {
                        let active = t >= w.start && t < w.end
                        let alpha: CGFloat = look.highlight && !active ? 0.55 : 1
                        let textH = font.lineHeight
                        let origin = CGPoint(x: x, y: y - textH / 2)
                        cg.saveGState()
                        if look.highlight && active {
                            // The spoken word pops a little.
                            cg.translateBy(x: x + w.width / 2, y: y)
                            cg.scaleBy(x: 1.05, y: 1.05)
                            cg.translateBy(x: -(x + w.width / 2), y: -y)
                        }
                        if !look.box {
                            NSAttributedString(string: w.text, attributes: [
                                .font: font, .foregroundColor: UIColor.clear,
                                .strokeColor: UIColor(white: 0, alpha: alpha), .strokeWidth: 16,
                            ]).draw(at: origin)
                        }
                        NSAttributedString(string: w.text, attributes: [
                            .font: font, .foregroundColor: block.color.withAlphaComponent(alpha),
                        ]).draw(at: origin)
                        cg.restoreGState()
                        x += w.width + space
                    }
                    y += lh
                }
                y += blockGap
            }
        }
        let frame = CGRect(x: 0, y: canvas.height * (1 - look.position) - size.height / 2,
                           width: size.width, height: size.height)
        return (image, frame)
    }

    /// Splits the timeline into stretches where the subtitle picture doesn't change.
    static func segments(_ caps: [Caption], total: Double, look: CaptionLook)
        -> [(start: Double, end: Double, key: String, blocks: [Caption], t: Double)] {
        var times = Set<Double>([0, total])
        for c in caps {
            times.insert(c.start); times.insert(c.end)
            if look.highlight { for w in c.timedWords { times.insert(w.start); times.insert(w.end) } }
        }
        let sorted = times.filter { $0 >= 0 && $0 <= total }.sorted()
        var out: [(start: Double, end: Double, key: String, blocks: [Caption], t: Double)] = []
        for (a, b) in zip(sorted, sorted.dropFirst()) where b - a > 0.001 {
            let mid = (a + b) / 2
            let shown = blocks(at: mid, in: caps, look: look)
            guard !shown.isEmpty else { continue }
            let k = key(shown, at: mid, look: look)
            if let last = out.last, last.key == k, abs(last.end - a) < 0.0001 {
                out[out.count - 1].end = b
            } else {
                out.append((a, b, k, shown, mid))
            }
        }
        return out
    }
}
