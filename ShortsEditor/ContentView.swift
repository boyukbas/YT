import AVKit
import PhotosUI
import SwiftUI
import UniformTypeIdentifiers

@main
struct ShortsEditorApp: App {
    var body: some Scene {
        WindowGroup {
            ContentView().preferredColorScheme(.dark)
        }
    }
}

struct ContentView: View {
    @StateObject private var model = EditorModel()
    @State private var pickerItem: PhotosPickerItem?
    @State private var showCaptionEditor = false
    @Environment(\.displayScale) private var displayScale

    var body: some View {
        NavigationStack {
            Group {
                if model.sourceURL == nil { emptyState } else { editor }
            }
            .navigationTitle("Shorts Editor")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                if model.sourceURL != nil {
                    ToolbarItem(placement: .topBarLeading) {
                        Button { model.closeProject() } label: { Label("Projects", systemImage: "square.grid.2x2") }
                    }
                }
            }
        }
        .onChange(of: pickerItem) {
            guard let item = pickerItem else { return }
            Task { await importVideo(item) }
        }
        .overlay { if model.isBusy { busyOverlay } }
        .sheet(isPresented: $showCaptionEditor, onDismiss: { model.refreshCaptions() }) {
            CaptionEditor(captions: $model.captions, langs: model.activeLangs,
                          colorFor: { model.color(for: $0).color })
                .presentationDetents([.medium, .large])
        }
    }

    private func importVideo(_ item: PhotosPickerItem) async {
        model.isBusy = true
        model.status = "Importing video…"
        do {
            if let movie = try await item.loadTransferable(type: PickedMovie.self) {
                let name = "Video " + Date().formatted(date: .abbreviated, time: .shortened)
                await model.startProject(with: movie.url, name: name)
            }
        } catch {
            model.status = "Import failed: \(error.localizedDescription)"
        }
        model.isBusy = false
        pickerItem = nil
    }

    // MARK: Screens

    private var emptyState: some View {
        ScrollView {
            VStack(spacing: 20) {
                Image(systemName: "film.stack").font(.system(size: 64)).foregroundStyle(.red)
                Text("Make a Short").font(.title.bold())
                Text("Trim, cut, remove silences and add subtitles in two languages and two colors.")
                    .foregroundStyle(.secondary).multilineTextAlignment(.center)
                PhotosPicker(selection: $pickerItem, matching: .videos, preferredItemEncoding: .current) {
                    Label("Choose video", systemImage: "photo.on.rectangle").padding(.horizontal)
                }
                .buttonStyle(.borderedProminent).tint(.red).controlSize(.large)
                if let status = model.status { Text(status).font(.footnote).foregroundStyle(.secondary) }

                if !model.projects.isEmpty {
                    VStack(alignment: .leading, spacing: 10) {
                        Text("Recent projects").font(.headline)
                        ForEach(model.projects) { p in
                            HStack {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(p.name).lineLimit(1)
                                    Text("\(formatTime(p.duration)) · \(p.captions.count) lines · \(p.updated.formatted(date: .abbreviated, time: .omitted))")
                                        .font(.caption).foregroundStyle(.secondary)
                                }
                                Spacer()
                                Button("Open") { Task { await model.openProject(p) } }.buttonStyle(.bordered)
                                Button(role: .destructive) { model.deleteProject(p) } label: { Image(systemName: "trash") }
                                    .buttonStyle(.borderless)
                            }
                            .padding(12)
                            .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 14))
                        }
                    }
                    .padding(.top, 8)
                }
            }
            .padding()
        }
    }

    private var editor: some View {
        ScrollView {
            VStack(spacing: 16) {
                preview
                if let status = model.status {
                    Text(status).font(.footnote).foregroundStyle(.secondary).multilineTextAlignment(.center)
                }
                trimCard
                cutCard
                silenceCard
                subtitleCard
                if !model.captions.isEmpty { lookCard }
                exportCard
            }
            .padding()
        }
    }

    private var preview: some View {
        VStack(spacing: 6) {
            VideoPlayer(player: model.player) {
                GeometryReader { geo in
                    // Draw subtitles over the actual video area, exactly as the export will.
                    let rect = AVMakeRect(aspectRatio: model.videoSize, insideRect: CGRect(origin: .zero, size: geo.size))
                    if let r = model.previewOverlay(canvas: rect.size, scale: displayScale) {
                        Image(uiImage: r.image)
                            .resizable()
                            .frame(width: r.frame.width, height: r.frame.height)
                            .position(x: rect.minX + r.frame.midX, y: rect.minY + r.frame.midY)
                    }
                }
                .allowsHitTesting(false)
            }
            .frame(height: 440)
            .clipShape(RoundedRectangle(cornerRadius: 16))

            HStack {
                Text(formatTime(model.currentOutputTime))
                Spacer()
                Text("Edited \(formatTime(model.outputDuration)) · was \(formatTime(model.duration))")
            }
            .font(.caption.monospacedDigit()).foregroundStyle(.secondary)
        }
    }

    private var trimCard: some View {
        Card(title: "Trim", systemImage: "timeline.selection") {
            LabeledSlider(title: "Start", value: $model.trimStart, range: 0...max(model.duration, 0.1),
                          format: formatTime) { Task { await model.trimChanged() } }
            LabeledSlider(title: "End", value: $model.trimEnd, range: 0...max(model.duration, 0.1),
                          format: formatTime) { Task { await model.trimChanged() } }
            HStack {
                Button("Start at playhead") { Task { await model.setTrimStartToPlayhead() } }
                Spacer()
                Button("End at playhead") { Task { await model.setTrimEndToPlayhead() } }
            }
            .font(.subheadline).buttonStyle(.bordered)
        }
    }

    private var cutCard: some View {
        Card(title: "Cut out a part", systemImage: "scissors") {
            Text("Pause where the bad part starts → Mark start. Move to where it ends → Cut.")
                .font(.caption).foregroundStyle(.secondary)
            HStack {
                Button(model.markIn == nil ? "Mark start" : "Start marked ✓") { model.setMarkIn() }
                    .buttonStyle(.bordered)
                Button("Cut") { Task { await model.cutToHere() } }
                    .buttonStyle(.borderedProminent).tint(.red)
                    .disabled(model.markIn == nil)
                Spacer()
                Button { Task { await model.undoCut() } } label: { Image(systemName: "arrow.uturn.backward") }
                    .disabled(model.manualCuts.isEmpty)
            }
            ForEach(model.manualCuts) { cut in
                HStack {
                    Text("Removed \(formatTime(cut.start)) – \(formatTime(cut.end))")
                    Spacer()
                    Button(role: .destructive) { Task { await model.removeCut(cut) } } label: {
                        Image(systemName: "xmark.circle.fill")
                    }
                }
                .font(.subheadline)
            }
        }
    }

    private var silenceCard: some View {
        Card(title: "Remove silences", systemImage: "waveform.badge.minus") {
            Toggle("Cut out pauses automatically", isOn: $model.removeSilence)
                .onChange(of: model.removeSilence) { Task { await model.rebuildPreview() } }
            if model.removeSilence {
                LabeledSlider(title: "Silence level", value: $model.silenceSettings.thresholdDB, range: -60...(-20),
                              format: { "\(Int($0)) dB" }) { Task { await model.silenceSettingsChanged() } }
                LabeledSlider(title: "Shortest pause to cut", value: $model.silenceSettings.minSilence, range: 0.2...1.5,
                              format: { String(format: "%.2f s", $0) }) { Task { await model.silenceSettingsChanged() } }
                LabeledSlider(title: "Keep around speech", value: $model.silenceSettings.padding, range: 0...0.3,
                              format: { String(format: "%.2f s", $0) }) { Task { await model.silenceSettingsChanged() } }
                HStack {
                    Text("\(model.silences.count) pauses found").font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    Button("Auto level") { Task { await model.autoThreshold() } }.font(.subheadline)
                }
            }
        }
    }

    private var subtitleCard: some View {
        Card(title: "Auto subtitles", systemImage: "captions.bubble") {
            HStack {
                Text("Spoken language")
                Spacer()
                Picker("Spoken language", selection: $model.primaryLang) {
                    ForEach(Transcriber.languageCodes, id: \.self) { Text(Transcriber.displayName($0)).tag($0) }
                }
                .pickerStyle(.menu)
            }
            HStack {
                Text("Words in")
                Spacer()
                Picker("Second language", selection: $model.secondaryLang) {
                    Text("No second language").tag("")
                    ForEach(Transcriber.languageCodes, id: \.self) { Text(Transcriber.displayName($0)).tag($0) }
                }
                .pickerStyle(.menu)
            }
            Picker("Accuracy", selection: $model.speechModel) {
                ForEach(SpeechModel.allCases) { Text($0.label).tag($0) }
            }
            .pickerStyle(.segmented)
            if !model.secondaryLang.isEmpty && model.speechModel != .best {
                Toggle(isOn: $model.accurateSecondary) {
                    VStack(alignment: .leading) {
                        Text("Accurate second language")
                        Text("Uses the Best model only for those words (\(SpeechModel.best.downloadSize) once)")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
            DisclosureGroup(model.script.isEmpty ? "My script (optional)" : "My script ✓") {
                Text("Paste what you say. The subtitles use your exact words; the app only finds the timing. Each line becomes its own subtitle break.")
                    .font(.caption).foregroundStyle(.secondary)
                TextEditor(text: $model.script)
                    .frame(minHeight: 120)
                    .scrollContentBackground(.hidden)
                    .background(Color.black.opacity(0.25), in: RoundedRectangle(cornerRadius: 10))
            }
            DisclosureGroup(model.glossary.isEmpty ? "Word list (optional)" : "Word list ✓") {
                Text("The words you teach, one pair per line, like “der Wald – orman”. Second-language words are guided and corrected to these.")
                    .font(.caption).foregroundStyle(.secondary)
                TextEditor(text: $model.glossary)
                    .frame(minHeight: 100)
                    .scrollContentBackground(.hidden)
                    .background(Color.black.opacity(0.25), in: RoundedRectangle(cornerRadius: 10))
            }
            if !Transcriber.shared.isDownloaded(model.speechModel) {
                Text("First use downloads the speech model (\(model.speechModel.downloadSize)) — use Wi-Fi. After that it works offline.")
                    .font(.caption2).foregroundStyle(.secondary)
            }
            Button(model.captions.isEmpty ? "Generate subtitles" : "Regenerate subtitles") {
                Task { await model.generateCaptions() }
            }
            .buttonStyle(.borderedProminent)
            if !model.captions.isEmpty {
                Button("Fix lines: text, language, timing") { showCaptionEditor = true }
            }
        }
    }

    private var lookCard: some View {
        Card(title: "Look", systemImage: "textformat") {
            Picker("Layout", selection: $model.layout) {
                ForEach(CaptionLayout.allCases) { Text($0.label).tag($0) }
            }
            .pickerStyle(.segmented)
            ColorRow(title: Transcriber.displayName(model.primaryLang), selection: $model.primaryColor)
            if model.activeLangs.count > 1 {
                ColorRow(title: Transcriber.displayName(model.secondaryLang), selection: $model.secondaryColor)
            }
            LabeledSlider(title: "Text size", value: $model.textSize, range: 0.05...0.11,
                          format: { "\(Int($0 / 0.075 * 100))%" }) {}
            LabeledSlider(title: "Height on screen", value: $model.captionPosition, range: 0.12...0.85,
                          format: { "\(Int($0 * 100))%" }) {}
            Toggle("Dark box behind text", isOn: $model.boxBehind)
            Toggle("Highlight the spoken word", isOn: $model.highlightWords)
            Toggle("ALL CAPS", isOn: $model.uppercase)
                .onChange(of: model.uppercase) { model.refreshCaptions() }
            Stepper("Words per line: \(model.wordsPerCaption)", value: $model.wordsPerCaption, in: 1...6)
                .onChange(of: model.wordsPerCaption) { model.rebuildCaptions() }
            Toggle("Burn subtitles into video", isOn: $model.burnCaptions)
            Text("Changing words per line rebuilds the lines and drops text fixes.")
                .font(.caption2).foregroundStyle(.secondary)
        }
    }

    private var exportCard: some View {
        Card(title: "Export", systemImage: "square.and.arrow.up") {
            if model.outputDuration > 180 {
                Label("Over 3 minutes — YouTube won't treat it as a Short.", systemImage: "exclamationmark.triangle")
                    .font(.caption).foregroundStyle(.orange)
            }
            Button { Task { await model.exportToPhotos() } } label: {
                Label("Save video to Photos", systemImage: "square.and.arrow.down").frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent).tint(.red).controlSize(.large)

            if let url = model.lastExportURL {
                ShareLink(item: url) { Label("Share video", systemImage: "square.and.arrow.up") }
            }
            if let srt = model.srtURL {
                ShareLink(item: srt) { Label("Share subtitles (.srt)", systemImage: "doc.text") }
                Text("YouTube shows .srt subtitles in one color. Upload it in YouTube Studio only if you didn't burn subtitles in.")
                    .font(.caption2).foregroundStyle(.secondary)
            }
        }
    }

    private var busyOverlay: some View {
        ZStack {
            Color.black.opacity(0.55).ignoresSafeArea()
            VStack(spacing: 14) {
                ProgressView().controlSize(.large)
                Text(model.status ?? "Working…").multilineTextAlignment(.center)
            }
            .padding(24)
            .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 16))
        }
    }
}

