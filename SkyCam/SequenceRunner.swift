import Foundation

/// Runs a capture program on the phone, independent of the network connection.
///
/// Program JSON (all keys optional):
///   name        label used in file names (default "seq")
///   focus, exposure, iso, wb, bias   base settings, as for /settings
///   count       frames with the base settings (default 1)
///   frames      explicit list of per-frame setting overrides, e.g. [{"focus":0.90},{"focus":0.92}]
///   focus_bracket   {"from":0.86,"to":0.98,"step":0.01} or [0.88, 0.9, 0.92]
///   per_value   frames per bracket value / per listed frame (default 1)
///   repeat      repeat the whole list (default 1)
///   interval    seconds, start to start (default 0 = back to back)
///   settle      seconds to wait after a settings change (default 0.5)
///   raw         Bayer DNG (default true);  jpeg / heic: also save a processed image
final class SequenceRunner {
    struct Frame {
        var settings: [String: Any]
        var tag: String?
    }

    private let camera: CameraController
    private let events: EventHub
    private let queue = DispatchQueue(label: "skycam.sequence")
    private let lock = NSLock()
    private var running = false
    private var stopRequested = false
    private var info: [String: Any] = ["state": "idle"]
    private static let settingKeys = ["focus", "exposure", "iso", "wb", "bias"]

    init(camera: CameraController, events: EventHub) {
        self.camera = camera
        self.events = events
    }

    func status() -> [String: Any] {
        lock.lock(); defer { lock.unlock() }
        return info
    }

    var isRunning: Bool { lock.lock(); defer { lock.unlock() }; return running }

    @discardableResult
    func stop() -> Bool {
        lock.lock(); defer { lock.unlock() }
        guard running else { return false }
        stopRequested = true
        info["state"] = "stopping"
        return true
    }

    private func stopping() -> Bool { lock.lock(); defer { lock.unlock() }; return stopRequested }
    private func update(_ f: (inout [String: Any]) -> Void) { lock.lock(); f(&info); lock.unlock() }

    static func plan(_ p: [String: Any]) throws -> [Frame] {
        var base: [String: Any] = [:]
        for k in settingKeys { if let v = p[k] { base[k] = v } }
        let perValue = max(1, Int(num(p["per_value"]) ?? 1))
        var frames: [Frame] = []

        func merged(_ o: [String: Any]) -> [String: Any] { base.merging(o) { _, new in new } }

        if let list = p["frames"] as? [[String: Any]] {
            for f in list {
                let tag = f["tag"] as? String ?? (num(f["focus"]).map { String(format: "f%.3f", $0) })
                for _ in 0..<perValue { frames.append(Frame(settings: merged(f.filter { settingKeys.contains($0.key) }), tag: tag)) }
            }
        } else if let fb = p["focus_bracket"] {
            var values: [Double] = []
            if let arr = fb as? [Any] {
                values = arr.compactMap { num($0) }
            } else if let d = fb as? [String: Any], let from = num(d["from"]), let to = num(d["to"]), let step = num(d["step"]), step > 0 {
                var v = from
                let dir: Double = to >= from ? 1 : -1
                while (to - v) * dir >= -1e-9 { values.append((v * 10000).rounded() / 10000); v += step * dir }
            }
            guard !values.isEmpty else { throw NSError(domain: "skycam", code: 1, userInfo: [NSLocalizedDescriptionKey: "focus_bracket needs {from,to,step} or a list"]) }
            for v in values {
                for _ in 0..<perValue { frames.append(Frame(settings: merged(["focus": v]), tag: String(format: "f%.3f", v))) }
            }
        } else {
            let count = max(1, Int(num(p["count"]) ?? 1))
            frames = Array(repeating: Frame(settings: base, tag: nil), count: count)
        }
        let rep = max(1, Int(num(p["repeat"]) ?? 1))
        frames = Array([[Frame]](repeating: frames, count: rep).joined())
        guard frames.count <= 100_000 else { throw NSError(domain: "skycam", code: 2, userInfo: [NSLocalizedDescriptionKey: "more than 100000 frames"]) }
        return frames
    }

