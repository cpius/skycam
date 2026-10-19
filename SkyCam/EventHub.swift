import Foundation
import Network

/// Live event stream: every event goes to all Server-Sent-Events clients and into a
/// ring buffer that /events/poll?since=<id> can read back.
final class EventHub {
    private let queue = DispatchQueue(label: "skycam.events")
    private var clients: [ObjectIdentifier: NWConnection] = [:]
    private var ring: [[String: Any]] = []
    private var nextId = 0

    func add(_ c: NWConnection) {
        queue.async {
            let key = ObjectIdentifier(c)
            self.clients[key] = c
            c.stateUpdateHandler = { st in
                switch st {
                case .failed, .cancelled: self.queue.async { self.clients[key] = nil }
                default: break
                }
            }
            c.send(content: Data("retry: 2000\n\n".utf8), completion: .contentProcessed { _ in })
        }
    }

    func publish(_ type: String, _ payload: [String: Any] = [:]) {
        queue.async {
            self.nextId += 1
            var ev = payload
            ev["type"] = type
            ev["id"] = self.nextId
            ev["t"] = isoNow()
            self.ring.append(ev)
            if self.ring.count > 2000 { self.ring.removeFirst(self.ring.count - 2000) }
            let data = Data("id: \(self.nextId)\nevent: \(type)\ndata: \(JSON.string(ev))\n\n".utf8)
            for (key, c) in self.clients {
                c.send(content: data, completion: .contentProcessed { err in
                    guard err != nil else { return }
                    self.queue.async { self.clients[key] = nil }
                    c.cancel()
                })
            }
        }
    }

    func since(_ id: Int, limit: Int = 500) -> [[String: Any]] {
        queue.sync { Array(ring.filter { ($0["id"] as? Int ?? 0) > id }.suffix(limit)) }
    }

    var lastId: Int { queue.sync { nextId } }
    var clientCount: Int { queue.sync { clients.count } }
}
