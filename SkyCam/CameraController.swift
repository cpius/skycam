import AVFoundation
import CoreImage
import ImageIO
import UIKit

struct CaptureOptions {
    var raw = true
    var processed: String?      // nil, "jpeg" or "heic"
    var seq: String?
    var index: Int?
    var tag: String?
}

/// Back wide camera with full manual control and Bayer RAW (DNG) capture.
/// Every capture also writes <base>.json with the lens position, exposure, ISO and WB
/// it was taken with -- the things NightCap never recorded.
final class CameraController: NSObject {
    let session = AVCaptureSession()
    let sessionQueue = DispatchQueue(label: "skycam.camera")
    private let videoQueue = DispatchQueue(label: "skycam.video")
    private let photoOutput = AVCapturePhotoOutput()
    private let videoOutput = AVCaptureVideoDataOutput()
    private let store: FileStore
    private let ciContext = CIContext()
    private let bufferLock = NSLock()
    private var latestBuffer: CVPixelBuffer?
    private var latestBufferTime = Date.distantPast
    private var latestVideoPTS = CMTime.invalid
    private var pending: [Int64: Pending] = [:]
    /// Frames time-stamped before this (the sync time of the last focus/exposure change) are
    /// stale: the photo output serves frames from a buffer exposed before the request.
    private var validAfter = CMTime.zero
    private var lastFrameTime = CMTime.invalid
    private(set) var staleDiscards = 0
    /// Two deliveries of the same sensor frame can differ by sub-microsecond rounding.
    private let sameFrame = CMTime(value: 1, timescale: 1000)
    private(set) var device: AVCaptureDevice?
    private(set) var bayerFormat: OSType?
    private(set) var lastError: String?
    var onLog: ((String) -> Void)?

    private struct Pending {
        var base: String
        var meta: [String: Any]
        var processed: String?
        var files: [String] = []
        var errors: [String] = []
        var completion: ([String], [String: Any], String?) -> Void
        var makeSettings: () -> AVCapturePhotoSettings
        var requestTime: CMTime
        var attempt = 0
        var stale: Bool?
    }

