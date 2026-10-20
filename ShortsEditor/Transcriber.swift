import AVFoundation
import Speech

enum Transcriber {
    static func supportedLocaleIDs() -> [String] {
        SFSpeechRecognizer.supportedLocales()
            .map { $0.identifier.replacingOccurrences(of: "_", with: "-") }
            .sorted { displayName($0) < displayName($1) }
    }

    static func displayName(_ id: String) -> String {
        Locale.current.localizedString(forIdentifier: id) ?? id
    }

    static func defaultLocaleID() -> String {
        let ids = Set(supportedLocaleIDs())
        let current = Locale.current.identifier.replacingOccurrences(of: "_", with: "-")
        if ids.contains(current) { return current }
        return ids.contains("en-US") ? "en-US" : (ids.first ?? "en-US")
    }

    static func requestAuthorization() async -> Bool {
        await withCheckedContinuation { cont in
            SFSpeechRecognizer.requestAuthorization { cont.resume(returning: $0 == .authorized) }
        }
    }

    /// Transcribes the whole original video and returns words with timestamps.
    static func transcribe(videoURL: URL, localeID: String) async throws -> [Word] {
        guard await requestAuthorization() else { throw EditorError.speechPermission }

        let audioURL = try await extractAudio(from: videoURL)
        defer { try? FileManager.default.removeItem(at: audioURL) }

        guard let recognizer = SFSpeechRecognizer(locale: Locale(identifier: localeID)),
              recognizer.isAvailable else { throw EditorError.recognizerUnavailable }

        let request = SFSpeechURLRecognitionRequest(url: audioURL)
        request.shouldReportPartialResults = false
        request.addsPunctuation = true
        request.taskHint = .dictation
        // On-device = private, free, and no 1-minute limit.
        if recognizer.supportsOnDeviceRecognition { request.requiresOnDeviceRecognition = true }

        let collector = RecognitionCollector()
        let words: [Word] = try await withCheckedThrowingContinuation { cont in
            collector.continuation = cont
            collector.task = recognizer.recognitionTask(with: request, delegate: collector)
        }
        withExtendedLifetime((recognizer, collector)) {}
        return words
    }

    private static func extractAudio(from url: URL) async throws -> URL {
        let asset = AVURLAsset(url: url)
        guard let session = AVAssetExportSession(asset: asset, presetName: AVAssetExportPresetAppleM4A) else {
            throw EditorError.exportFailed
        }
        let out = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".m4a")
        try await session.export(to: out, as: .m4a)
        return out
    }
}

/// Collects every final result (the recognizer can split long audio at pauses).
final class RecognitionCollector: NSObject, SFSpeechRecognitionTaskDelegate {
    var continuation: CheckedContinuation<[Word], Error>?
    var task: SFSpeechRecognitionTask?
    private var words: [Word] = []

    func speechRecognitionTask(_ task: SFSpeechRecognitionTask, didFinishRecognition recognitionResult: SFSpeechRecognitionResult) {
        let lastEnd = words.last?.end ?? -1
        for seg in recognitionResult.bestTranscription.segments {
            let text = seg.substring.trimmingCharacters(in: .whitespaces)
            guard !text.isEmpty, seg.timestamp >= lastEnd - 0.05 else { continue }
            words.append(Word(text: text, start: seg.timestamp, end: seg.timestamp + seg.duration))
        }
    }

    func speechRecognitionTask(_ task: SFSpeechRecognitionTask, didFinishSuccessfully successfully: Bool) {
        guard let cont = continuation else { return }
        continuation = nil
        if successfully || !words.isEmpty {
            cont.resume(returning: words)
        } else if let error = task.error as NSError?, error.code == 1110 { // "No speech detected"
            cont.resume(returning: [])
        } else {
            cont.resume(throwing: task.error ?? EditorError.transcriptionFailed)
        }
    }
}

enum CaptionBuilder {
    /// Groups words into short, punchy Shorts-style captions.
    static func build(from words: [Word], maxWords: Int) -> [Caption] {
        var captions: [Caption] = []
        var current: [Word] = []

        func flush() {
            guard let first = current.first, let last = current.last else { return }
            captions.append(Caption(start: first.start, end: last.end,
                                    text: current.map(\.text).joined(separator: " ")))
            current.removeAll()
        }

        for w in words {
            if let last = current.last, w.start - last.end > 0.6 || current.count >= maxWords { flush() }
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
