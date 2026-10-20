import AVFoundation
import SwiftUI

enum CaptionColor: String, CaseIterable, Identifiable {
    case white, yellow, cyan, green, pink, orange
    var id: String { rawValue }
    var uiColor: UIColor {
        switch self {
        case .white: return .white
        case .yellow: return UIColor(red: 1, green: 0.86, blue: 0.1, alpha: 1)
        case .cyan: return UIColor(red: 0.3, green: 0.9, blue: 1, alpha: 1)
        case .green: return UIColor(red: 0.45, green: 1, blue: 0.45, alpha: 1)
        case .pink: return UIColor(red: 1, green: 0.5, blue: 0.8, alpha: 1)
        case .orange: return UIColor(red: 1, green: 0.62, blue: 0.2, alpha: 1)
        }
    }
    var color: Color { Color(uiColor) }
}

/// Small helper so choices survive app restarts.
private enum Saved {
    static func string(_ key: String, _ fallback: String) -> String { UserDefaults.standard.string(forKey: key) ?? fallback }
    static func set(_ value: String, _ key: String) { UserDefaults.standard.set(value, forKey: key) }
}

@MainActor
final class EditorModel: ObservableObject {
    // Source
    @Published var sourceURL: URL?
    @Published var duration: Double = 0

    // Cutting (all in original-video time)
    @Published var trimStart: Double = 0
    @Published var trimEnd: Double = 0
    @Published var manualCuts: [TimeRange] = []
    @Published var markIn: Double?

    // Silence removal
    @Published var removeSilence = true
    @Published var silenceSettings = SilenceDetector.Settings()
    @Published private(set) var silences: [TimeRange] = []

    // Subtitles
    @Published var primaryLang = Saved.string("primaryLang", "de") { didSet { Saved.set(primaryLang, "primaryLang") } }
    /// "" = only one language in the video.
    @Published var secondaryLang = Saved.string("secondaryLang", "tr") { didSet { Saved.set(secondaryLang, "secondaryLang") } }
    @Published var speechModel = SpeechModel(rawValue: Saved.string("speechModel", "")) ?? .fast {
        didSet { Saved.set(speechModel.rawValue, "speechModel") }
    }
    @Published var captions: [Caption] = []          // original-video time, editable text
    @Published var wordsPerCaption = 3
    @Published var uppercase = true
    @Published var primaryColor = CaptionColor(rawValue: Saved.string("primaryColor", "")) ?? .white {
        didSet { Saved.set(primaryColor.rawValue, "primaryColor") }
    }
    @Published var secondaryColor = CaptionColor(rawValue: Saved.string("secondaryColor", "")) ?? .yellow {
        didSet { Saved.set(secondaryColor.rawValue, "secondaryColor") }
    }
    @Published var captionPosition = 0.35
    @Published var burnCaptions = true
    @Published private(set) var previewCaptions: [Caption] = []  // edited-video time

    // Playback / status
    @Published private(set) var outputDuration: Double = 0
    @Published var currentOutputTime: Double = 0
    @Published var isBusy = false
    @Published var status: String?
    @Published private(set) var srtURL: URL?
    @Published private(set) var lastExportURL: URL?

    let player = AVPlayer()
    private var words: [Word] = []
    private var levels: [Float] = []
    private var previewMap = TimelineMap(kept: [])

    init() {
        _ = player.addPeriodicTimeObserver(forInterval: CMTime(value: 1, timescale: 10), queue: .main) { [weak self] time in
            MainActor.assumeIsolated { self?.currentOutputTime = time.seconds }
        }
    }

    var keptRanges: [TimeRange] {
        let removals = (removeSilence ? silences : []) + manualCuts
        return TimelineMap.subtract(from: TimeRange(start: trimStart, end: trimEnd), removing: removals)
    }

    /// Languages captions can be tagged with (main first).
    var activeLangs: [String] {
        secondaryLang.isEmpty || secondaryLang == primaryLang ? [primaryLang] : [primaryLang, secondaryLang]
    }

    func color(for lang: String) -> CaptionColor { lang == primaryLang ? primaryColor : secondaryColor }

    var captionStyle: CaptionStyle {
        CaptionStyle(primaryLang: primaryLang, primaryColor: primaryColor.uiColor,
                     secondaryColor: secondaryColor.uiColor, position: captionPosition)
    }

    var currentCaption: Caption? {
        previewCaptions.first { currentOutputTime >= $0.start && currentOutputTime < $0.end }
    }

    var playheadSourceTime: Double { previewMap.toSource(currentOutputTime) }

    // MARK: Loading

    func load(_ url: URL) async {
        isBusy = true
        status = "Analyzing audio…"
        player.pause()
        duration = (try? await AVURLAsset(url: url).load(.duration))?.seconds ?? 0
        sourceURL = url
        trimStart = 0
        trimEnd = duration
        manualCuts = []
        markIn = nil
        words = []
        captions = []
        previewCaptions = []
        srtURL = nil
        lastExportURL = nil
        previewMap = TimelineMap(kept: [])
        currentOutputTime = 0

        levels = (try? await Task.detached(priority: .userInitiated) {
            try await SilenceDetector.levels(url: url)
        }.value) ?? []
        if let auto = SilenceDetector.suggestedThreshold(levels: levels) {
            silenceSettings.thresholdDB = auto
        }
        recomputeSilences()
        await rebuildPreview()
        isBusy = false
        status = nil
    }

