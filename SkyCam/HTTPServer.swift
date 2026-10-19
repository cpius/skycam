import Foundation
import Network

struct HTTPRequest {
    let method: String
    let path: String
    let query: [String: String]
    let headers: [String: String]   // lower-cased names
    let body: Data

    var json: [String: Any] {
        (try? JSONSerialization.jsonObject(with: body)) as? [String: Any] ?? [:]
    }
}

enum HTTPBody {
    case data(Data)
    case file(URL)
    case sse            // keep the connection open and hand it to the event hub
}

struct HTTPResponse {
    var status = 200
    var contentType = "application/json"
    var headers: [String: String] = [:]
    var body: HTTPBody = .data(Data())

    static func json(_ obj: Any, status: Int = 200) -> HTTPResponse {
        var d = JSON.data(obj, pretty: true)
        d.append(0x0A)
        return HTTPResponse(status: status, contentType: "application/json", body: .data(d))
    }

    static func error(_ msg: String, status: Int = 400) -> HTTPResponse {
        json(["ok": false, "error": msg], status: status)
    }
}

/// Minimal HTTP/1.1 server on Network.framework: one request per connection,
/// files streamed in 1 MB chunks, Server-Sent Events for live progress.
/// Advertised over Bonjour as _skycam._tcp.
final class HTTPServer {
    let port: UInt16
    let serviceName: String
    let queue = DispatchQueue(label: "skycam.http")
    var handler: ((HTTPRequest, @escaping (HTTPResponse) -> Void) -> Void)?
    var onSSE: ((NWConnection) -> Void)?
    var onState: ((String) -> Void)?
    private var listener: NWListener?

    init(port: UInt16, serviceName: String) {
        self.port = port
        self.serviceName = serviceName
    }

    func start() throws {
        let params = NWParameters.tcp
        params.allowLocalEndpointReuse = true
        let l = try NWListener(using: params, on: NWEndpoint.Port(rawValue: port)!)
        l.service = NWListener.Service(name: serviceName, type: "_skycam._tcp")
        l.stateUpdateHandler = { [weak self] st in self?.onState?("\(st)") }
        l.newConnectionHandler = { [weak self] c in self?.accept(c) }
        l.start(queue: queue)
        listener = l
    }

    func stop() {
        listener?.cancel()
        listener = nil
    }

    private func accept(_ c: NWConnection) {
        c.start(queue: queue)
        read(c, Data())
    }

    private func read(_ c: NWConnection, _ soFar: Data) {
        c.receive(minimumIncompleteLength: 1, maximumLength: 1 << 16) { [weak self] data, _, done, err in
            guard let self else { return }
            var buf = soFar
            if let data { buf.append(data) }
            if err != nil { c.cancel(); return }
            switch Self.parse(buf) {
            case .needMore:
                if done || buf.count > 8 << 20 { c.cancel() } else { self.read(c, buf) }
            case .bad:
                self.send(c, .error("bad request"))
            case .ok(let req):
                guard let h = self.handler else { c.cancel(); return }
                h(req) { resp in self.queue.async { self.send(c, resp) } }
            }
        }
    }

    enum ParseResult { case needMore, bad, ok(HTTPRequest) }

    static func parse(_ buf: Data) -> ParseResult {
        guard let r = buf.range(of: Data("\r\n\r\n".utf8)) else { return buf.count > 65536 ? .bad : .needMore }
        guard let head = String(data: buf[buf.startIndex..<r.lowerBound], encoding: .utf8) else { return .bad }
        var lines = head.components(separatedBy: "\r\n")
        let reqLine = lines.removeFirst().split(separator: " ")
        guard reqLine.count >= 2 else { return .bad }
        var headers: [String: String] = [:]
        for l in lines {
            guard let i = l.firstIndex(of: ":") else { continue }
            headers[l[..<i].trimmingCharacters(in: .whitespaces).lowercased()] =
                l[l.index(after: i)...].trimmingCharacters(in: .whitespaces)
        }
        let len = Int(headers["content-length"] ?? "0") ?? 0
        let bodyStart = r.upperBound
        guard buf.endIndex - bodyStart >= len else { return .needMore }
        let body = Data(buf[bodyStart..<(bodyStart + len)])
        guard let comps = URLComponents(string: String(reqLine[1])) else { return .bad }
        var q: [String: String] = [:]
        for item in comps.queryItems ?? [] { q[item.name] = item.value ?? "" }
        return .ok(HTTPRequest(method: String(reqLine[0]).uppercased(), path: comps.path,
                               query: q, headers: headers, body: body))
    }

    private func header(_ r: HTTPResponse, extra: [String: String]) -> Data {
        var h = r.headers
        h["Content-Type"] = r.contentType
        h["Access-Control-Allow-Origin"] = "*"
        for (k, v) in extra { h[k] = v }
        var s = "HTTP/1.1 \(r.status) \(Self.reason(r.status))\r\n"
        for (k, v) in h.sorted(by: { $0.key < $1.key }) { s += "\(k): \(v)\r\n" }
        s += "\r\n"
        return Data(s.utf8)
    }

    private func send(_ c: NWConnection, _ r: HTTPResponse) {
        switch r.body {
        case .data(let d):
            var out = header(r, extra: ["Content-Length": "\(d.count)", "Connection": "close"])
            out.append(d)
            c.send(content: out, contentContext: .finalMessage, isComplete: true,
                   completion: .contentProcessed { _ in c.cancel() })
        case .file(let url):
            guard let fh = try? FileHandle(forReadingFrom: url),
                  let size = (try? FileManager.default.attributesOfItem(atPath: url.path))?[.size] as? NSNumber
            else { send(c, .error("not found", status: 404)); return }
            let head = header(r, extra: ["Content-Length": "\(size.intValue)", "Connection": "close"])
            c.send(content: head, completion: .contentProcessed { [weak self] err in
                if err != nil { try? fh.close(); c.cancel(); return }
                self?.queue.async { self?.pump(c, fh) }
            })
        case .sse:
            let head = header(r, extra: ["Cache-Control": "no-cache", "Connection": "keep-alive"])
            c.send(content: head, completion: .contentProcessed { [weak self] _ in self?.onSSE?(c) })
        }
    }

    /// Stream a file 1 MB at a time, waiting for each chunk to drain so memory stays flat.
    private func pump(_ c: NWConnection, _ fh: FileHandle) {
        let chunk = (try? fh.read(upToCount: 1 << 20)) ?? nil
        guard let chunk, !chunk.isEmpty else {
            try? fh.close()
            c.send(content: nil, contentContext: .finalMessage, isComplete: true,
                   completion: .contentProcessed { _ in c.cancel() })
            return
        }
        c.send(content: chunk, completion: .contentProcessed { [weak self] err in
            if err != nil { try? fh.close(); c.cancel(); return }
            self?.queue.async { self?.pump(c, fh) }
        })
    }

    static func reason(_ s: Int) -> String {
        switch s {
        case 200: return "OK"
        case 202: return "Accepted"
        case 400: return "Bad Request"
        case 401: return "Unauthorized"
        case 404: return "Not Found"
        case 409: return "Conflict"
        case 503: return "Service Unavailable"
        default: return "Error"
        }
    }
}
