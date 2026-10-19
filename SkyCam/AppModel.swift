import Foundation
import UIKit

/// Owns the camera, server, sequence runner and event hub; feeds the on-screen status.
final class AppModel: ObservableObject {
    static let shared = AppModel()

    let files = FileStore()
    let events = EventHub()
    let camera: CameraController
    let runner: SequenceRunner
    private(set) var api: API!
    private var server: HTTPServer?
    let token: String
    let port: UInt16 = 8070
    let version: String
    private let launched = Date()

    @Published var addresses: [String] = []
    @Published var serverState = "stopped"
    @Published var statusLine = "starting…"
    @Published var recentLog: [String] = []
    @Published var dimmed = false

    private let logLock = NSLock()
    private var logRing: [String] = []
    private let cacheLock = NSLock()
    private var cache: [String: Any] = [:]
    private var timers: [Timer] = []
    private var started = false

    private init() {
        let info = Bundle.main.infoDictionary ?? [:]
        version = "\(info["CFBundleShortVersionString"] ?? "?") (\(info["CFBundleVersion"] ?? "?"))"
        camera = CameraController(store: files)
        runner = SequenceRunner(camera: camera, events: events)
        token = Self.loadToken()
        api = API(model: self)
        camera.onLog = { [weak self] s in self?.log(s) }
    }

