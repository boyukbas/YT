import AVFoundation
import WhisperKit

/// Which Whisper model runs on the phone. Downloaded once, then works offline.
enum SpeechModel: String, CaseIterable, Identifiable {
    case fast = "openai_whisper-small_216MB"
    case best = "openai_whisper-large-v3-v20240930_turbo_632MB"

    var id: String { rawValue }
    var label: String { self == .fast ? "Fast" : "Best" }
    var downloadSize: String { self == .fast ? "~220 MB" : "~630 MB" }
}

/// On-device subtitles with WhisperKit, including videos that mix two languages
/// (e.g. German sentences with Turkish word explanations).
///
/// How the mixed-language part works:
/// 1. Transcribe everything in the main language (gives words + timings).
/// 2. Split that into short phrases at punctuation and pauses.
/// 3. Re-listen to each phrase twice, forced to the main and to the second language.
///    Whichever language Whisper is more confident in wins; second-language phrases
///    get their text and timings from the second pass.
@MainActor
final class Transcriber {
    static let shared = Transcriber()

    private var kit: WhisperKit?
    private var loadedModel: SpeechModel?

    /// Margin the second language must win by (Whisper avg log-prob). Keeps the main language as default.
    private let switchMargin: Float = 0.15

    // MARK: Languages

    nonisolated static let languageCodes: [String] = Array(Set(Constants.languages.values))
        .sorted { displayName($0) < displayName($1) }

    nonisolated static func displayName(_ code: String) -> String {
        (Locale.current.localizedString(forLanguageCode: code) ?? code).capitalized
    }

    // MARK: Model

    private func folderKey(_ model: SpeechModel) -> String { "modelFolder." + model.rawValue }

    func isDownloaded(_ model: SpeechModel) -> Bool {
        guard let path = UserDefaults.standard.string(forKey: folderKey(model)) else { return false }
        return FileManager.default.fileExists(atPath: path)
    }

    private func prepare(_ model: SpeechModel, status: @escaping (String) -> Void) async throws -> WhisperKit {
        if let kit, loadedModel == model { return kit }
        kit = nil
        loadedModel = nil

        let folder: String
        if isDownloaded(model), let saved = UserDefaults.standard.string(forKey: folderKey(model)) {
            folder = saved
        } else {
            status("Downloading speech model (\(model.downloadSize), one time)…")
            let url = try await WhisperKit.download(variant: model.rawValue, progressCallback: { progress in
                let pct = Int(progress.fractionCompleted * 100)
                Task { @MainActor in status("Downloading speech model (\(model.downloadSize), one time)… \(pct)%") }
            })
            folder = url.path
            UserDefaults.standard.set(folder, forKey: folderKey(model))
        }

        status("Loading speech model… (first time takes a minute)")
        let config = WhisperKitConfig(modelFolder: folder, verbose: false, logLevel: .error,
                                      prewarm: true, load: true, download: false)
        let newKit = try await WhisperKit(config)
        kit = newKit
        loadedModel = model
        return newKit
    }

    // MARK: Transcription

    /// Returns words (original-video time) tagged with their language.
    func transcribe(videoURL: URL, primary: String, secondary: String?, model: SpeechModel,
                    status: @escaping (String) -> Void) async throws -> [Word] {
        let samples = try await Task.detached(priority: .userInitiated) {
            try await AudioLoader.samples(url: videoURL)
        }.value
        guard !samples.isEmpty else { return [] }

        let kit = try await prepare(model, status: status)

        status("Listening in \(Self.displayName(primary))…")
        let main = try await kit.transcribe(audioArray: samples, decodeOptions: Self.options(primary))
        var words = Self.words(from: main, offset: 0, lang: primary)

        guard let secondary, !secondary.isEmpty, secondary != primary, !words.isEmpty else { return words }

        status("Looking for \(Self.displayName(secondary)) words…")
        let phrases = Self.phrases(words)
        let sampleRate = Double(WhisperKit.sampleRate)
        let clips: [(start: Double, end: Double, audio: [Float])] = phrases.map { p in
            let s = max(0, p.first!.start - 0.15)
            let e = min(Double(samples.count) / sampleRate, p.last!.end + 0.15)
            let a = min(Int(s * sampleRate), samples.count - 1)
            let b = max(a + 1, min(samples.count, Int(e * sampleRate)))
            return (s, e, Array(samples[a..<b]))
        }
        let audios = clips.map(\.audio)
        let asSecondary = await kit.transcribe(audioArrays: audios, decodeOptions: Self.options(secondary, workers: 4))
        let asPrimary = await kit.transcribe(audioArrays: audios, decodeOptions: Self.options(primary, workers: 4))

        words = []
        for (i, phrase) in phrases.enumerated() {
            let sec = asSecondary[i] ?? []
            let pri = asPrimary[i] ?? []
            let secText = sec.map(\.text).joined().trimmingCharacters(in: .whitespacesAndNewlines)
            if !secText.isEmpty, let ss = Self.score(sec), ss > (Self.score(pri) ?? -.infinity) + switchMargin {
                var replaced = Self.words(from: sec, offset: clips[i].start, lang: secondary)
                    .map { w -> Word in
                        var w = w
                        w.start = min(max(w.start, clips[i].start), clips[i].end)
                        w.end = min(max(w.end, w.start), clips[i].end)
                        return w
                    }
                if replaced.isEmpty {
                    replaced = [Word(text: secText, start: phrase.first!.start, end: phrase.last!.end, lang: secondary)]
                }
                words += replaced
            } else {
                words += phrase
            }
        }
        return words.sorted { $0.start < $1.start }
    }

