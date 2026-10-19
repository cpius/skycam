import Foundation

/// JSON helpers that never throw on NaN/inf (AVFoundation reports NaN offsets in some states).
enum JSON {
    static func sanitize(_ v: Any) -> Any {
        switch v {
        case let d as Double: return d.isFinite ? d : NSNull()
        case let f as Float: return f.isFinite ? Double(f) : NSNull()
        case let dict as [String: Any]: return dict.mapValues { sanitize($0) }
        case let arr as [Any]: return arr.map { sanitize($0) }
        default: return v
        }
    }

    static func data(_ v: Any, pretty: Bool = false) -> Data {
        let opts: JSONSerialization.WritingOptions = pretty ? [.prettyPrinted, .sortedKeys] : [.sortedKeys]
        return (try? JSONSerialization.data(withJSONObject: sanitize(v), options: opts)) ?? Data("{}".utf8)
    }

    static func string(_ v: Any) -> String { String(decoding: data(v), as: UTF8.self) }
}

private let isoFormatter: ISO8601DateFormatter = {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return f
}()

func isoNow() -> String { isoFormatter.string(from: Date()) }

func clamp<T: Comparable>(_ v: T, _ lo: T, _ hi: T) -> T { min(max(v, lo), hi) }

/// Number from a JSON value (NSNumber or numeric string).
func num(_ v: Any?) -> Double? {
    switch v {
    case let n as NSNumber: return n.doubleValue
    case let s as String: return Double(s)
    default: return nil
    }
}

/// Model identifier, e.g. "iPhone18,4".
func machineId() -> String {
    var u = utsname()
    uname(&u)
    return withUnsafeBytes(of: &u.machine) { raw in
        String(decoding: raw.prefix { $0 != 0 }, as: UTF8.self)
    }
}

/// IPv4 addresses of the Wi-Fi / ethernet interfaces, e.g. ["en0 192.168.1.23"].
func ipAddresses() -> [String] {
    var result: [String] = []
    var ifaddr: UnsafeMutablePointer<ifaddrs>?
    guard getifaddrs(&ifaddr) == 0, let first = ifaddr else { return [] }
    defer { freeifaddrs(ifaddr) }
    for ptr in sequence(first: first, next: { $0.pointee.ifa_next }) {
        let ifa = ptr.pointee
        guard let addr = ifa.ifa_addr, addr.pointee.sa_family == UInt8(AF_INET) else { continue }
        let name = String(cString: ifa.ifa_name)
        guard name.hasPrefix("en") || name.hasPrefix("bridge") else { continue }
        var host = [CChar](repeating: 0, count: Int(NI_MAXHOST))
        if getnameinfo(addr, socklen_t(addr.pointee.sa_len), &host, socklen_t(host.count), nil, 0, NI_NUMERICHOST) == 0 {
            result.append("\(name) \(String(cString: host))")
        }
    }
    return result
}

/// The captures folder (Documents/captures, also visible in the Files app).
final class FileStore {
    let dir: URL

    init() {
        let docs = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        dir = docs.appendingPathComponent("captures", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    }

    /// URL for an existing capture; rejects anything that could escape the folder.
    func url(_ name: String) -> URL? {
        guard !name.isEmpty, !name.contains("/"), !name.hasPrefix("."), !name.contains("..") else { return nil }
        let u = dir.appendingPathComponent(name)
        return FileManager.default.fileExists(atPath: u.path) ? u : nil
    }

    /// Write via a hidden temp name so a listing never shows a half-written file.
    func write(_ data: Data, name: String) throws {
        let tmp = dir.appendingPathComponent(".tmp-\(name)")
        let dst = dir.appendingPathComponent(name)
        try data.write(to: tmp)
        try? FileManager.default.removeItem(at: dst)
        try FileManager.default.moveItem(at: tmp, to: dst)
    }

    func list() -> [[String: Any]] {
        let keys: [URLResourceKey] = [.fileSizeKey, .contentModificationDateKey]
        let urls = (try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: keys,
                                                                 options: [.skipsHiddenFiles])) ?? []
        return urls.map { u -> [String: Any] in
            let rv = try? u.resourceValues(forKeys: Set(keys))
            return ["name": u.lastPathComponent,
                    "size": rv?.fileSize ?? 0,
                    "mtime": (rv?.contentModificationDate ?? Date()).timeIntervalSince1970]
        }.sorted { ($0["name"] as? String ?? "") < ($1["name"] as? String ?? "") }
    }

    @discardableResult
    func delete(_ name: String) -> Bool {
        guard let u = url(name) else { return false }
        return (try? FileManager.default.removeItem(at: u)) != nil
    }

    func freeBytes() -> Int64 {
        let rv = try? URL(fileURLWithPath: NSHomeDirectory())
            .resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey])
        return rv?.volumeAvailableCapacityForImportantUsage ?? -1
    }
}
