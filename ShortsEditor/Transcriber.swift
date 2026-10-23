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

    private var kits: [SpeechModel: WhisperKit] = [:]

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
        if let kit = kits[model] { return kit }
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
        kits[model] = newKit
        return newKit
    }

    // MARK: Transcription

    /// Returns words (original-video time) tagged with their language.
    /// - accurateSecondary: re-listen to second-language phrases with the Best model.
    /// - glossary: "der Wald – orman" lines; used as a hint and to correct look-alike words.
    /// - script: the user's own text; when given, only its timing is taken from the audio.
    func transcribe(videoURL: URL, primary: String, secondary: String?, model: SpeechModel,
                    accurateSecondary: Bool = false, glossary: String = "", script: String = "",
                    status: @escaping (String) -> Void) async throws -> [Word] {
        let samples = try await Task.detached(priority: .userInitiated) {
            try await AudioLoader.samples(url: videoURL)
        }.value
        guard !samples.isEmpty else { return [] }

        let kit = try await prepare(model, status: status)
        let terms = TextTools.parseGlossary(glossary)

        status("Listening in \(Self.displayName(primary))…")
        let main = try await kit.transcribe(audioArray: samples, decodeOptions: Self.options(primary))
        var words = Self.words(from: main, offset: 0, lang: primary)

        if let secondary, !secondary.isEmpty, secondary != primary, !words.isEmpty {
            status("Looking for \(Self.displayName(secondary)) words…")
            let phrases = Self.phrases(words)
            let sampleRate = Double(WhisperKit.sampleRate)
            let duration = Double(samples.count) / sampleRate
            let clips: [(start: Double, end: Double, audio: [Float])] = phrases.enumerated().map { i, p in
                // Pad a little, but never into the neighbouring phrases.
                let prevEnd = i > 0 ? phrases[i - 1].last!.end : 0
                let nextStart = i + 1 < phrases.count ? phrases[i + 1].first!.start : duration
                let s = max(0, prevEnd, p.first!.start - 0.15)
                let e = min(duration, nextStart, p.last!.end + 0.15)
                let a = min(Int(s * sampleRate), samples.count - 1)
                let b = max(a + 1, min(samples.count, Int(e * sampleRate)))
                return (s, e, Array(samples[a..<b]))
            }
            let audios = clips.map(\.audio)
            let asSecondary = await kit.transcribe(audioArrays: audios, decodeOptions: Self.options(secondary, workers: 4))
            let asPrimary = await kit.transcribe(audioArrays: audios, decodeOptions: Self.options(primary, workers: 4))

            var switched: [Int] = []
            var perPhrase: [[Word]] = phrases
            for i in phrases.indices {
                let sec = asSecondary[i] ?? []
                let pri = asPrimary[i] ?? []
                let secText = sec.map(\.text).joined().trimmingCharacters(in: .whitespacesAndNewlines)
                guard !secText.isEmpty, let ss = Self.score(sec), ss > (Self.score(pri) ?? -.infinity) + switchMargin else { continue }
                perPhrase[i] = Self.clamped(Self.words(from: sec, offset: clips[i].start, lang: secondary), clips[i])
                if perPhrase[i].isEmpty {
                    perPhrase[i] = [Word(text: secText, start: phrases[i].first!.start, end: phrases[i].last!.end, lang: secondary)]
                }
                switched.append(i)
            }

            // Second pass on the second-language phrases: bigger model and/or the word list as a hint.
            let useBest = accurateSecondary && model != .best
            if !switched.isEmpty && (useBest || !terms.secondary.isEmpty) {
                let fixKit: WhisperKit
                if useBest { fixKit = try await prepare(.best, status: status) } else { fixKit = kit }
                status("Polishing \(Self.displayName(secondary)) words…")
                var prompt: [Int]? = nil
                if !terms.secondary.isEmpty, let tok = fixKit.tokenizer {
                    prompt = tok.encode(text: " " + terms.secondary.joined(separator: ", "))
                        .filter { $0 < tok.specialTokens.specialTokenBegin }
                }
                let redo = await fixKit.transcribe(audioArrays: switched.map { clips[$0].audio },
                                                   decodeOptions: Self.options(secondary, workers: 4, prompt: prompt))
                for (k, i) in switched.enumerated() {
                    let w = Self.clamped(Self.words(from: redo[k] ?? [], offset: clips[i].start, lang: secondary), clips[i])
                    if !w.isEmpty { perPhrase[i] = w }
                }
            }
            for i in switched { perPhrase[i] = TextTools.snap(perPhrase[i], to: terms.secondary, lang: secondary) }
            words = perPhrase.flatMap { $0 }.sorted { $0.start < $1.start }
        } else if !terms.primary.isEmpty {
            words = TextTools.snap(words, to: terms.primary + terms.secondary, lang: primary)
        }

        if !script.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            words = TextTools.alignScript(script, words: words, primary: primary, secondary: secondary ?? "")
        }
        return words
    }

    private static func clamped(_ words: [Word], _ clip: (start: Double, end: Double, audio: [Float])) -> [Word] {
        words.map { w in
            var w = w
            w.start = min(max(w.start, clip.start), clip.end)
            w.end = min(max(w.end, w.start), clip.end)
            return w
        }
    }

    private static func options(_ lang: String, workers: Int? = nil, prompt: [Int]? = nil) -> DecodingOptions {
        DecodingOptions(task: .transcribe, language: lang, temperature: 0,
                        usePrefillPrompt: true, detectLanguage: false,
                        skipSpecialTokens: true, wordTimestamps: true,
                        promptTokens: prompt, concurrentWorkerCount: workers)
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
                                    lang: first.lang,
                                    words: current.map { TimedWord(text: $0.text, start: $0.start, end: $0.end) }))
            current.removeAll()
        }

        for w in words {
            if let last = current.last,
               w.lang != last.lang || w.start - last.end > 0.6 || current.count >= maxWords { flush() }
            current.append(w)
            if w.lineEnd || (w.text.last.map { ".!?,;:".contains($0) } ?? false) { flush() }
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