    private let nameFormatter: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.dateFormat = "yyyyMMdd_HHmmss_SSS"
        return f
    }()

    init(store: FileStore) {
        self.store = store
        super.init()
        let nc = NotificationCenter.default
        nc.addObserver(forName: AVCaptureSession.runtimeErrorNotification, object: session, queue: nil) { [weak self] n in
            let err = n.userInfo?[AVCaptureSessionErrorKey] as? NSError
            self?.onLog?("camera runtime error: \(err?.localizedDescription ?? "?") -- restarting")
            self?.resume()
        }
        nc.addObserver(forName: AVCaptureSession.wasInterruptedNotification, object: session, queue: nil) { [weak self] n in
            self?.onLog?("camera interrupted (\(n.userInfo?[AVCaptureSessionInterruptionReasonKey] ?? "?"))")
        }
        nc.addObserver(forName: AVCaptureSession.interruptionEndedNotification, object: session, queue: nil) { [weak self] _ in
            self?.onLog?("camera interruption ended")
        }
    }

    // MARK: setup

    func configure(completion: @escaping (String?) -> Void) {
        sessionQueue.async {
            switch AVCaptureDevice.authorizationStatus(for: .video) {
            case .authorized:
                completion(self.setup())
            case .notDetermined:
                AVCaptureDevice.requestAccess(for: .video) { granted in
                    self.sessionQueue.async { completion(granted ? self.setup() : "camera permission denied") }
                }
            default:
                completion("camera permission denied -- enable it in Settings > SkyCam")
            }
        }
    }

    private func setup() -> String? {
        guard device == nil else { return nil }
        guard let d = AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: .back) else {
            lastError = "no back wide camera"; return lastError
        }
        session.beginConfiguration()
        session.sessionPreset = .photo
        do {
            let input = try AVCaptureDeviceInput(device: d)
            guard session.canAddInput(input) else { session.commitConfiguration(); lastError = "cannot add camera input"; return lastError }
            session.addInput(input)
        } catch {
            session.commitConfiguration(); lastError = "camera input: \(error.localizedDescription)"; return lastError
        }
        guard session.canAddOutput(photoOutput) else { session.commitConfiguration(); lastError = "cannot add photo output"; return lastError }
        session.addOutput(photoOutput)
        videoOutput.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA]
        videoOutput.alwaysDiscardsLateVideoFrames = true
        videoOutput.setSampleBufferDelegate(self, queue: videoQueue)
        if session.canAddOutput(videoOutput) { session.addOutput(videoOutput) }
        // Zero shutter lag hands back a frame from a ring buffer exposed BEFORE the request --
        // i.e. possibly before the last focus/exposure change. Every frame must start after it.
        if photoOutput.isZeroShutterLagSupported { photoOutput.isZeroShutterLagEnabled = false }
        if photoOutput.isResponsiveCaptureSupported { photoOutput.isResponsiveCaptureEnabled = false }
        session.commitConfiguration()
        device = d
        bayerFormat = photoOutput.availableRawPhotoPixelFormatTypes.first { AVCapturePhotoOutput.isBayerRAWPixelFormat($0) }
        session.startRunning()
        lastError = nil
        onLog?("camera: \(d.localizedName), Bayer RAW \(bayerFormat.map(fourCC) ?? "NOT available"), "
               + String(format: "exposure %.6f-%.3f s, ISO %.0f-%.0f",
                        d.activeFormat.minExposureDuration.seconds, d.activeFormat.maxExposureDuration.seconds,
                        d.activeFormat.minISO, d.activeFormat.maxISO))
        return nil
    }

    func resume() {
        sessionQueue.async {
            if self.device != nil && !self.session.isRunning { self.session.startRunning() }
        }
    }

    // MARK: settings

    /// Apply any of: focus (lens position 0..1 | "auto" | "lock"), exposure (seconds | "auto"),
    /// iso, bias (EV, auto exposure only), wb ("auto" | "lock" | {temp, tint} | {gains: [r,g,b]}).
    /// Values are clamped to the device limits (out-of-range values crash AVFoundation).
    /// Completion gets a fatal error (or nil) and notes such as clamping.
    func apply(_ s: [String: Any], completion: @escaping (String?, [String]) -> Void) {
        sessionQueue.async {
            guard let d = self.device else { completion("camera not ready: \(self.lastError ?? "starting")", []); return }
            do { try d.lockForConfiguration() } catch { completion("lockForConfiguration: \(error.localizedDescription)", []); return }
            let group = DispatchGroup()
            let pendingLock = NSLock()
            var waiting = Set<String>()
            func expect(_ what: String) { pendingLock.lock(); waiting.insert(what); pendingLock.unlock(); group.enter() }
            func confirmed(_ what: String) -> (CMTime) -> Void {
                return { t in
                    self.sessionQueue.async { if t.isValid && t > self.validAfter { self.validAfter = t } }
                    pendingLock.lock(); let was = waiting.remove(what) != nil; pendingLock.unlock()
                    if was { group.leave() }
                }
            }
            var notes: [String] = []
            var fatal: String?

            if let f = s["focus"] {
                if let str = f as? String {
                    switch str {
                    case "auto", "continuous":
                        if d.isFocusModeSupported(.continuousAutoFocus) { d.focusMode = .continuousAutoFocus }
                    case "lock", "locked":
                        if d.isFocusModeSupported(.locked) { d.focusMode = .locked }
                    default:
                        fatal = "focus must be a lens position 0..1, 'auto' or 'lock'"
                    }
                } else if let v = num(f) {
                    if d.isLockingFocusWithCustomLensPositionSupported {
                        if v < 0 || v > 1 { notes.append("focus clamped to 0..1") }
                        expect("focus")
                        d.setFocusModeLocked(lensPosition: Float(clamp(v, 0, 1)), completionHandler: confirmed("focus"))
                    } else { fatal = "this camera cannot lock a custom lens position" }
                }
            }

            let fmt = d.activeFormat
            if let e = s["exposure"] as? String, e == "auto" {
                if d.isExposureModeSupported(.continuousAutoExposure) { d.exposureMode = .continuousAutoExposure }
                if let b = num(s["bias"]) {
                    d.setExposureTargetBias(clamp(Float(b), d.minExposureTargetBias, d.maxExposureTargetBias), completionHandler: nil)
                }
            } else if num(s["exposure"]) != nil || num(s["iso"]) != nil {
                if d.isExposureModeSupported(.custom) {
                    var dur = AVCaptureDevice.currentExposureDuration
                    if let e = num(s["exposure"]) {
                        let lo = fmt.minExposureDuration.seconds, hi = fmt.maxExposureDuration.seconds
                        if e > hi { notes.append(String(format: "exposure %.3g s clamped to the %.3g s maximum", e, hi)) }
                        if e < lo { notes.append(String(format: "exposure clamped to the %.3g s minimum", lo)) }
                        dur = CMTime(seconds: clamp(e, lo, hi), preferredTimescale: 1_000_000_000)
                    }
                    var iso = AVCaptureDevice.currentISO
                    if let i = num(s["iso"]) {
                        if Float(i) > fmt.maxISO || Float(i) < fmt.minISO {
                            notes.append(String(format: "ISO clamped to %.0f-%.0f", fmt.minISO, fmt.maxISO))
                        }
                        iso = clamp(Float(i), fmt.minISO, fmt.maxISO)
                    }
                    expect("exposure")
                    d.setExposureModeCustom(duration: dur, iso: iso, completionHandler: confirmed("exposure"))
                } else { fatal = "custom exposure not supported" }
            }

            if let w = s["wb"] {
                if let str = w as? String {
                    if str == "auto", d.isWhiteBalanceModeSupported(.continuousAutoWhiteBalance) {
                        d.whiteBalanceMode = .continuousAutoWhiteBalance
                    } else if str == "lock", d.isWhiteBalanceModeSupported(.locked) {
                        d.whiteBalanceMode = .locked
                    }
                } else if let dict = w as? [String: Any] {
                    var gains: AVCaptureDevice.WhiteBalanceGains?
                    if let t = num(dict["temp"]) {
                        let tt = AVCaptureDevice.WhiteBalanceTemperatureAndTintValues(temperature: Float(t),
                                                                                    tint: Float(num(dict["tint"]) ?? 0))
                        gains = d.deviceWhiteBalanceGains(for: tt)
                    } else if let g = dict["gains"] as? [Any], g.count == 3,
                              let r = num(g[0]), let gg = num(g[1]), let b = num(g[2]) {
                        gains = AVCaptureDevice.WhiteBalanceGains(redGain: Float(r), greenGain: Float(gg), blueGain: Float(b))
                    }
                    if var g = gains, d.isLockingWhiteBalanceWithCustomDeviceGainsSupported {
                        let mx = d.maxWhiteBalanceGain
                        g.redGain = clamp(g.redGain, 1, mx)
                        g.greenGain = clamp(g.greenGain, 1, mx)
                        g.blueGain = clamp(g.blueGain, 1, mx)
                        d.setWhiteBalanceModeLocked(with: g, completionHandler: nil)
                    }
                }
            }
            d.unlockForConfiguration()
            if let fatal { completion(fatal, notes); return }
            // A confirmation arrives with the first frame that has the new settings: allow two
            // frame times plus slack, then report what did not confirm instead of hanging.
            let deadline = 2 * max(d.exposureDuration.seconds, num(s["exposure"]) ?? 0) + 3
            DispatchQueue.global().async {
                if group.wait(timeout: .now() + deadline) == .timedOut {
                    pendingLock.lock(); let late = waiting.sorted(); pendingLock.unlock()
                    notes.append("not confirmed within \(Int(deadline)) s: \(late.joined(separator: ", "))")
                }
                self.sessionQueue.async { completion(nil, notes) }
            }
        }
    }

    /// One autofocus scan (optionally at a point, 0..1 in sensor coordinates), then report
    /// the lens position it settled on and, if lock is set, hold it there.
    func autofocusOnce(x: Double?, y: Double?, lock: Bool, completion: @escaping (Double?, String?) -> Void) {
        sessionQueue.async {
            guard let d = self.device else { completion(nil, "camera not ready"); return }
            guard d.isFocusModeSupported(.autoFocus) else { completion(nil, "autofocus not supported"); return }
            do { try d.lockForConfiguration() } catch { completion(nil, error.localizedDescription); return }
            if let x, let y, d.isFocusPointOfInterestSupported {
                d.focusPointOfInterest = CGPoint(x: clamp(x, 0, 1), y: clamp(y, 0, 1))
            }
            d.focusMode = .autoFocus
            d.unlockForConfiguration()
            let start = Date()
            var sawAdjusting = false
            func poll() {
                if d.isAdjustingFocus { sawAdjusting = true }
                let t = Date().timeIntervalSince(start)
                let finished = (sawAdjusting && !d.isAdjustingFocus) || (!sawAdjusting && t > 2)
                if finished || t > 10 {
                    if lock, (try? d.lockForConfiguration()) != nil {
                        d.setFocusModeLocked(lensPosition: d.lensPosition, completionHandler: nil)
                        d.unlockForConfiguration()
                    }
                    completion(Double(d.lensPosition), finished ? nil : "autofocus did not settle within 10 s")
                    return
                }
                self.sessionQueue.asyncAfter(deadline: .now() + 0.05) { poll() }
            }
            poll()
        }
    }

    // MARK: capture

    func capture(_ o: CaptureOptions, completion: @escaping ([String], [String: Any], String?) -> Void) {
        sessionQueue.async {
            guard let d = self.device, self.session.isRunning else {
                completion([], [:], "camera not running: \(self.lastError ?? "is the app in the foreground?")"); return
            }
            let codec: AVVideoCodecType? = o.processed == nil ? nil : (o.processed == "heic" ? .hevc : .jpeg)
            if o.raw && self.bayerFormat == nil { completion([], [:], "Bayer RAW is not available"); return }
            let rawType = self.bayerFormat
            let output = self.photoOutput
            let make: () -> AVCapturePhotoSettings = {
                let settings: AVCapturePhotoSettings
                if o.raw, let rt = rawType {
                    if let codec, output.availablePhotoCodecTypes.contains(codec) {
                        settings = AVCapturePhotoSettings(rawPixelFormatType: rt, processedFormat: [AVVideoCodecKey: codec])
                    } else {
                        settings = AVCapturePhotoSettings(rawPixelFormatType: rt)
                    }
                } else {
                    settings = AVCapturePhotoSettings(format: [AVVideoCodecKey: codec ?? .jpeg])
                }
                settings.photoQualityPrioritization = .speed
                if output.supportedFlashModes.contains(.off) { settings.flashMode = .off }
                return settings
            }
            let settings = make()

            var base = "SC_" + self.nameFormatter.string(from: Date())
            if let seq = o.seq { base += "_\(seq)" }
            if let i = o.index { base += String(format: "_%04d", i) }
            if let tag = o.tag, !tag.isEmpty {
                base += "_" + tag.map { $0.isLetter || $0.isNumber || $0 == "." || $0 == "-" ? String($0) : "-" }.joined()
            }
            var meta = self.snapshot(d)
            meta["captured_at"] = isoNow()
            meta["base"] = base
            meta["raw_format"] = o.raw ? self.bayerFormat.map(fourCC) : nil
            if let seq = o.seq { meta["sequence"] = seq }
            if let i = o.index { meta["index"] = i }
            if let tag = o.tag { meta["tag"] = tag }
            _ = settings
            let p = Pending(base: base, meta: meta, processed: o.processed, completion: completion,
                            makeSettings: make, requestTime: CMClockGetTime(CMClockGetHostTimeClock()))
            self.issue(p, deadline: Date().addingTimeInterval(3 * d.exposureDuration.seconds + 5))
        }
    }

    /// The photo output hands back the newest frame of the running stream -- the same frame again
    /// if asked twice within one exposure, or a frame exposed before the last settings change.
    /// So wait until the live-view stream shows a frame that is both new and post-change, then capture.
    private func issue(_ p: Pending, deadline: Date) {
        bufferLock.lock()
        let have = latestVideoPTS
        bufferLock.unlock()
        let afterChange = have.isValid && have >= validAfter
        let fresh = !lastFrameTime.isValid || (have.isValid && have > lastFrameTime + sameFrame)
        if (afterChange && fresh) || Date() > deadline {
            var p = p
            p.meta["video_pts_at_issue"] = have.seconds
            p.meta["video_pts_scale"] = Int(have.timescale)
            let s = p.makeSettings()
            pending[s.uniqueID] = p
            photoOutput.capturePhoto(with: s, delegate: self)
        } else {
            sessionQueue.asyncAfter(deadline: .now() + 0.02) { self.issue(p, deadline: deadline) }
        }
    }

    // MARK: status & preview

    func snapshot(_ d: AVCaptureDevice) -> [String: Any] {
        let g = d.deviceWhiteBalanceGains
        let tt = d.temperatureAndTintValues(for: g)
        return [
            "lens_position": Double(d.lensPosition),
            "focus_mode": Self.name(d.focusMode),
            "adjusting_focus": d.isAdjustingFocus,
            "exposure_s": d.exposureDuration.seconds,
            "iso": Double(d.iso),
            "exposure_mode": Self.name(d.exposureMode),
            "exposure_offset_ev": Double(d.exposureTargetOffset),
            "wb_mode": Self.name(d.whiteBalanceMode),
            "wb_gains": [Double(g.redGain), Double(g.greenGain), Double(g.blueGain)],
            "wb_temp": Double(tt.temperature),
            "wb_tint": Double(tt.tint),
            "aperture": Double(d.lensAperture),
        ]
    }

    func status() -> [String: Any] {
        guard let d = device else { return ["ready": false, "error": lastError ?? "starting"] }
        let f = d.activeFormat
        var s = snapshot(d)
        s["ready"] = session.isRunning
        s["camera"] = d.localizedName
        s["raw"] = bayerFormat.map(fourCC) ?? NSNull()
        s["proraw_supported"] = photoOutput.isAppleProRAWSupported
        s["stale_discards"] = staleDiscards
        s["zero_shutter_lag"] = photoOutput.isZeroShutterLagEnabled
        s["limits"] = [
            "exposure_min_s": f.minExposureDuration.seconds,
            "exposure_max_s": f.maxExposureDuration.seconds,
            "iso_min": Double(f.minISO),
            "iso_max": Double(f.maxISO),
            "bias_min": Double(d.minExposureTargetBias),
            "bias_max": Double(d.maxExposureTargetBias),
            "wb_gain_max": Double(d.maxWhiteBalanceGain),
            "fov_deg": Double(f.videoFieldOfView),
        ]
        bufferLock.lock()
        s["preview_age_s"] = Date().timeIntervalSince(latestBufferTime)
        bufferLock.unlock()
        return s
    }

    /// The newest live-view frame as JPEG (portrait, at most maxWidth wide).
    func previewJPEG(maxWidth: Int, quality: Double) -> Data? {
        bufferLock.lock()
        let pb = latestBuffer
        bufferLock.unlock()
        guard let pb else { return nil }
        var img = CIImage(cvPixelBuffer: pb).oriented(.right)
        let scale = min(1, CGFloat(maxWidth) / img.extent.width)
        img = img.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
        let cs = CGColorSpace(name: CGColorSpace.sRGB)!
        let opts = [CIImageRepresentationOption(rawValue: kCGImageDestinationLossyCompressionQuality as String): quality]
        return ciContext.jpegRepresentation(of: img, colorSpace: cs, options: opts)
    }

    var exposureSeconds: Double { device?.exposureDuration.seconds ?? 1 }

    static func name(_ m: AVCaptureDevice.FocusMode) -> String {
        switch m { case .locked: return "locked"; case .autoFocus: return "auto-once"; case .continuousAutoFocus: return "continuous"; @unknown default: return "?" }
    }
    static func name(_ m: AVCaptureDevice.ExposureMode) -> String {
        switch m { case .locked: return "locked"; case .autoExpose: return "auto-once"; case .continuousAutoExposure: return "continuous"; case .custom: return "custom"; @unknown default: return "?" }
    }
    static func name(_ m: AVCaptureDevice.WhiteBalanceMode) -> String {
        switch m { case .locked: return "locked"; case .autoWhiteBalance: return "auto-once"; case .continuousAutoWhiteBalance: return "continuous"; @unknown default: return "?" }
    }
}

