#!/usr/bin/env python3
"""Drive the SkyCam iPhone app over Wi-Fi: settings, RAW capture, sequences, downloads.

    python3 -u client/skycam.py pair                     # read token + address off the phone (once)
    python3 -u client/skycam.py status
    python3 -u client/skycam.py set --focus 0.92 --exp 1 --iso 3200 --wb 4000
    python3 -u client/skycam.py af                       # one autofocus scan, prints the lens position
    python3 -u client/skycam.py shoot --count 30 --exp 1 --iso 3200 --focus 0.92 --out frames/
    python3 -u client/skycam.py bracket --focus-from 0.86 --focus-to 0.98 --focus-step 0.01 --exp 1 --iso 3200 --out focus/
    python3 -u client/skycam.py pull --out frames/ [--follow] [--delete]
    python3 -u client/skycam.py preview --out preview.jpg
    python3 -u client/skycam.py events                   # live log, heartbeat every 5 s
    python3 -u client/skycam.py stop

Focus is the AVFoundation lens position, 0 (closest) .. 1 (furthest); it is NOT NightCap's FOC number.
Every frame comes as <base>.dng plus <base>.json holding the lens position, exposure, ISO and WB used.
Connection details live in ~/.config/skycam.json (written by `pair`); --host/--token override them.
Standard library only.
"""
import argparse, json, os, queue, subprocess, sys, tempfile, threading, time, urllib.error, urllib.request

CONFIG = os.path.expanduser("~/.config/skycam.json")
DEFAULT_DEVICE = "00008150-00124D061A38401C"   # MD iPhone Air
BUNDLE_ID = "dk.dorup.skycam"


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_cfg():
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_cfg(cfg):
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    with open(CONFIG, "w") as f:
        json.dump(cfg, f, indent=2)


