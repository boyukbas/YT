import Foundation

/// A saved project: the video (copied into the app) plus every edit.
struct Project: Codable, Identifiable {
    var id: String
    var name: String
    var updated: Date
    var videoFile: String
    var duration: Double
    var trimStart: Double
    var trimEnd: Double
    var manualCuts: [TimeRange]
    var removeSilence: Bool
    var thresholdDB: Double
    var words: [Word]
    var captions: [Caption]
    var script: String
    var glossary: String
}

/// Keeps projects in Application Support/Projects/<id>/ so they survive app restarts.
enum ProjectStore {
    static let keep = 10

    static var root: URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        let dir = base.appendingPathComponent("Projects", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }
    static func folder(_ id: String) -> URL { root.appendingPathComponent(id, isDirectory: true) }
    static func videoURL(_ p: Project) -> URL { folder(p.id).appendingPathComponent(p.videoFile) }

    /// Moves a freshly picked video into a new project folder.
    static func adopt(videoAt url: URL, id: String) throws -> URL {
        let dir = folder(id)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let dest = dir.appendingPathComponent("video." + (url.pathExtension.isEmpty ? "mov" : url.pathExtension))
        if FileManager.default.fileExists(atPath: dest.path) { try FileManager.default.removeItem(at: dest) }
        try FileManager.default.moveItem(at: url, to: dest)
        return dest
    }

    static func save(_ p: Project) {
        let enc = JSONEncoder()
        guard let data = try? enc.encode(p) else { return }
        try? data.write(to: folder(p.id).appendingPathComponent("project.json"), options: .atomic)
    }

    static func list() -> [Project] {
        let dirs = (try? FileManager.default.contentsOfDirectory(at: root, includingPropertiesForKeys: nil)) ?? []
        return dirs.compactMap { dir in
            guard let data = try? Data(contentsOf: dir.appendingPathComponent("project.json")) else { return nil }
            return try? JSONDecoder().decode(Project.self, from: data)
        }
        .sorted { $0.updated > $1.updated }
    }

    static func delete(_ id: String) { try? FileManager.default.removeItem(at: folder(id)) }

    static func pruneOld() { for p in list().dropFirst(keep) { delete(p.id) } }
}