func fourCC(_ t: OSType) -> String {
    let chars = [24, 16, 8, 0].map { Character(UnicodeScalar(UInt8((t >> $0) & 0xFF))) }
    return String(chars).trimmingCharacters(in: .whitespaces)
}

extension CameraController: AVCapturePhotoCaptureDelegate {
    func photoOutput(_ output: AVCapturePhotoOutput, didFinishProcessingPhoto photo: AVCapturePhoto, error: Error?) {
        let id = photo.resolvedSettings.uniqueID
        let isRaw = photo.isRawPhoto
        let data = error == nil ? photo.fileDataRepresentation() : nil
        let exif = photo.metadata[kCGImagePropertyExifDictionary as String] as? [String: Any] ?? [:]
        let ts = photo.timestamp
        sessionQueue.async {
            guard var p = self.pending[id] else { return }
            if p.stale == nil {
                let afterChange = ts >= self.validAfter
                let fresh = !self.lastFrameTime.isValid || ts > self.lastFrameTime + self.sameFrame
                p.stale = !(afterChange && fresh)
                p.meta["frame_ts"] = ts.seconds
                p.meta["frame_ts_ns"] = ts.convertScale(1_000_000_000, method: .roundHalfAwayFromZero).value
                p.meta["last_frame_ts_ns"] = self.lastFrameTime.isValid ? self.lastFrameTime.convertScale(1_000_000_000, method: .roundHalfAwayFromZero).value : 0
                p.meta["frame_ts_scale"] = Int(ts.timescale)
                p.meta["request_ts"] = p.requestTime.seconds
                p.meta["valid_after_ts"] = self.validAfter.seconds
                if p.stale == false { self.lastFrameTime = ts }
            }
            if p.stale == true {
                self.pending[id] = p
                return
            }
            if let error {
                p.errors.append(error.localizedDescription)
            } else if let data {
                let ext = isRaw ? "dng" : (p.processed == "heic" ? "heic" : "jpg")
                let name = "\(p.base).\(ext)"
                do {
                    try self.store.write(data, name: name)
                    p.files.append(name)
                } catch {
                    p.errors.append("write \(name): \(error.localizedDescription)")
                }
                if isRaw {
                    p.meta["exif_exposure_s"] = exif[kCGImagePropertyExifExposureTime as String]
                    p.meta["exif_iso"] = (exif[kCGImagePropertyExifISOSpeedRatings as String] as? [Any])?.first
                }
            } else {
                p.errors.append("no image data")
            }
            self.pending[id] = p
        }
    }

