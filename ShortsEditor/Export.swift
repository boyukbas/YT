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

struct CaptionStyle {
    var color: UIColor
    var position: Double   // 0 = bottom edge, 1 = top edge
}

enum VideoExporter {
    @MainActor
    static func export(sourceURL: URL, kept: [TimeRange], captions: [Caption], style: CaptionStyle) async throws -> URL {
        let built = try await CompositionBuilder.build(sourceURL: sourceURL, kept: kept)
        guard let session = AVAssetExportSession(asset: built.composition,
                                                 presetName: AVAssetExportPresetHighestQuality) else {
            throw EditorError.exportFailed
        }
        if !captions.isEmpty {
            session.videoComposition = captionComposition(built: built, captions: captions, style: style)
        }
        let out = FileManager.default.temporaryDirectory
            .appendingPathComponent("short-\(UUID().uuidString.prefix(8)).mp4")
        try await session.export(to: out, as: .mp4)
        return out
    }

    @MainActor
    private static func captionComposition(built: BuiltComposition, captions: [Caption],
                                           style: CaptionStyle) -> AVMutableVideoComposition? {
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
        for c in captions {
            if let layer = captionLayer(c, renderSize: renderSize, total: total, style: style) {
                parent.addSublayer(layer)
            }
        }
        vc.animationTool = AVVideoCompositionCoreAnimationTool(postProcessingAsVideoLayer: videoLayer, in: parent)
        return vc
    }

    /// Renders one caption as a bitmap (bold text with black outline) that is only visible during its time.
    @MainActor
    private static func captionLayer(_ c: Caption, renderSize: CGSize, total: Double, style: CaptionStyle) -> CALayer? {
        let fontSize = renderSize.width * 0.075
        let maxWidth = renderSize.width * 0.86
        let paragraph = NSMutableParagraphStyle()
        paragraph.alignment = .center
        let font = UIFont.systemFont(ofSize: fontSize, weight: .heavy)

        let fill = NSAttributedString(string: c.text, attributes: [
            .font: font, .foregroundColor: style.color, .paragraphStyle: paragraph])
        let outline = NSAttributedString(string: c.text, attributes: [
            .font: font, .foregroundColor: UIColor.clear, .strokeColor: UIColor.black,
            .strokeWidth: 14.0, .paragraphStyle: paragraph])

        let options: NSStringDrawingOptions = [.usesLineFragmentOrigin, .usesFontLeading]
        let textHeight = ceil(fill.boundingRect(with: CGSize(width: maxWidth, height: .greatestFiniteMagnitude),
                                                options: options, context: nil).height)
        let pad = fontSize * 0.25
        let size = CGSize(width: maxWidth + pad * 2, height: textHeight + pad * 2)

        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        format.opaque = false
        let image = UIGraphicsImageRenderer(size: size, format: format).image { _ in
            let r = CGRect(x: pad, y: pad, width: maxWidth, height: textHeight)
            outline.draw(with: r, options: options, context: nil)
            fill.draw(with: r, options: options, context: nil)
        }
        guard let cg = image.cgImage else { return nil }

        let layer = CALayer()
        layer.contents = cg
        layer.frame = CGRect(x: (renderSize.width - size.width) / 2,
                             y: renderSize.height * style.position - size.height / 2,
                             width: size.width, height: size.height)
        layer.opacity = 0

        let anim = CAKeyframeAnimation(keyPath: "opacity")
        anim.values = [0.0, 1.0, 0.0]
        anim.keyTimes = [NSNumber(value: 0.0), NSNumber(value: c.start / total),
                         NSNumber(value: c.end / total), NSNumber(value: 1.0)]
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
