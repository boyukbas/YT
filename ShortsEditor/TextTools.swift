import Foundation

/// Word-list and script helpers (same logic as the web version).
enum TextTools {
    /// Lowercase, no accents or punctuation: "Ormanda." ~ "ormanda".
    static func fold(_ s: String) -> String {
        let lower = s.lowercased(with: Locale(identifier: "tr"))
            .replacingOccurrences(of: "ı", with: "i")
            .replacingOccurrences(of: "ß", with: "ss")
            .folding(options: .diacriticInsensitive, locale: nil)
        return String(lower.unicodeScalars.filter { CharacterSet.alphanumerics.contains($0) || $0 == " " })
            .trimmingCharacters(in: .whitespaces)
    }

    static func similarity(_ a: String, _ b: String) -> Double {
        let x = Array(fold(a)), y = Array(fold(b))
        guard !x.isEmpty, !y.isEmpty else { return 0 }
        var prev = Array(0...y.count)
        for i in 1...x.count {
            var cur = [i] + Array(repeating: 0, count: y.count)
            for j in 1...y.count {
                cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x[i - 1] == y[j - 1] ? 0 : 1))
            }
            prev = cur
        }
        return 1 - Double(prev[y.count]) / Double(max(x.count, y.count))
    }

    static func trailingPunct(_ s: String) -> String {
        String(s.reversed().prefix { ".!?,;:…".contains($0) }.reversed())
    }

    /// "der Wald – orman" per line (also "=", ":", "→", tab or " - ").
    static func parseGlossary(_ text: String) -> (primary: [String], secondary: [String]) {
        var p: [String] = [], s: [String] = []
        for raw in text.split(whereSeparator: { $0 == "\n" || $0 == ";" }) {
            let line = raw.trimmingCharacters(in: .whitespaces)
            guard !line.isEmpty else { continue }
            let parts = line.components(separatedBy: CharacterSet(charactersIn: "–—=→:\t"))
                .flatMap { $0.components(separatedBy: " - ") }
                .map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
            if parts.count >= 2 { p.append(parts[0]); s.append(parts[1...].joined(separator: " ")) }
            else if let one = parts.first { p.append(one); s.append(one) }
        }
        return (Array(NSOrderedSet(array: p)) as? [String] ?? p, Array(NSOrderedSet(array: s)) as? [String] ?? s)
    }

    /// Replaces misheard words with look-alike word-list terms (one or two words at a time).
    static func snap(_ words: [Word], to terms: [String], lang: String) -> [Word] {
        guard !terms.isEmpty else { return words }
        var out = words
        var i = 0
        while i < out.count {
            var best: (score: Double, n: Int, term: String)?
            for n in [2, 1] where i + n <= out.count {
                let span = out[i..<(i + n)].map(\.text).joined(separator: " ")
                for term in terms {
                    let sc = similarity(span, term)
                    if sc >= 0.6, fold(span) != fold(term), sc > (best?.score ?? 0) { best = (sc, n, term) }
                    if fold(span) == fold(term) { best = nil; break }
                }
            }
            if let b = best {
                let first = out[i], last = out[i + b.n - 1]
                var text = b.term
                if first.text.first?.isUppercase == true { text = text.prefix(1).uppercased(with: Locale(identifier: lang)) + text.dropFirst() }
                text += trailingPunct(last.text)
                out.replaceSubrange(i..<(i + b.n), with: [Word(text: text, start: first.start, end: last.end, lang: lang)])
            }
            i += 1
        }
        return out
    }

    /// Puts the user's own script on the recognized timings (fuzzy, in order).
    /// Line breaks in the script become caption breaks.
    static func alignScript(_ script: String, words: [Word], primary: String, secondary: String) -> [Word] {
        var tokens: [(text: String, lineEnd: Bool)] = []
        for line in script.components(separatedBy: .newlines) {
            let parts = line.split(whereSeparator: \.isWhitespace).map(String.init)
            for (i, p) in parts.enumerated() { tokens.append((p, i == parts.count - 1)) }
        }
        guard !tokens.isEmpty, !words.isEmpty else { return words }
        let S = tokens.count, R = words.count, gap: Float = -0.4
        var sim = [[Float]](repeating: [Float](repeating: 0, count: R), count: S)
        for i in 0..<S { for j in 0..<R { sim[i][j] = Float(similarity(tokens[i].text, words[j].text)) } }
        var score = [[Float]](repeating: [Float](repeating: 0, count: R + 1), count: S + 1)
        var move = [[UInt8]](repeating: [UInt8](repeating: 0, count: R + 1), count: S + 1)
        for i in 1...S { score[i][0] = Float(i) * gap; move[i][0] = 2 }
        // Skipping recognized words before the script starts is free (the script may cover only part).
        for j in 1...R { score[0][j] = 0; move[0][j] = 3 }
        for i in 1...S {
            for j in 1...R {
                let s = sim[i - 1][j - 1]
                let d = score[i - 1][j - 1] + (s >= 0.5 ? s * 2 : -1)
                let u = score[i - 1][j] + gap, l = score[i][j - 1] + gap
                if d >= u && d >= l { score[i][j] = d; move[i][j] = 1 }
                else if u >= l { score[i][j] = u; move[i][j] = 2 }
                else { score[i][j] = l; move[i][j] = 3 }
            }
        }
        var match = [Int](repeating: -1, count: S)
        // ...and so is skipping recognized words after it ends.
        var i = S, j = (0...R).max { score[S][$0] < score[S][$1] } ?? R
        while i > 0 || j > 0 {
            switch move[i][j] {
            case 1: if sim[i - 1][j - 1] >= 0.5 { match[i - 1] = j - 1 }; i -= 1; j -= 1
            case 2: i -= 1
            default: j -= 1
            }
        }
        var out: [(text: String, lineEnd: Bool, start: Double?, end: Double?, lang: String?)] =
            tokens.enumerated().map { k, t in
                let w = match[k] >= 0 ? words[match[k]] : nil
                return (t.text, t.lineEnd, w?.start, w?.end, w?.lang)
            }
        // Times for unmatched runs, spread between matched neighbours.
        var k = 0
        while k < S {
            if out[k].start != nil { k += 1; continue }
            var e = k
            while e < S && out[e].start == nil { e += 1 }
            let from = k > 0 ? out[k - 1].end! : words[0].start
            let to = e < S ? out[e].start! : words[R - 1].end
            let step = max(0.05, (to - from) / Double(e - k))
            for m in k..<e { out[m].start = from + step * Double(m - k); out[m].end = out[m].start! + step }
            k = e
        }
        // Language: matched word, else letters only one language uses, else a neighbour.
        func hint(_ t: String) -> String? {
            let trOnly = t.rangeOfCharacter(from: CharacterSet(charactersIn: "ğıışçİĞŞÇ")) != nil
            let deOnly = t.rangeOfCharacter(from: CharacterSet(charactersIn: "äßÄ")) != nil
            if trOnly { return [primary, secondary].first { $0 == "tr" } }
            if deOnly { return [primary, secondary].first { $0 == "de" } }
            return nil
        }
        for m in 0..<S where out[m].lang == nil { out[m].lang = hint(out[m].text) }
        for m in 0..<S where out[m].lang == nil {
            let startsNew = m == 0 || out[m - 1].lineEnd || (out[m - 1].text.last.map { ".!?–—-".contains($0) } ?? false)
            if startsNew, let next = out[(m + 1)...].first(where: { $0.lang != nil }) { out[m].lang = next.lang; continue }
            out[m].lang = out[..<m].last(where: { $0.lang != nil })?.lang
                ?? out[(m + 1)...].first(where: { $0.lang != nil })?.lang ?? primary
        }
        var result: [Word] = []
        for t in out {
            var start = t.start!
            if let last = result.last, start < last.start { start = last.start }
            result.append(Word(text: t.text, start: start, end: max(t.end!, start + 0.05), lang: t.lang!, lineEnd: t.lineEnd))
        }
        return result
    }
}