    private static func options(_ lang: String, workers: Int? = nil) -> DecodingOptions {
        DecodingOptions(task: .transcribe, language: lang, temperature: 0,
                        usePrefillPrompt: true, detectLanguage: false,
                        skipSpecialTokens: true, wordTimestamps: true,
                        concurrentWorkerCount: workers)
    }

    /// Average confidence of a result (higher = better), weighted by text length.
    private static func score(_ results: [TranscriptionResult]) -> Float? {
        let segs = results.flatMap(\.segments).filter {
            !$0.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        }
        guard !segs.isEmpty else { return nil }
        let weights = segs.map { Float(max(1, $0.text.count)) }
        let total = zip(segs, weights).reduce(Float(0)) { $0 + $1.0.avgLogprob * $1.1 }
        return total / weights.reduce(0, +)
    }

    private static func words(from results: [TranscriptionResult], offset: Double, lang: String) -> [Word] {
        var out: [Word] = []
        for seg in results.flatMap(\.segments) {
            if let timed = seg.words, !timed.isEmpty {
                for w in timed {
                    let text = w.word.trimmingCharacters(in: .whitespacesAndNewlines)
                    guard !text.isEmpty else { continue }
                    out.append(Word(text: text, start: offset + Double(w.start),
                                    end: offset + Double(w.end), lang: lang))
                }
            } else {
                // No word timings: spread the segment's words evenly over its time.
                let parts = seg.text.split(whereSeparator: \.isWhitespace).map(String.init)
                guard !parts.isEmpty else { continue }
                let step = Double(seg.end - seg.start) / Double(parts.count)
                for (i, p) in parts.enumerated() {
                    let s = offset + Double(seg.start) + step * Double(i)
                    out.append(Word(text: p, start: s, end: s + step, lang: lang))
                }
            }
        }
        return out
    }

    /// Groups words into short phrases: breaks after punctuation or at pauses.
    private static func phrases(_ words: [Word]) -> [[Word]] {
        var result: [[Word]] = []
        var current: [Word] = []
        for (i, w) in words.enumerated() {
            current.append(w)
            let endsWithPunct = w.text.last.map { ".!?,;:–-".contains($0) } ?? false
            let gapToNext = i + 1 < words.count ? words[i + 1].start - w.end : .infinity
            if endsWithPunct || gapToNext > 0.25 {
                result.append(current)
                current = []
            }
        }
        if !current.isEmpty { result.append(current) }
        return result
    }
}

/// Reads a video's audio as 16 kHz mono floats (the format Whisper wants).
enum AudioLoader {
    static let sampleRate = 16_000

    static func samples(url: URL) async throws -> [Float] {
        let asset = AVURLAsset(url: url)
        guard let track = try await asset.loadTracks(withMediaType: .audio).first else { return [] }

        let reader = try AVAssetReader(asset: asset)
        let output = AVAssetReaderTrackOutput(track: track, outputSettings: [
            AVFormatIDKey: kAudioFormatLinearPCM,
            AVLinearPCMBitDepthKey: 32,
            AVLinearPCMIsFloatKey: true,
            AVLinearPCMIsBigEndianKey: false,
            AVLinearPCMIsNonInterleaved: false,
            AVNumberOfChannelsKey: 1,
            AVSampleRateKey: sampleRate,
        ])
        output.alwaysCopiesSampleData = false
        guard reader.canAdd(output) else { throw EditorError.audioReadFailed }
        reader.add(output)
        guard reader.startReading() else { throw reader.error ?? EditorError.audioReadFailed }

        var samples: [Float] = []
        while let sample = output.copyNextSampleBuffer() {
            guard let block = CMSampleBufferGetDataBuffer(sample) else { continue }
            let length = CMBlockBufferGetDataLength(block)
            var chunk = [Float](repeating: 0, count: length / MemoryLayout<Float>.size)
            chunk.withUnsafeMutableBytes { raw in
                if let base = raw.baseAddress {
                    _ = CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: raw.count, destination: base)
                }
            }
            samples.append(contentsOf: chunk)
        }
        if reader.status == .failed { throw reader.error ?? EditorError.audioReadFailed }
        return samples
    }
}

enum CaptionBuilder {
    /// Groups words into short, punchy Shorts-style captions. A caption never mixes languages,
    /// so each one can get its language's color.
    static func build(from words: [Word], maxWords: Int) -> [Caption] {
        var captions: [Caption] = []
        var current: [Word] = []

        func flush() {
            guard let first = current.first, let last = current.last else { return }
            captions.append(Caption(start: first.start, end: last.end,
                                    text: current.map(\.text).joined(separator: " "),
                                    lang: first.lang))
            current.removeAll()
        }

        for w in words {
            if let last = current.last,
               w.lang != last.lang || w.start - last.end > 0.6 || current.count >= maxWords { flush() }
            current.append(w)
            if let ch = w.text.last, ".!?,;:".contains(ch) { flush() }
        }
        flush()

        // Close tiny gaps and give very short captions a minimum screen time.
        for i in captions.indices {
            let next = i + 1 < captions.count ? captions[i + 1].start : Double.infinity
            if next - captions[i].end < 0.4 { captions[i].end = min(next, captions[i].end + 0.4) }
            if captions[i].end - captions[i].start < 0.35 {
                captions[i].end = min(captions[i].start + 0.35, next)
            }
        }
        return captions
    }
}
