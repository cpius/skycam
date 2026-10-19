import Foundation

/// HTTP routes. Everything except GET /ping needs "Authorization: Bearer <token>"
/// (or ?token=<token>); the token is shown in the app and stored in Documents/connection.json.
///
///   GET  /ping                      app name/version (no token)
///   GET  /status                    camera, sequence, battery, thermal, storage
///   POST /settings                  {focus, exposure, iso, bias, wb}
///   POST /af                        {x, y, lock}  one autofocus scan, returns lens_position
///   POST /capture                   {settings..., raw, jpeg, heic, tag}  one frame, waits for the files
///   POST /sequence                  program (see SequenceRunner); returns at once
///   GET  /sequence                  progress
///   POST /sequence/stop
///   GET  /files[?after=<name>]      list
///   GET  /files/<name>              download
///   DELETE /files/<name>
///   POST /files/delete              {names: [...]} or {all: true}
///   GET  /preview.jpg[?w=1280&q=0.7]  latest live-view frame
///   GET  /events                    Server-Sent Events (frame, sequence_*, heartbeat every 5 s, log)
///   GET  /events/poll?since=<id>    the same events as JSON
///   GET  /log[?n=200]
final class API {
    unowned let model: AppModel

    init(model: AppModel) { self.model = model }

    private func authorized(_ r: HTTPRequest) -> Bool {
        if r.headers["authorization"] == "Bearer \(model.token)" { return true }
        return r.query["token"] == model.token
    }

    func handle(_ req: HTTPRequest, _ respond: @escaping (HTTPResponse) -> Void) {
        let parts = req.path.split(separator: "/").map(String.init)
        let route = parts.first ?? ""
        if req.method == "GET" && (route.isEmpty || route == "ping") {
            respond(.json(["ok": true, "app": "SkyCam", "version": model.version, "device": machineId(), "api": 1]))
            return
        }
        guard authorized(req) else {
            respond(.error("unauthorized: send 'Authorization: Bearer <token>' (token is on the phone screen)", status: 401))
            return
        }
        if route != "events" && route != "preview.jpg" && !(req.method == "GET" && route == "status") {
            model.log("\(req.method) \(req.path)")
        }

        switch (req.method, route, parts.count) {
        case ("GET", "status", 1):
            respond(.json(model.status()))

        case ("POST", "settings", 1):
            model.camera.apply(req.json) { err, notes in
                if let err { respond(.error(err)); return }
                var out: [String: Any] = ["ok": true, "camera": self.model.camera.status()]
                if !notes.isEmpty { out["notes"] = notes }
                respond(.json(out))
            }

        case ("POST", "af", 1):
            let b = req.json
            model.camera.autofocusOnce(x: num(b["x"]), y: num(b["y"]), lock: b["lock"] as? Bool ?? true) { pos, err in
                var out: [String: Any] = ["ok": err == nil, "lens_position": pos ?? NSNull()]
                if let err { out["error"] = err }
                respond(.json(out))
            }

        case ("POST", "capture", 1):
            guard !model.runner.isRunning else { respond(.error("a sequence is running", status: 409)); return }
            let b = req.json
            let settings = b.filter { ["focus", "exposure", "iso", "wb", "bias"].contains($0.key) }
            let raw = b["raw"] as? Bool ?? true
            let processed: String? = (b["heic"] as? Bool ?? false) ? "heic" : ((b["jpeg"] as? Bool ?? false) || !raw ? "jpeg" : nil)
            let shoot = {
                self.model.camera.capture(CaptureOptions(raw: raw, processed: processed, tag: b["tag"] as? String)) { files, meta, err in
                    self.model.events.publish("frame", ["files": files, "lens_position": meta["lens_position"] ?? NSNull(),
                                                        "exposure_s": meta["exposure_s"] ?? NSNull(), "iso": meta["iso"] ?? NSNull(),
                                                        "error": err ?? NSNull()])
                    respond(.json(["ok": err == nil, "files": files, "meta": meta, "error": err ?? NSNull()], status: err == nil ? 200 : 500))
                }
            }
            if settings.isEmpty {
                shoot()
            } else {
                model.camera.apply(settings) { err, _ in
                    if let err { respond(.error(err)) } else { shoot() }
                }
            }

        case ("POST", "sequence", 1):
            let (err, summary) = model.runner.start(req.json)
            if let err { respond(.json(["ok": false, "error": err, "sequence": summary], status: 409)); return }
            respond(.json(["ok": true, "sequence": summary], status: 202))

        case ("GET", "sequence", 1):
            respond(.json(model.runner.status()))

        case ("POST", "sequence", 2) where parts[1] == "stop":
            respond(.json(["ok": model.runner.stop(), "sequence": model.runner.status()]))

        case ("GET", "files", 1):
            var list = model.files.list()
            if let after = req.query["after"] { list = list.filter { ($0["name"] as? String ?? "") > after } }
            respond(.json(["files": list, "free_bytes": model.files.freeBytes()]))

        case ("GET", "files", 2):
            guard let url = model.files.url(parts[1]) else { respond(.error("no such file", status: 404)); return }
            let ext = url.pathExtension.lowercased()
            let type = ["dng": "image/x-adobe-dng", "jpg": "image/jpeg", "heic": "image/heic", "json": "application/json"][ext]
                ?? "application/octet-stream"
            respond(HTTPResponse(status: 200, contentType: type, body: .file(url)))

        case ("DELETE", "files", 2):
            respond(.json(["ok": model.files.delete(parts[1])]))

        case ("POST", "files", 2) where parts[1] == "delete":
            let b = req.json
            var names = b["names"] as? [String] ?? []
            if b["all"] as? Bool == true { names = model.files.list().compactMap { $0["name"] as? String } }
            let deleted = names.filter { model.files.delete($0) }
            respond(.json(["ok": true, "deleted": deleted.count, "free_bytes": model.files.freeBytes()]))

        case ("GET", "preview.jpg", 1):
            let w = Int(req.query["w"] ?? "") ?? 1280
            let q = Double(req.query["q"] ?? "") ?? 0.7
            guard let jpg = model.camera.previewJPEG(maxWidth: w, quality: q) else {
                respond(.error("no live-view frame yet", status: 503)); return
            }
            respond(HTTPResponse(status: 200, contentType: "image/jpeg", body: .data(jpg)))

        case ("GET", "events", 1):
            respond(HTTPResponse(status: 200, contentType: "text/event-stream", body: .sse))

        case ("GET", "events", 2) where parts[1] == "poll":
            let since = Int(req.query["since"] ?? "") ?? 0
            respond(.json(["events": model.events.since(since), "last_id": model.events.lastId]))

        case ("GET", "log", 1):
            respond(.json(["log": model.logTail(Int(req.query["n"] ?? "") ?? 200)]))

        default:
            respond(.error("no route \(req.method) \(req.path)", status: 404))
        }
    }
}
