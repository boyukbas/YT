import AVFoundation
import Photos
import UIKit

struct BuiltComposition {
    let composition: AVMutableComposition
    let videoTrack: AVMutableCompositionTrack?
    let transform: CGAffineTransform
    let naturalSize: CGSize
    let frameRate: Float
}

enum CompositionBuilder {
    /// Glues the kept parts of the original video back to back.
    static func build(sourceURL: URL, kept: [TimeRange]) async throws -> BuiltComposition {
        let asset = AVURLAsset(url: sourceURL)
        guard let srcVideo = try await asset.loadTracks(withMediaType: .video).first else {
            throw EditorError.noVideoTrack
        }
        let srcAudio = try await asset.loadTracks(withMediaType: .audio).first
        let (transform, naturalSize, fps, videoRange) = try await srcVideo.load(
            .preferredTransform, .naturalSize, .nominalFrameRate, .timeRange)
        let audioRange = try await srcAudio?.load(.timeRange)

        let comp = AVMutableComposition()
        let compVideo = comp.addMutableTrack(withMediaType: .video, preferredTrackID: kCMPersistentTrackID_Invalid)
        compVideo?.preferredTransform = transform
        let compAudio = srcAudio == nil ? nil
            : comp.addMutableTrack(withMediaType: .audio, preferredTrackID: kCMPersistentTrackID_Invalid)

        var cursor = CMTime.zero
        for r in kept {
            let range = CMTimeRange(start: CMTime(seconds: r.start, preferredTimescale: 600),
                                    end: CMTime(seconds: r.end, preferredTimescale: 600))
                .intersection(videoRange)
            guard range.duration > .zero else { continue }
            try compVideo?.insertTimeRange(range, of: srcVideo, at: cursor)
            if let srcAudio, let audioRange, let compAudio {
                let a = range.intersection(audioRange)
                if a.duration > .zero {
                    try compAudio.insertTimeRange(a, of: srcAudio, at: cursor + (a.start - range.start))
                }
            }
            cursor = cursor + range.duration
        }
        return BuiltComposition(composition: comp, videoTrack: compVideo, transform: transform,
                                naturalSize: naturalSize, frameRate: fps)
    }
}

enum VideoExporter {
    @MainActor
    static func export(sourceURL: URL, kept: [TimeRange], captions: [Caption], look: CaptionLook) async throws -> URL {
        let built = try await CompositionBuilder.build(sourceURL: sourceURL, kept: kept)
        guard let session = AVAssetExportSession(asset: built.composition,
                                                 presetName: AVAssetExportPresetHighestQuality) else {
            throw EditorError.exportFailed
        }
        if !captions.isEmpty {
            session.videoComposition = captionComposition(built: built, captions: captions, look: look)
        }
        let out = FileManager.default.temporaryDirectory
            .appendingPathComponent("short-\(UUID().uuidString.prefix(8)).mp4")
        try await session.export(to: out, as: .mp4)
        return out
    }

    @MainActor
    private static func captionComposition(built: BuiltComposition, captions: [Caption],
                                           look: CaptionLook) -> AVMutableVideoComposition? {
        guard let track = built.videoTrack else { return nil }
        let total = built.composition.duration.seconds
        guard total > 0 else { return nil }

        // Handle portrait iPhone video (stored rotated + preferredTransform).
        let rect = CGRect(origin: .zero, size: built.naturalSize).applying(built.transform)
        let renderSize = CGSize(width: abs(rect.width).rounded(), height: abs(rect.height).rounded())
        let transform = built.transform.concatenating(CGAffineTransform(translationX: -rect.minX, y: -rect.minY))

        let vc = AVMutableVideoComposition()
        vc.renderSize = renderSize
        let fps = built.frameRate > 0 ? built.frameRate : 30
        vc.frameDuration = CMTime(value: 1, timescale: CMTimeScale(fps.rounded()))

        let instruction = AVMutableVideoCompositionInstruction()
        instruction.timeRange = CMTimeRange(start: .zero, duration: built.composition.duration)
        let layerInstruction = AVMutableVideoCompositionLayerInstruction(assetTrack: track)
        layerInstruction.setTransform(transform, at: .zero)
        instruction.layerInstructions = [layerInstruction]
        vc.instructions = [instruction]

        let parent = CALayer()
        parent.frame = CGRect(origin: .zero, size: renderSize)
        let videoLayer = CALayer()
        videoLayer.frame = parent.frame
        parent.addSublayer(videoLayer)
        // One image per distinct subtitle picture, shown during each stretch where it applies.
        let segments = CaptionRenderer.segments(captions, total: total, look: look)
        var groups: [String: (blocks: [Caption], t: Double, ranges: [(Double, Double)])] = [:]
        var order: [String] = []
        for s in segments {
            if groups[s.key] == nil { groups[s.key] = (s.blocks, s.t, []); order.append(s.key) }
            groups[s.key]!.ranges.append((s.start, s.end))
        }
        for key in order {
            guard let g = groups[key],
                  let r = CaptionRenderer.render(g.blocks, at: g.t, canvas: renderSize, look: look),
                  let cg = r.image.cgImage else { continue }
            parent.addSublayer(timedLayer(cg, frame: r.frame, renderSize: renderSize, ranges: g.ranges, total: total))
        }
        vc.animationTool = AVVideoCompositionCoreAnimationTool(postProcessingAsVideoLayer: videoLayer, in: parent)
        return vc
    }

    /// A layer that is visible only during the given time ranges (output time, seconds).
    @MainActor
    private static func timedLayer(_ image: CGImage, frame: CGRect, renderSize: CGSize,
                                   ranges: [(Double, Double)], total: Double) -> CALayer {
        let layer = CALayer()
        layer.contents = image
        // Core Animation here has its origin at the bottom-left.
        layer.frame = CGRect(x: frame.minX, y: renderSize.height - frame.maxY, width: frame.width, height: frame.height)
        layer.opacity = 0

        var keyTimes: [Double] = [0], values: [Float] = []
        var cursor = 0.0
        for (a, b) in ranges.sorted(by: { $0.0 < $1.0 }) {
            if a > cursor + 1e-6 { values.append(0); keyTimes.append(a) }
            values.append(1); keyTimes.append(b)
            cursor = b
        }
        if cursor < total - 1e-6 { values.append(0); keyTimes.append(total) }

        let anim = CAKeyframeAnimation(keyPath: "opacity")
        anim.values = values
        anim.keyTimes = keyTimes.map { NSNumber(value: min(1, $0 / total)) }
        anim.calculationMode = .discrete
        anim.beginTime = AVCoreAnimationBeginTimeAtZero
        anim.duration = total
        anim.isRemovedOnCompletion = false
        anim.fillMode = .both
        layer.add(anim, forKey: "visibility")
        return layer
    }
}

enum PhotoSaver {
    static func save(videoAt url: URL) async throws {
        let status = await PHPhotoLibrary.requestAuthorization(for: .addOnly)
        guard status == .authorized || status == .limited else { throw EditorError.photosPermission }
        try await PHPhotoLibrary.shared().performChanges {
            _ = PHAssetChangeRequest.creationRequestForAssetFromVideo(atFileURL: url)
        }
    }
}