    private static var docs: URL { FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0] }

    private static func loadToken() -> String {
        let url = docs.appendingPathComponent("token.txt")
        if let t = try? String(contentsOf: url, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines), t.count >= 8 {
            return t
        }
        let t = (0..<12).map { _ in String(format: "%x", Int.random(in: 0..<16)) }.joined()
        try? t.write(to: url, atomically: true, encoding: .utf8)
        return t
    }

    func start() {
        guard !started else { return }
        started = true
        UIDevice.current.isBatteryMonitoringEnabled = true
        log("SkyCam \(version) on \(machineId())")
        camera.configure { [weak self] err in self?.log(err.map { "camera: \($0)" } ?? "camera ready") }
        startServer()
        tick()
        timers.append(Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in self?.tick() })
        timers.append(Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            guard let self else { return }
            self.events.publish("heartbeat", self.heartbeat())
        })
        let nc = NotificationCenter.default
        nc.addObserver(forName: UIApplication.didBecomeActiveNotification, object: nil, queue: .main) { [weak self] _ in
            guard let self else { return }
            self.camera.resume()
            if !self.serverState.contains("ready") { self.restartServer() }
            UIApplication.shared.isIdleTimerDisabled = true
        }
        nc.addObserver(forName: UIApplication.didEnterBackgroundNotification, object: nil, queue: .main) { [weak self] _ in
            self?.log("app went to the background -- camera and server pause until it is reopened")
        }
    }

    private func startServer() {
        let s = HTTPServer(port: port, serviceName: "SkyCam \(machineId())")
        s.handler = { [weak self] req, respond in
            guard let self else { return }
            self.api.handle(req, respond)
        }
        s.onSSE = { [weak self] c in self?.events.add(c) }
        s.onState = { [weak self] st in
            DispatchQueue.main.async {
                guard let self else { return }
                self.serverState = st
                self.log("server: \(st)")
                if st.contains("failed") {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 2) { self.restartServer() }
                }
            }
        }
        do {
            try s.start()
            server = s
        } catch {
            log("server failed to start: \(error)")
        }
    }

    private func restartServer() {
        server?.stop()
        server = nil
        startServer()
    }

    // MARK: status

    /// Runs on the main thread: UIDevice values are cached here for the server threads.
    private func tick() {
        let dev = UIDevice.current
        let thermal: String
        switch ProcessInfo.processInfo.thermalState {
        case .nominal: thermal = "nominal"
        case .fair: thermal = "fair"
        case .serious: thermal = "serious"
        case .critical: thermal = "critical"
        @unknown default: thermal = "?"
        }
        let battery = dev.batteryLevel >= 0 ? Double(dev.batteryLevel) : -1
        let charging = dev.batteryState == .charging || dev.batteryState == .full
        let addrs = ipAddresses()
        cacheLock.lock()
        cache = ["battery": battery, "charging": charging, "thermal": thermal, "addresses": addrs]
        cacheLock.unlock()
        if addrs != addresses {
            addresses = addrs
            writeConnectionFile()
        }

        let cam = camera.device.map { camera.snapshot($0) } ?? [:]
        let seq = runner.status()
        var line = ""
        if let lp = cam["lens_position"] as? Double, let e = cam["exposure_s"] as? Double, let iso = cam["iso"] as? Double {
            line = String(format: "focus %.3f  %@  ISO %.0f", lp, e >= 0.1 ? String(format: "%.2f s", e) : String(format: "1/%.0f s", 1 / max(e, 1e-6)), iso)
        }
        if seq["state"] as? String == "running" {
            line += "\nsequence \(seq["id"] ?? "") \(seq["index"] ?? 0)/\(seq["total"] ?? 0)"
        }
        line += String(format: "\nbattery %.0f%%%@  thermal %@  free %.1f GB",
                       battery * 100, charging ? " (charging)" : "", thermal, Double(files.freeBytes()) / 1e9)
        statusLine = line
    }

    func status() -> [String: Any] {
        cacheLock.lock()
        let c = cache
        cacheLock.unlock()
        return [
            "app": ["version": version, "device": machineId(), "uptime_s": Date().timeIntervalSince(launched),
                    "port": Int(port), "addresses": c["addresses"] ?? [], "event_clients": events.clientCount],
            "camera": camera.status(),
            "sequence": runner.status(),
            "power": ["battery": c["battery"] ?? -1, "charging": c["charging"] ?? false, "thermal": c["thermal"] ?? "?"],
            "storage": ["free_bytes": files.freeBytes(), "files": files.list().count],
        ]
    }

    private func heartbeat() -> [String: Any] {
        cacheLock.lock()
        let c = cache
        cacheLock.unlock()
        var hb: [String: Any] = ["battery": c["battery"] ?? -1, "thermal": c["thermal"] ?? "?",
                                 "free_bytes": files.freeBytes()]
        if let d = camera.device {
            let s = camera.snapshot(d)
            hb["lens_position"] = s["lens_position"]
            hb["exposure_s"] = s["exposure_s"]
            hb["iso"] = s["iso"]
        }
        let seq = runner.status()
        hb["sequence"] = ["state": seq["state"] ?? "idle", "index": seq["index"] ?? 0, "total": seq["total"] ?? 0]
        return hb
    }

    /// Documents/connection.json: lets the Mac pick up the token and address with
    /// `xcrun devicectl device copy from ... --domain-type appDataContainer` -- no typing.
    private func writeConnectionFile() {
        let ips = addresses.compactMap { $0.split(separator: " ").last.map(String.init) }
        let obj: [String: Any] = ["token": token, "port": Int(port), "addresses": ips, "updated": isoNow()]
        try? JSON.data(obj, pretty: true).write(to: Self.docs.appendingPathComponent("connection.json"))
    }

    // MARK: log

    func log(_ s: String) {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss"
        let line = "\(f.string(from: Date())) \(s)"
        logLock.lock()
        logRing.append(line)
        if logRing.count > 1000 { logRing.removeFirst(logRing.count - 1000) }
        logLock.unlock()
        events.publish("log", ["message": s])
        DispatchQueue.main.async {
            self.recentLog.append(line)
            if self.recentLog.count > 8 { self.recentLog.removeFirst(self.recentLog.count - 8) }
        }
    }

    func logTail(_ n: Int) -> [String] {
        logLock.lock(); defer { logLock.unlock() }
        return Array(logRing.suffix(max(1, n)))
    }

    func setDimmed(_ on: Bool) {
        dimmed = on
        let screen = (UIApplication.shared.connectedScenes.first as? UIWindowScene)?.screen
        screen?.brightness = on ? 0.02 : 0.6
    }
}
