import Foundation

struct TimeRange: Hashable, Identifiable, Codable {
    var start: Double
    var end: Double
    var id: String { "\(start)-\(end)" }
    var duration: Double { max(0, end - start) }
}

struct Word: Hashable, Codable {
    var text: String
    var start: Double
    var end: Double
    var lang: String          // Whisper language code, e.g. "de", "tr"
    var lineEnd = false       // last word of a line in the user's script
}

/// A word inside a caption, with its own timing (for highlighting the spoken word).
struct TimedWord: Hashable, Codable {
    var text: String
    var start: Double
    var end: Double
}

struct Caption: Identifiable, Hashable, Codable {
    var id = UUID()
    var start: Double
    var end: Double
    var text: String
    var lang: String          // decides the caption's color
    var words: [TimedWord] = []

    /// Word timings that still fit the (possibly edited) text; spread evenly if the word count changed.
    var timedWords: [TimedWord] {
        let parts = text.split(whereSeparator: \.isWhitespace).map(String.init)
        if words.count == parts.count {
            return zip(parts, words).map { TimedWord(text: $0, start: $1.start, end: $1.end) }
        }
        let step = (end - start) / Double(max(1, parts.count))
        return parts.enumerated().map { i, p in
            TimedWord(text: p, start: start + step * Double(i), end: start + step * Double(i + 1))
        }
    }
}

/// Maps times between the original video and the edited (cut) video.
struct TimelineMap {
    var kept: [TimeRange]

    var outputDuration: Double { kept.reduce(0) { $0 + $1.duration } }

    /// Original time -> edited time. Times inside removed parts snap to the next kept part.
    func toOutput(_ t: Double) -> Double {
        var acc = 0.0
        for r in kept {
            if t < r.start { return acc }
            if t <= r.end { return acc + (t - r.start) }
            acc += r.duration
        }
        return acc
    }

    /// Edited time -> original time.
    func toSource(_ o: Double) -> Double {
        var acc = 0.0
        for r in kept {
            if o <= acc + r.duration { return r.start + max(0, o - acc) }
            acc += r.duration
        }
        return kept.last?.end ?? 0
    }

    static func subtract(from base: TimeRange, removing removals: [TimeRange]) -> [TimeRange] {
        var result = [base]
        for cut in removals {
            result = result.flatMap { r -> [TimeRange] in
                if cut.end <= r.start || cut.start >= r.end { return [r] }
                var parts: [TimeRange] = []
                if cut.start > r.start { parts.append(TimeRange(start: r.start, end: cut.start)) }
                if cut.end < r.end { parts.append(TimeRange(start: cut.end, end: r.end)) }
                return parts
            }
        }
        return result.filter { $0.duration > 0.04 }
    }
}

enum EditorError: LocalizedError {
    case noVideoTrack, exportFailed, transcriptionFailed
    case photosPermission, audioReadFailed

    var errorDescription: String? {
        switch self {
        case .noVideoTrack: return "This file has no video track."
        case .exportFailed: return "Export failed."
        case .transcriptionFailed: return "Couldn't transcribe the audio."
        case .photosPermission: return "Allow adding to Photos for this app in Settings."
        case .audioReadFailed: return "Couldn't read the audio."
        }
    }
}

func formatTime(_ t: Double) -> String {
    guard t.isFinite, t >= 0 else { return "0:00.0" }
    let m = Int(t) / 60
    let s = t - Double(m * 60)
    return String(format: "%d:%04.1f", m, s)
}

enum SRTWriter {
    static func make(_ captions: [Caption]) -> String {
        captions.enumerated().map { i, c in
            "\(i + 1)\n\(stamp(c.start)) --> \(stamp(c.end))\n\(c.text)\n"
        }.joined(separator: "\n")
    }

    private static func stamp(_ t: Double) -> String {
        let ms = Int((t * 1000).rounded())
        return String(format: "%02d:%02d:%02d,%03d", ms / 3_600_000, (ms / 60_000) % 60, (ms / 1000) % 60, ms % 1000)
    }
}