// MARK: - Small views

struct Card<Content: View>: View {
    let title: String
    let systemImage: String
    @ViewBuilder let content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Label(title, systemImage: systemImage).font(.headline)
            content()
        }
        .padding()
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 16))
    }
}

struct LabeledSlider: View {
    let title: String
    @Binding var value: Double
    let range: ClosedRange<Double>
    let format: (Double) -> String
    let onCommit: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(title)
                Spacer()
                Text(format(value)).monospacedDigit().foregroundStyle(.secondary)
            }
            .font(.subheadline)
            Slider(value: $value, in: range, onEditingChanged: { editing in if !editing { onCommit() } })
        }
    }
}

/// One tappable color swatch per option, for one language.
struct ColorRow: View {
    let title: String
    @Binding var selection: CaptionColor

    var body: some View {
        HStack {
            Text(title).font(.subheadline)
            Spacer()
            ForEach(CaptionColor.allCases) { c in
                Circle()
                    .fill(c.color)
                    .frame(width: 26, height: 26)
                    .overlay(Circle().stroke(Color.primary, lineWidth: selection == c ? 3 : 0).padding(-3))
                    .onTapGesture { selection = c }
                    .accessibilityLabel(c.rawValue)
                    .accessibilityAddTraits(selection == c ? .isSelected : [])
            }
        }
    }
}