class Phone:
    def __init__(self, host, port, token):
        if not host or not token:
            sys.exit("no phone configured: run `skycam.py pair` (phone unlocked, SkyCam open) or pass --host and --token")
        self.base = f"http://{host}:{port}"
        self.token = token

    def _req(self, method, path, body=None, timeout=30):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        return urllib.request.urlopen(req, timeout=timeout)

    def call(self, method, path, body=None, timeout=30):
        try:
            with self._req(method, path, body, timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read())
            except Exception:
                return {"ok": False, "error": f"HTTP {e.code}"}

    def download(self, name, outdir):
        """Fetch one file to outdir via a .part name; skip it if a same-size copy exists."""
        os.makedirs(outdir, exist_ok=True)
        dst = os.path.join(outdir, name)
        part = dst + ".part"
        t0 = time.time()
        with self._req("GET", "/files/" + name, timeout=60) as r:
            size = int(r.headers.get("Content-Length", -1))
            if os.path.exists(dst) and os.path.getsize(dst) == size:
                return size, 0.0, True
            got = 0
            with open(part, "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
        if size >= 0 and got != size:
            raise IOError(f"{name}: got {got} of {size} bytes")
        os.replace(part, dst)
        return got, time.time() - t0, False

    def events(self, timeout=20):
        """Yield events from the SSE stream; reconnects after a drop."""
        while True:
            try:
                with self._req("GET", "/events", timeout=timeout) as r:
                    for raw in r:
                        line = raw.decode("utf-8", "replace").rstrip("\n")
                        if line.startswith("data: "):
                            yield json.loads(line[6:])
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                log(f"event stream dropped ({e}); reconnecting in 2 s")
                time.sleep(2)


def phone_from(args):
    cfg = load_cfg()
    return Phone(args.host or cfg.get("host"), args.port or cfg.get("port", 8070), args.token or cfg.get("token"))


def show(obj):
    print(json.dumps(obj, indent=2, sort_keys=True), flush=True)


def settings_from(args):
    s = {}
    if getattr(args, "focus", None) is not None:
        s["focus"] = args.focus if args.focus in ("auto", "lock") else float(args.focus)
    if getattr(args, "exp", None) is not None:
        s["exposure"] = "auto" if args.exp == "auto" else float(args.exp)
    if getattr(args, "iso", None) is not None:
        s["iso"] = args.iso
    if getattr(args, "bias", None) is not None:
        s["bias"] = args.bias
    wb = getattr(args, "wb", None)
    if wb is not None:
        s["wb"] = wb if wb in ("auto", "lock") else {"temp": float(wb.rstrip("Kk"))}
    return s


# --- commands ---------------------------------------------------------------

def cmd_pair(args):
    log(f"reading connection.json from SkyCam on device {args.device} ...")
    with tempfile.TemporaryDirectory() as td:
        dst = os.path.join(td, "connection.json")
        cmd = ["xcrun", "devicectl", "device", "copy", "from", "--device", args.device,
               "--domain-type", "appDataContainer", "--domain-identifier", BUNDLE_ID,
               "--source", "Documents/connection.json", "--destination", dst]
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0 or not os.path.exists(dst):
            sys.exit(f"devicectl failed (is the phone unlocked and has SkyCam run once?):\n{p.stdout}{p.stderr}")
        with open(dst) as f:
            conn = json.load(f)
    for ip in conn.get("addresses", []):
        try:
            with urllib.request.urlopen(f"http://{ip}:{conn['port']}/ping", timeout=3) as r:
                info = json.loads(r.read())
            save_cfg({"host": ip, "port": conn["port"], "token": conn["token"]})
            log(f"paired: {info.get('app')} {info.get('version')} on {info.get('device')} at {ip}:{conn['port']} -> {CONFIG}")
            return
        except Exception as e:
            log(f"{ip}: not reachable ({e})")
    sys.exit("phone not reachable on any address: same Wi-Fi? SkyCam in the foreground? Local Network allowed?")


def cmd_status(args):
    show(phone_from(args).call("GET", "/status"))


def cmd_set(args):
    s = settings_from(args)
    if not s:
        sys.exit("nothing to set: give --focus, --exp, --iso, --bias or --wb")
    r = phone_from(args).call("POST", "/settings", s, timeout=30)
    if not r.get("ok"):
        sys.exit(f"error: {r.get('error')}")
    c = r["camera"]
    log(f"focus {c['lens_position']:.3f} ({c['focus_mode']})  exposure {c['exposure_s']:.4g} s ({c['exposure_mode']})  "
        f"ISO {c['iso']:.0f}  WB {c['wb_temp']:.0f} K ({c['wb_mode']})")
    for n in r.get("notes", []):
        log(f"note: {n}")


def cmd_af(args):
    body = {"lock": not args.no_lock}
    if args.x is not None and args.y is not None:
        body.update(x=args.x, y=args.y)
    r = phone_from(args).call("POST", "/af", body, timeout=20)
    log(f"autofocus -> lens position {r.get('lens_position')}" + (f"  ({r['error']})" if r.get("error") else ""))


def cmd_preview(args):
    ph = phone_from(args)
    with ph._req("GET", f"/preview.jpg?w={args.width}") as r, open(args.out, "wb") as f:
        f.write(r.read())
    log(f"wrote {args.out}")


def cmd_stop(args):
    show(phone_from(args).call("POST", "/sequence/stop"))


def cmd_events(args):
    for ev in phone_from(args).events():
        print_event(ev)


def print_event(ev):
    t = ev.get("type")
    if t == "heartbeat":
        seq = ev.get("sequence", {})
        lp, e, iso = ev.get("lens_position"), ev.get("exposure_s"), ev.get("iso")
        log(f"♥ battery {ev.get('battery', -1) * 100:.0f}%  thermal {ev.get('thermal')}  "
            f"free {ev.get('free_bytes', 0) / 1e9:.1f} GB  focus {lp if lp is None else round(lp, 3)}  "
            f"exp {e if e is None else round(e, 4)} s  ISO {iso if iso is None else round(iso)}  "
            f"seq {seq.get('state')} {seq.get('index')}/{seq.get('total')}")
    elif t == "frame":
        lp = ev.get("lens_position")
        log(f"frame {ev.get('index', '-')}/{ev.get('total', '-')}  focus {lp if lp is None else round(lp, 3)}  "
            f"exp {ev.get('exposure_s')}  ISO {ev.get('iso')}  {', '.join(ev.get('files', []))}"
            + (f"  ERROR {ev['error']}" if ev.get("error") else ""))
    elif t == "log":
        log(f"phone: {ev.get('message')}")
    else:
        log(f"{t}: {json.dumps({k: v for k, v in ev.items() if k not in ('type', 't', 'id')})}")


def downloader(ph, outdir, q, done, delete):
    """Background thread: download (and optionally delete on the phone) every queued file."""
    total = 0
    while True:
        try:
            name = q.get(timeout=0.5)
        except queue.Empty:
            if done.is_set():
                return
            continue
        for attempt in range(3):
            try:
                n, dt, skipped = ph.download(name, outdir)
                total += n
                if not skipped:
                    rate = n / dt / 1e6 if dt > 0 else 0
                    log(f"  ↓ {name}  {n / 1e6:.1f} MB  {rate:.1f} MB/s")
                if delete:
                    ph.call("DELETE", "/files/" + name)
                break
            except Exception as e:
                log(f"  ↓ {name} failed ({e}); retry {attempt + 1}/3")
                time.sleep(2)


def run_sequence(args, program):
    ph = phone_from(args)
    r = ph.call("POST", "/sequence", program)
    if not r.get("ok"):
        sys.exit(f"phone refused the sequence: {r.get('error')}")
    seq = r["sequence"]
    log(f"sequence {seq['id']} started: {seq['total']} frames")
    q, done = queue.Queue(), threading.Event()
    th = None
    if args.out:
        th = threading.Thread(target=downloader, args=(ph, args.out, q, done, args.delete), daemon=True)
        th.start()
    for ev in ph.events():
        if ev.get("seq") not in (None, seq["id"]) and ev.get("type") != "heartbeat":
            continue
        print_event(ev)
        if ev.get("type") == "frame" and args.out:
            for f in ev.get("files", []):
                q.put(f)
        if ev.get("type") == "sequence_done" and ev.get("seq") == seq["id"]:
            break
    if th:
        log(f"waiting for {q.qsize()} downloads ...")
        done.set()
        th.join()
        log(f"files in {os.path.abspath(args.out)}")


def cmd_shoot(args):
    program = {"name": args.name, "count": args.count, "interval": args.interval, "settle": args.settle,
               "jpeg": args.jpeg, **settings_from(args)}
    run_sequence(args, program)


def cmd_bracket(args):
    program = {"name": args.name, "focus_bracket": {"from": args.focus_from, "to": args.focus_to, "step": args.focus_step},
               "per_value": args.per_value, "interval": args.interval, "settle": args.settle, "jpeg": args.jpeg,
               **settings_from(args)}
    run_sequence(args, program)


def cmd_pull(args):
    ph = phone_from(args)
    have = set()
    def sync():
        for f in ph.call("GET", "/files")["files"]:
            if f["name"] in have:
                continue
            n, dt, skipped = ph.download(f["name"], args.out)
            have.add(f["name"])
            if not skipped:
                log(f"↓ {f['name']}  {n / 1e6:.1f} MB  {n / dt / 1e6 if dt else 0:.1f} MB/s")
            if args.delete:
                ph.call("DELETE", "/files/" + f["name"])
    sync()
    if args.follow:
        for ev in ph.events():
            if ev.get("type") in ("frame", "heartbeat"):
                print_event(ev)
                sync()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host"); ap.add_argument("--port", type=int); ap.add_argument("--token")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def cam_flags(p):
        p.add_argument("--focus", help="lens position 0..1, or auto / lock")
        p.add_argument("--exp", help="exposure in seconds, or auto")
        p.add_argument("--iso", type=float)
        p.add_argument("--bias", type=float, help="EV bias (auto exposure only)")
        p.add_argument("--wb", help="auto, lock, or a temperature like 4000K")

    def seq_flags(p):
        cam_flags(p)
        p.add_argument("--interval", type=float, default=0, help="seconds start-to-start (0 = back to back)")
        p.add_argument("--settle", type=float, default=0.5, help="seconds to wait after a settings change")
        p.add_argument("--jpeg", action="store_true", help="also save a processed JPEG")
        p.add_argument("--out", help="download each frame here as it is taken")
        p.add_argument("--delete", action="store_true", help="delete from the phone once downloaded")

    p = sub.add_parser("pair"); p.add_argument("--device", default=DEFAULT_DEVICE); p.set_defaults(fn=cmd_pair)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    p = sub.add_parser("set"); cam_flags(p); p.set_defaults(fn=cmd_set)
    p = sub.add_parser("af"); p.add_argument("--x", type=float); p.add_argument("--y", type=float)
    p.add_argument("--no-lock", action="store_true"); p.set_defaults(fn=cmd_af)
    p = sub.add_parser("shoot"); seq_flags(p); p.add_argument("--count", type=int, default=1)
    p.add_argument("--name", default="shoot"); p.set_defaults(fn=cmd_shoot)
    p = sub.add_parser("bracket"); seq_flags(p)
    p.add_argument("--focus-from", type=float, required=True); p.add_argument("--focus-to", type=float, required=True)
    p.add_argument("--focus-step", type=float, default=0.01); p.add_argument("--per-value", type=int, default=1)
    p.add_argument("--name", default="focus"); p.set_defaults(fn=cmd_bracket)
    p = sub.add_parser("pull"); p.add_argument("--out", required=True); p.add_argument("--follow", action="store_true")
    p.add_argument("--delete", action="store_true"); p.set_defaults(fn=cmd_pull)
    p = sub.add_parser("preview"); p.add_argument("--out", default="preview.jpg"); p.add_argument("--width", type=int, default=1280)
    p.set_defaults(fn=cmd_preview)
    sub.add_parser("events").set_defaults(fn=cmd_events)
    sub.add_parser("stop").set_defaults(fn=cmd_stop)

    args = ap.parse_args()
    if getattr(args, "focus", None) not in (None, "auto", "lock"):
        float(args.focus)   # fail early on a typo
    try:
        args.fn(args)
    except KeyboardInterrupt:
        log("interrupted (a running sequence keeps going on the phone; `skycam.py stop` ends it)")


if __name__ == "__main__":
    main()
