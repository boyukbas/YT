import AVFoundation

enum SilenceDetector {
    struct Settings: Equatable {
        var thresholdDB: Double = -38   // quieter than this counts as silence
        var minSilence: Double = 0.4    // shortest pause that gets cut (seconds)
        var padding: Double = 0.12      // breathing room kept around speech (seconds)
    }

    /// Length of one analysis window in seconds.
    static let hop = 0.02

    /// Reads the audio once and returns loudness (dBFS) for every 20 ms window.
    static func levels(url: URL) async throws -> [Float] {
        let samples = try await AudioLoader.samples(url: url)
        let window = Int(Double(AudioLoader.sampleRate) * hop)
        guard window > 0 else { return [] }
        var levels: [Float] = []
        levels.reserveCapacity(samples.count / window)
        var i = 0
        while i + window <= samples.count {
            var sum: Float = 0
            for j in i..<(i + window) { sum += samples[j] * samples[j] }
            let rms = (sum / Float(window)).squareRoot()
            levels.append(20 * log10(max(rms, 1e-7)))
            i += window
        }
        return levels
    }

    /// Turns loudness levels into time ranges that should be removed.
    static func silences(in levels: [Float], settings: Settings) -> [TimeRange] {
        var ranges: [TimeRange] = []
        var runStart: Int?

        func close(_ s: Int, _ e: Int) {
            let start = Double(s) * hop
            let end = Double(e) * hop
            guard end - start >= settings.minSilence else { return }
            let a = s == 0 ? 0 : start + settings.padding
            let b = e == levels.count ? end : end - settings.padding
            if b - a > 0.05 { ranges.append(TimeRange(start: a, end: b)) }
        }

        for (i, db) in levels.enumerated() {
            if Double(db) < settings.thresholdDB {
                if runStart == nil { runStart = i }
            } else if let s = runStart {
                close(s, i)
                runStart = nil
            }
        }
        if let s = runStart { close(s, levels.count) }
        return ranges
    }

    /// Picks a threshold between the room noise and the speech level.
    static func suggestedThreshold(levels: [Float]) -> Double? {
        guard levels.count > 50 else { return nil }
        let sorted = levels.sorted()
        let noise = Double(sorted[Int(Double(sorted.count) * 0.1)])
        let speech = Double(sorted[Int(Double(sorted.count) * 0.9)])
        return min(-20, max(-60, (noise + (speech - noise) * 0.35).rounded()))
    }
}