    // MARK: Editing

    func trimChanged() async {
        trimStart = min(max(0, trimStart), duration)
        trimEnd = min(max(trimEnd, trimStart + 0.5), duration)
        if trimEnd - trimStart < 0.5 { trimStart = max(0, trimEnd - 0.5) }
        await rebuildPreview()
    }

    func setTrimStartToPlayhead() async { trimStart = playheadSourceTime; await trimChanged() }
    func setTrimEndToPlayhead() async { trimEnd = playheadSourceTime; await trimChanged() }

    func setMarkIn() { markIn = playheadSourceTime }

    func cutToHere() async {
        guard let a = markIn else { return }
        let b = playheadSourceTime
        markIn = nil
        guard abs(b - a) > 0.05 else { return }
        manualCuts.append(TimeRange(start: min(a, b), end: max(a, b)))
        await rebuildPreview()
    }

    func undoCut() async {
        guard !manualCuts.isEmpty else { return }
        manualCuts.removeLast()
        await rebuildPreview()
    }

    func removeCut(_ cut: TimeRange) async {
        manualCuts.removeAll { $0 == cut }
        await rebuildPreview()
    }

    func silenceSettingsChanged() async {
        recomputeSilences()
        if removeSilence { await rebuildPreview() }
    }

    func autoThreshold() async {
        if let auto = SilenceDetector.suggestedThreshold(levels: levels) {
            silenceSettings.thresholdDB = auto
            await silenceSettingsChanged()
        }
    }

    private func recomputeSilences() {
        silences = SilenceDetector.silences(in: levels, settings: silenceSettings)
    }

    /// Rebuilds the edited video for preview, keeping the playhead on the same moment.
    func rebuildPreview() async {
        guard let url = sourceURL else { return }
        let sourcePosition = previewMap.toSource(currentOutputTime)
        let kept = keptRanges
        do {
            let built = try await CompositionBuilder.build(sourceURL: url, kept: kept)
            previewMap = TimelineMap(kept: kept)
            outputDuration = previewMap.outputDuration
            player.replaceCurrentItem(with: AVPlayerItem(asset: built.composition))
            refreshCaptions()
            let target = CMTime(seconds: previewMap.toOutput(sourcePosition), preferredTimescale: 600)
            await player.seek(to: target, toleranceBefore: .zero, toleranceAfter: .zero)
        } catch {
            status = error.localizedDescription
        }
    }

    // MARK: Subtitles

    func generateCaptions() async {
        guard let url = sourceURL else { return }
        player.pause()
        isBusy = true
        status = "Reading audio…"
        defer { isBusy = false }
        do {
            let second = secondaryLang == primaryLang ? "" : secondaryLang
            words = try await Transcriber.shared.transcribe(
                videoURL: url, primary: primaryLang, secondary: second, model: speechModel,
                status: { [weak self] in self?.status = $0 })
            rebuildCaptions()
            status = words.isEmpty ? "No speech found in this video." : nil
        } catch {
            status = "Subtitles failed: \(error.localizedDescription)"
        }
    }

    func rebuildCaptions() {
        captions = CaptionBuilder.build(from: words, maxWords: wordsPerCaption)
        refreshCaptions()
    }

    /// Maps captions onto the edited timeline (drops ones that were cut out).
    func refreshCaptions() {
        let mapped: [Caption] = captions.compactMap { c in
            let s = previewMap.toOutput(c.start)
            let e = previewMap.toOutput(c.end)
            let text = c.text.trimmingCharacters(in: .whitespacesAndNewlines)
            guard e - s > 0.05, !text.isEmpty else { return nil }
            return Caption(id: c.id, start: s, end: e, text: text, lang: c.lang)
        }
        // Locale-aware caps: Turkish "i" -> "İ", German "ß" -> "SS".
        previewCaptions = uppercase
            ? mapped.map { var c = $0; c.text = c.text.uppercased(with: Locale(identifier: c.lang)); return c }
            : mapped

        if mapped.isEmpty {
            srtURL = nil
        } else {
            let url = FileManager.default.temporaryDirectory.appendingPathComponent("subtitles.srt")
            do {
                try SRTWriter.make(mapped).write(to: url, atomically: true, encoding: .utf8)
                srtURL = url
            } catch {
                srtURL = nil
            }
        }
    }

    // MARK: Export

    func exportToPhotos() async {
        guard let url = sourceURL else { return }
        player.pause()
        isBusy = true
        status = "Exporting video…"
        defer { isBusy = false }
        do {
            let out = try await VideoExporter.export(sourceURL: url, kept: keptRanges,
                                                     captions: burnCaptions ? previewCaptions : [],
                                                     style: captionStyle)
            lastExportURL = out
            try await PhotoSaver.save(videoAt: out)
            status = "Saved to Photos ✅"
        } catch {
            status = "Export failed: \(error.localizedDescription)"
        }
    }
}