struct CaptionEditor: View {
    @Binding var captions: [Caption]
    let langs: [String]
    let colorFor: (String) -> Color
    @Environment(\.dismiss) private var dismiss

    private func nudge(_ icon: String, _ label: String, _ action: @escaping () -> Void) -> some View {
        Button(action: action) { Image(systemName: icon).frame(width: 28, height: 24) }
            .buttonStyle(.bordered).controlSize(.mini)
            .accessibilityLabel(label)
    }

    var body: some View {
        NavigationStack {
            List {
                ForEach($captions) { $caption in
                    VStack(alignment: .leading, spacing: 4) {
                        HStack {
                            Text(formatTime(caption.start)).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                            Spacer()
                            if langs.count > 1 {
                                // Tap to switch the caption between the two languages (changes its color).
                                Button {
                                    let i = langs.firstIndex(of: caption.lang) ?? -1
                                    caption.lang = langs[(i + 1) % langs.count]
                                } label: {
                                    Text(caption.lang.uppercased())
                                        .font(.caption.bold())
                                        .padding(.horizontal, 8).padding(.vertical, 2)
                                        .foregroundStyle(.black)
                                        .background(colorFor(caption.lang), in: Capsule())
                                }
                                .buttonStyle(.plain)
                            }
                        }
                        TextField("Subtitle", text: $caption.text, axis: .vertical)
                            .foregroundStyle(colorFor(caption.lang))
                        HStack(spacing: 8) {
                            Text("Start").foregroundStyle(.secondary)
                            nudge("minus", "Start earlier") { caption.start = max(0, caption.start - 0.1) }
                            nudge("plus", "Start later") { caption.start = min(caption.end - 0.1, caption.start + 0.1) }
                            Text("End").foregroundStyle(.secondary).padding(.leading, 6)
                            nudge("minus", "End earlier") { caption.end = max(caption.start + 0.1, caption.end - 0.1) }
                            nudge("plus", "End later") { caption.end += 0.1 }
                            Spacer()
                            Text("\(formatTime(caption.start))–\(formatTime(caption.end))")
                                .font(.caption2.monospacedDigit()).foregroundStyle(.secondary)
                        }
                        .font(.caption)
                    }
                }
                .onDelete { captions.remove(atOffsets: $0) }
            }
            .navigationTitle("Lines")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { Button("Done") { dismiss() } }
        }
    }
}

/// Copies the picked video into the app's temp folder.
struct PickedMovie: Transferable {
    let url: URL

    static var transferRepresentation: some TransferRepresentation {
        FileRepresentation(contentType: .movie) { movie in
            SentTransferredFile(movie.url)
        } importing: { received in
            let ext = received.file.pathExtension.isEmpty ? "mov" : received.file.pathExtension
            let dest = FileManager.default.temporaryDirectory
                .appendingPathComponent("source-\(UUID().uuidString).\(ext)")
            try FileManager.default.copyItem(at: received.file, to: dest)
            return PickedMovie(url: dest)
        }
    }
}