    /// Start a program; returns an error message or the accepted plan summary.
    func start(_ p: [String: Any]) -> (String?, [String: Any]) {
        let frames: [Frame]
        do { frames = try Self.plan(p) } catch { return (error.localizedDescription, [:]) }
        lock.lock()
        if running { lock.unlock(); return ("a sequence is already running; POST /sequence/stop first", info) }
        running = true
        stopRequested = false
        let name = (p["name"] as? String ?? "seq").filter { $0.isLetter || $0.isNumber || $0 == "-" }
        let id = (name.isEmpty ? "seq" : name) + "-" + String(Int(Date().timeIntervalSince1970) % 100000)
        let interval = num(p["interval"]) ?? 0
        let settle = num(p["settle"]) ?? 0.5
        let raw = p["raw"] as? Bool ?? true
        let processed: String? = (p["heic"] as? Bool ?? false) ? "heic" : ((p["jpeg"] as? Bool ?? false) || !raw ? "jpeg" : nil)
        info = ["state": "running", "id": id, "index": 0, "total": frames.count, "files": 0, "errors": 0,
                "started": isoNow(), "interval": interval, "program": JSON.sanitize(p)]
        let summary = info
        lock.unlock()

        events.publish("sequence_started", ["seq": id, "total": frames.count])
        queue.async { self.run(id: id, frames: frames, interval: interval, settle: settle, raw: raw, processed: processed) }
        return (nil, summary)
    }

    private func run(id: String, frames: [Frame], interval: Double, settle: Double, raw: Bool, processed: String?) {
        var prev: [String: Any] = [:]
        var nFiles = 0, nErrors = 0
        let t0 = Date()
        for (i, fr) in frames.enumerated() {
            if stopping() { break }
            let tStart = Date()

            // Apply only what changed since the previous frame.
            let changed = fr.settings.filter { k, v in prev[k].map { JSON.string([$0]) != JSON.string([v]) } ?? true }
            if !changed.isEmpty {
                let sem = DispatchSemaphore(value: 0)
                var err: String?
                var notes: [String] = []
                camera.apply(changed) { e, n in err = e; notes = n; sem.signal() }
                if sem.wait(timeout: .now() + 20) == .timedOut { err = "settings did not apply within 20 s" }
                for n in notes { events.publish("note", ["seq": id, "index": i + 1, "message": n]) }
                if let err {
                    nErrors += 1
                    events.publish("error", ["seq": id, "index": i + 1, "message": err])
                }
                if settle > 0 { Thread.sleep(forTimeInterval: settle) }
                prev.merge(changed) { _, new in new }
            }

            let sem = DispatchSemaphore(value: 0)
            var files: [String] = []
            var meta: [String: Any] = [:]
            var cerr: String?
            camera.capture(CaptureOptions(raw: raw, processed: processed, seq: id, index: i + 1, tag: fr.tag)) { f, m, e in
                files = f; meta = m; cerr = e; sem.signal()
            }
            if sem.wait(timeout: .now() + camera.exposureSeconds + 30) == .timedOut { cerr = "capture did not finish within 30 s" }
            nFiles += files.count
            if cerr != nil { nErrors += 1 }
            update {
                $0["index"] = i + 1
                $0["files"] = nFiles
                $0["errors"] = nErrors
                $0["last_files"] = files
                if let cerr { $0["last_error"] = cerr }
            }
            events.publish("frame", [
                "seq": id, "index": i + 1, "total": frames.count, "files": files,
                "lens_position": meta["lens_position"] ?? NSNull(),
                "exposure_s": meta["exposure_s"] ?? NSNull(),
                "iso": meta["iso"] ?? NSNull(),
                "tag": fr.tag ?? NSNull(),
                "error": cerr ?? NSNull(),
            ])

            var wait = interval - Date().timeIntervalSince(tStart)
            while wait > 0 && !stopping() {
                Thread.sleep(forTimeInterval: min(wait, 0.25))
                wait -= 0.25
            }
        }
        let stopped = stopping()
        lock.lock()
        running = false
        stopRequested = false
        info["state"] = stopped ? "stopped" : "done"
        info["finished"] = isoNow()
        let idx = info["index"] ?? 0
        lock.unlock()
        events.publish("sequence_done", ["seq": id, "state": stopped ? "stopped" : "done", "frames": idx,
                                         "files": nFiles, "errors": nErrors,
                                         "elapsed_s": Date().timeIntervalSince(t0)])
    }
}