    func photoOutput(_ output: AVCapturePhotoOutput, didFinishCaptureFor resolvedSettings: AVCaptureResolvedPhotoSettings, error: Error?) {
        let id = resolvedSettings.uniqueID
        sessionQueue.async {
            guard var p = self.pending.removeValue(forKey: id) else { return }
            if p.stale == true && error == nil {
                self.staleDiscards += 1
                if p.attempt < 5 {
                    p.attempt += 1
                    p.stale = nil
                    p.meta["stale_discarded"] = p.attempt
                    self.issue(p, deadline: Date().addingTimeInterval(3 * self.exposureSeconds + 5))
                    return
                }
                p.errors.append("5 stale frames in a row -- giving up on this capture")
            }
            if let error { p.errors.append(error.localizedDescription) }
            p.meta["files"] = p.files
            if !p.errors.isEmpty { p.meta["errors"] = p.errors }
            let jsonName = "\(p.base).json"
            if (try? self.store.write(JSON.data(p.meta, pretty: true), name: jsonName)) != nil {
                p.files.append(jsonName)
            }
            p.completion(p.files, p.meta, p.errors.first)
        }
    }
}

extension CameraController: AVCaptureVideoDataOutputSampleBufferDelegate {
    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        guard let pb = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        bufferLock.lock()
        latestBuffer = pb
        latestBufferTime = Date()
        latestVideoPTS = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
        bufferLock.unlock()
    }
}
