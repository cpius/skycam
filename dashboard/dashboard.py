#!/usr/bin/env python3
"""The SkyCam dashboard: phone status, live preview, frames, focus, every event the phone sends.

Watches the phone and serves what it sees as a page that refreshes itself. It only ever sends
GET requests -- ReadOnlyPhone refuses anything else before it reaches the socket -- so it can be
left running beside a sequence, a focus bracket, or another session driving the phone, and it
cannot change a setting or stop a run. That separation is the point: the dashboard must never be
able to disturb the session it is watching.

What it reads from the phone, and how often:

    GET /status                every --status-every s (the phone does not log these)
    GET /events                Server-Sent Events, held open; frame, sequence_*, note, error,
                               log, and a heartbeat every 5 s
    GET /events/poll?since=N   on every (re)connect, to fill the gap the stream missed
    GET /preview.jpg?w=900     only while a browser is looking, at most every 2 s

It deliberately does NOT poll /files or fetch sidecars from the phone: the phone logs every one of
those requests as an event, which would spam the log of whoever is driving it. Frame details come
from the frame events, and from the DNG + JSON pairs already downloaded to the Mac under
--frames; quick-look JPEGs are rendered from those DNGs (dashboard/quicklook.py, via
~/ASICAP/.venv when this interpreter has no rawpy).

Six panes:

    phone      battery (with time left), thermal, free space, camera settings, sequence progress
    preview    the live view, refreshed every few seconds
    focus      the newest focustrack.py / focusraw.py result under --frames: V-curve and best
    frames     the newest frames, quick-look from the DNG when it is on the Mac
    commands   every HTTP request the phone logged (what other clients are telling it to do)
    messages   every event, newest first, split by type

Every event is also appended to --data/events-YYYY-MM-DD.jsonl, so a restart of the dashboard picks
the story back up (the phone itself only replays its newest 500).

    python3 -u dashboard/dashboard.py --port 8768                       # http://localhost:8768
    python3 -u dashboard/dashboard.py --port 8768 --bind 0.0.0.0        # reachable from the phone
    python3 -u dashboard/dashboard.py --once > state.json               # one state dump, no server
"""

import argparse
import collections
import datetime
import hashlib
import http.server
import json
import mimetypes
import os
import re
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "client"))

import skycam  # noqa: E402  (client/skycam.py: Phone, load_cfg)



def log(msg):
    """skycam.log's format, on stderr so --once leaves stdout as pure JSON."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


DATA = os.path.join(HERE, "data")
CACHE = os.path.expanduser("~/.cache/skycam-dashboard")
VENV_PY = os.path.expanduser("~/ASICAP/.venv/bin/python3")

MAX_EVENTS = 6000          # in memory; the JSONL on disk keeps everything
MAX_PER_KIND = 250         # per message tab
MAX_COMMANDS = 300
MAX_FRAMES = 24
HEARTBEAT_EVERY = 15       # terminal heartbeat, seconds
PREVIEW_TTL = 2.0          # seconds a fetched preview is reused across browsers
SCAN_TTL = 3.0             # seconds a walk of --frames is reused
PRUNE = {".git", "build", "__pycache__", "SkyCam", "SkyCam.xcodeproj", "data", "node_modules"}
HTTP_LINE = re.compile(r"^(GET|POST|DELETE|PUT|PATCH)\s+(\S+)")
ALARM = re.compile(r"fail|error|background|cannot|timed out|not run|denied|crash", re.I)


class ReadOnlyPhone(skycam.Phone):
    """skycam.Phone that can only GET. Anything else raises before a byte is sent."""

    def _req(self, method, path, body=None, timeout=30):
        if method != "GET":
            raise PermissionError(f"dashboard is read-only: refused {method} {path}")
        return super()._req(method, path, body, timeout)


def _age(t):
    return None if t is None else round(time.time() - t, 1)


def _iso_epoch(iso):
    """Phone timestamps are UTC with a Z; Python before 3.11 cannot parse the Z itself."""
    try:
        return datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# The watcher: one thread on /status, one on the event stream
# ---------------------------------------------------------------------------

class Watch:
    def __init__(self, phone, data_dir, status_every):
        self.ph = phone
        self.data_dir = data_dir
        self.status_every = status_every
        self.lock = threading.Lock()
        self.events = collections.deque(maxlen=MAX_EVENTS)
        self.keys = set()
        self.last_id = 0
        self.status, self.status_at, self.status_err, self.status_fails = None, None, None, 0
        self.sse_connected, self.sse_err, self.sse_since = False, None, None
        self.event_at = None           # wall time the newest event arrived
        self.heartbeat, self.heartbeat_at = None, None
        self.reconnects = 0
        self._filled = False
        self.browsers = {}             # ip -> last /api/state time
        self._preview = {"at": 0, "jpg": None, "err": None, "phone_at": None}
        self._preview_lock = threading.Lock()
        self._load_log()

    # -- the log on disk ----------------------------------------------------

    def _logfile(self):
        return os.path.join(self.data_dir, "events-%s.jsonl" % datetime.date.today().isoformat())

    def _load_log(self, tail_bytes=4_000_000):
        """Reload the newest day's events, so a restart does not start from an empty page."""
        try:
            files = sorted(f for f in os.listdir(self.data_dir) if f.startswith("events-"))
        except FileNotFoundError:
            return
        if not files:
            return
        path = os.path.join(self.data_dir, files[-1])
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
                f.readline()                       # drop the partial first line
            for line in f.read().decode("utf-8", "replace").splitlines():
                try:
                    self._remember(json.loads(line))
                except ValueError:
                    continue
        log(f"reloaded {len(self.events)} events from {path}")

    def _remember(self, ev):
        """Keep one event in memory. False if it was already there."""
        key = (ev.get("id"), ev.get("t"))
        if key in self.keys:
            return False
        if len(self.events) == self.events.maxlen:
            old = self.events[0]
            self.keys.discard((old.get("id"), old.get("t")))
        self.events.append(ev)
        self.keys.add(key)
        if ev.get("type") == "heartbeat":
            self.heartbeat = ev
        return True

    def add(self, ev, echo=True):
        with self.lock:
            if not self._remember(ev):
                return
            if isinstance(ev.get("id"), int) and ev.get("type") != "dashboard":
                self.last_id = max(self.last_id, ev["id"])
            self.event_at = time.time()
            if ev.get("type") == "heartbeat":
                self.heartbeat_at = self.event_at
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            with open(self._logfile(), "a") as f:
                f.write(json.dumps(ev) + "\n")
        except OSError as e:
            log(f"could not append to the event log: {e}")
        if echo:
            self._echo(ev)

    def note(self, message):
        """An event of our own, so link drops show up in the message log too."""
        self.add({"type": "dashboard", "message": message,
                  "t": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")
                  .replace("+00:00", "Z")})

    @staticmethod
    def _echo(ev):
        t = ev.get("type")
        if t in ("sequence_started", "sequence_done", "error", "dashboard"):
            body = {k: v for k, v in ev.items() if k not in ("type", "t", "id")}
            log(f"{t}: {json.dumps(body)}")
        elif t == "log" and ALARM.search(str(ev.get("message", ""))):
            log(f"phone: {ev.get('message')}")

    # -- threads ------------------------------------------------------------

    def start(self):
        threading.Thread(target=self._status_loop, name="status", daemon=True).start()
        threading.Thread(target=self._event_loop, name="events", daemon=True).start()

    def poll_status_once(self):
        try:
            s = self.ph.call("GET", "/status", timeout=5)
            if "camera" not in s:
                raise IOError(s.get("error") or "unexpected reply")
            with self.lock:
                self.status, self.status_at, self.status_err = s, time.time(), None
                was, self.status_fails = self.status_fails, 0
            if was >= 3:
                self.note(f"phone answering /status again after {was} failures")
        except Exception as e:
            with self.lock:
                self.status_err = str(e)
                self.status_fails += 1
                n = self.status_fails
            if n == 3:
                self.note(f"phone not answering /status: {e}")

    def _status_loop(self):
        while True:
            t0 = time.time()
            self.poll_status_once()
            time.sleep(max(0.5, self.status_every - (time.time() - t0)))

    def backfill(self):
        """Fetch what the stream missed. Ids going backwards mean the app was relaunched."""
        r = self.ph.call("GET", f"/events/poll?since={self.last_id}", timeout=10)
        if "events" not in r:
            raise IOError(r.get("error") or "unexpected reply to /events/poll")
        if r.get("last_id", 0) < self.last_id:
            self.note(f"phone event ids restarted ({self.last_id} -> {r.get('last_id')}): app relaunched?")
            with self.lock:
                self.last_id = 0
            r = self.ph.call("GET", "/events/poll?since=0", timeout=10)
        # The first fill is history: echoing it would print old runs as if they were happening now.
        for ev in r.get("events", []):
            self.add(ev, echo=self._filled)
        self._filled = True

    def _event_loop(self):
        while True:
            try:
                self.backfill()
                with self.ph._req("GET", "/events", timeout=20) as resp:
                    with self.lock:
                        self.sse_connected, self.sse_err, self.sse_since = True, None, time.time()
                    for raw in resp:
                        line = raw.decode("utf-8", "replace").rstrip("\r\n")
                        if line.startswith("data: "):
                            try:
                                self.add(json.loads(line[6:]))
                            except ValueError:
                                continue
                err = "stream closed by the phone"
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
            with self.lock:
                was = self.sse_connected
                self.sse_connected, self.sse_err = False, err
                self.reconnects += 1
            if was:
                self.note(f"event stream dropped ({err}); reconnecting")
            time.sleep(2)

    # -- preview ------------------------------------------------------------

    def preview(self, width):
        """Newest live-view JPEG, shared by every browser for PREVIEW_TTL seconds."""
        with self._preview_lock:
            p = self._preview
            if p["jpg"] and time.time() - p["at"] < PREVIEW_TTL:
                return p["jpg"], p["at"], None
            try:
                with self.ph._req("GET", f"/preview.jpg?w={width}", timeout=8) as r:
                    jpg = r.read()
                p.update(at=time.time(), jpg=jpg, err=None)
                return jpg, p["at"], None
            except Exception as e:
                p["err"] = str(e)
                return p["jpg"], p["at"], str(e)    # the last good frame, marked stale

    def preview_info(self):
        # No lock: preview() holds it across the phone request, and /api/state must not wait.
        p = dict(self._preview)
        return {"age_s": _age(p["at"]) if p["jpg"] else None, "error": p["err"]}

    def touch_browser(self, ip):
        with self.lock:
            self.browsers[ip] = time.time()

    def browsers_now(self, within=20):
        with self.lock:
            return sum(1 for t in self.browsers.values() if time.time() - t < within)


# ---------------------------------------------------------------------------
# Frames on the Mac
# ---------------------------------------------------------------------------

_scan = {"at": 0, "roots": None, "dngs": [], "results": []}
_scan_lock = threading.Lock()
_sidecars = {}


def scan(roots):
    """All DNGs and focus result files under the roots, cached for SCAN_TTL seconds."""
    with _scan_lock:
        if _scan["roots"] == roots and time.time() - _scan["at"] < SCAN_TTL:
            return _scan["dngs"], _scan["results"]
    dngs, results = [], []
    for root in roots:
        for d, subdirs, files in os.walk(root):
            subdirs[:] = [s for s in subdirs if s not in PRUNE and not s.startswith(".")]
            for f in files:
                p = os.path.join(d, f)
                try:
                    if f.endswith(".dng"):
                        dngs.append((os.path.getmtime(p), p))
                    elif f in ("track.txt", "focus.txt"):
                        results.append((os.path.getmtime(p), p))
                except OSError:
                    continue          # deleted between the walk and the stat
    dngs.sort(reverse=True)
    results.sort(reverse=True)
    with _scan_lock:
        _scan.update(at=time.time(), roots=roots, dngs=dngs, results=results)
    return dngs, results


def sidecar(dng):
    j = dng[:-4] + ".json"
    try:
        m = os.path.getmtime(j)
    except OSError:
        return None
    hit = _sidecars.get(j)
    if hit and hit[0] == m:
        return hit[1]
    try:
        with open(j) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    _sidecars[j] = (m, data)
    return data


SIDECAR_KEYS = ("lens_position", "exposure_s", "iso", "wb_temp", "wb_tint", "focus_mode",
                "adjusting_focus", "exposure_mode", "captured_at", "frame_ts", "sequence", "tag", "index")


def build_frames(events, roots):
    """Newest frames, from the phone's frame events and the DNGs already on the Mac.

    A frame the phone announced and the Mac also has is one entry, with the sidecar's fields
    (which are the ones written into the file) taking precedence over the event's.
    """
    dngs, _ = scan(roots)
    local = {}
    for m, p in dngs[:MAX_FRAMES * 4]:
        local.setdefault(os.path.basename(p), (m, p))
    out = {}
    for ev in events:
        if ev.get("type") != "frame":
            continue
        name = next((f for f in ev.get("files", []) if f.endswith(".dng")),
                    (ev.get("files") or [None])[0])
        if not name:
            continue
        out[name] = {"name": name, "t": ev.get("t"), "seq": ev.get("seq"), "index": ev.get("index"),
                     "total": ev.get("total"), "lens_position": ev.get("lens_position"),
                     "exposure_s": ev.get("exposure_s"), "iso": ev.get("iso"), "tag": ev.get("tag"),
                     "error": ev.get("error"), "files": ev.get("files"), "path": None, "sidecar": None}
    for name, (m, p) in local.items():
        e = out.setdefault(name, {"name": name, "t": None, "seq": None, "index": None, "total": None,
                                  "error": None, "path": None, "sidecar": None})
        e["path"] = p
        e["mtime"] = m
        sc = sidecar(p)
        if sc:
            e["sidecar"] = {k: sc.get(k) for k in SIDECAR_KEYS if k in sc}
            for k in ("lens_position", "exposure_s", "iso", "tag", "index"):
                if sc.get(k) is not None:
                    e[k] = sc[k]
            e["seq"] = e.get("seq") or sc.get("sequence")
            e["t"] = e["t"] or sc.get("captured_at")
        if not e["t"]:
            e["t"] = datetime.datetime.fromtimestamp(m, datetime.timezone.utc).isoformat()
    rows = sorted(out.values(), key=lambda e: _iso_epoch(e["t"]) or 0, reverse=True)
    return rows[:MAX_FRAMES]


# ---------------------------------------------------------------------------
# Focus results (focustrack.py writes track.txt, focusraw.py focus.txt, via tee)
# ---------------------------------------------------------------------------

CURVE = re.compile(r"^\s+(\d\.\d+)\s+(?:FWHM\s+([\d.]+)\s+)?HFR\s+([\d.]+)(?:\s+stars\s+(\d+))?")
FRAME = re.compile(r"\blens\s+(\d\.\d+)\s+(?:stars\s+(\d+)\s+FWHM\s+([\d.]+)\s*px\s+)?HFR\s+([\d.]+)\s*px")
BEST = re.compile(r"(?:best|sharpest):\s*lens position\s+(\d\.\d+)\s*\(HFR\s+([\d.]+)")
REF = re.compile(r"reference frame .*\(lens\s+(\d\.\d+)\):\s*(\d+)\s+stars")


def read_focus(path):
    if not path or not os.path.isfile(path):
        return None
    curve, frames, best, ref = [], [], None, None
    with open(path, errors="replace") as f:
        for line in f:
            m = CURVE.match(line)
            if m:
                curve.append({"lens": float(m[1]), "fwhm": m[2] and float(m[2]), "hfr": float(m[3]),
                              "stars": m[4] and int(m[4])})
                continue
            m = FRAME.search(line)
            if m:
                frames.append({"lens": float(m[1]), "hfr": float(m[4]),
                               "stars": m[2] and int(m[2]), "fwhm": m[3] and float(m[3])})
                continue
            m = BEST.search(line)
            if m:
                best = {"lens": float(m[1]), "hfr": float(m[2])}
                continue
            m = REF.search(line)
            if m:
                ref = {"lens": float(m[1]), "stars": int(m[2])}
    d = os.path.dirname(path)
    montage = os.path.join(d, "stars.jpg")
    n_dng = sum(1 for f in os.listdir(d) if f.endswith(".dng"))
    return {
        "path": path, "dir": d, "name": os.path.relpath(d, os.path.dirname(os.path.dirname(d))),
        "tool": "focustrack" if os.path.basename(path) == "track.txt" else "focusraw",
        "mtime": datetime.datetime.fromtimestamp(os.path.getmtime(path)).isoformat(timespec="seconds"),
        "curve": curve, "frames": frames, "best": best, "reference": ref, "dngs": n_dng,
        "vertex": _vertex([(c["lens"], c["hfr"]) for c in curve]),
        "montage": montage if os.path.exists(montage) else None,
    }


def _vertex(pts):
    """Parabola through the minimum and its neighbours, as in the ASIAIR dashboard."""
    pts = sorted(p for p in pts if p[0] is not None and p[1] is not None)
    if len(pts) < 3:
        return None
    i = min(range(len(pts)), key=lambda k: pts[k][1])
    i = max(1, min(len(pts) - 2, i))
    (x1, y1), (x2, y2), (x3, y3) = pts[i - 1], pts[i], pts[i + 1]
    d = (x1 - x2) * (x1 - x3) * (x2 - x3)
    if d == 0:
        return None
    a = (x3 * (y2 - y1) + x2 * (y1 - y3) + x1 * (y3 - y2)) / d
    b = (x3 * x3 * (y1 - y2) + x2 * x2 * (y3 - y1) + x1 * x1 * (y2 - y3)) / d
    if a <= 0:
        return None                                  # opens downward: no minimum
    v = -b / (2 * a)
    return round(v, 4) if pts[0][0] <= v <= pts[-1][0] else None


def build_focus(roots, pick=None, status=None, frames=()):
    _, results = scan(roots)
    runs = [{"path": p, "name": os.path.relpath(p, os.path.dirname(os.path.dirname(os.path.dirname(p)))),
             "mtime": datetime.datetime.fromtimestamp(m).isoformat(timespec="seconds")}
            for m, p in results[:20]]
    chosen = pick if pick and any(r["path"] == pick for r in runs) else (runs[0]["path"] if runs else None)
    live = None
    seq = (status or {}).get("sequence") or {}
    prog = seq.get("program") or {}
    if prog.get("focus_bracket"):
        here = next((f["path"] for f in frames if f.get("seq") == seq.get("id") and f.get("path")), None)
        live = {"id": seq.get("id"), "state": seq.get("state"), "index": seq.get("index"),
                "total": seq.get("total"), "bracket": prog["focus_bracket"],
                "per_value": prog.get("per_value"), "dir": os.path.dirname(here) if here else None}
    return {"runs": runs, "current": read_focus(chosen), "live_bracket": live}


# ---------------------------------------------------------------------------

def battery_trend(events, status):
    """Battery series from the heartbeats, and the time left at the recent drain rate."""
    pts = []
    for ev in events:
        if ev.get("type") == "heartbeat" and isinstance(ev.get("battery"), (int, float)) and ev["battery"] >= 0:
            t = _iso_epoch(ev.get("t"))
            if t:
                pts.append((t, ev["battery"] * 100))
    now = time.time()
    pts = [p for p in pts if now - p[0] < 3 * 3600]
    step = max(1, len(pts) // 180)
    series = pts[::step]
    charging = ((status or {}).get("power") or {}).get("charging")
    rate = eta = None
    recent = [p for p in pts if now - p[0] < 3600]
    if not charging and len(recent) >= 10 and recent[-1][0] - recent[0][0] >= 900:
        n = len(recent)
        mt = sum(p[0] for p in recent) / n
        mb = sum(p[1] for p in recent) / n
        var = sum((p[0] - mt) ** 2 for p in recent)
        if var > 0:
            slope = sum((p[0] - mt) * (p[1] - mb) for p in recent) / var * 3600   # % per hour
            rate = round(slope, 2)
            if slope < -0.2:
                eta = round(recent[-1][1] / -slope, 1)
    return {"series": [[round(t), round(b, 1)] for t, b in series], "rate_pct_h": rate, "eta_h": eta}


KINDS = ["frame", "sequence", "note", "error", "log", "heartbeat", "dashboard"]


def kind_of(ev):
    t = ev.get("type", "other")
    return "sequence" if t.startswith("sequence") else t if t in KINDS else "log"


def build_state(w, roots, pick=None, ql=False):
    with w.lock:
        events = list(w.events)
        status = w.status
        link = {
            "status_ok": w.status_err is None and w.status is not None,
            "status_age_s": _age(w.status_at), "status_err": w.status_err,
            "status_fails": w.status_fails,
            "sse_connected": w.sse_connected, "sse_err": w.sse_err,
            "sse_age_s": _age(w.sse_since), "reconnects": w.reconnects,
            "event_age_s": _age(w.event_at), "heartbeat_age_s": _age(w.heartbeat_at),
            "last_id": w.last_id,
        }
        heartbeat = w.heartbeat
    by = {k: [] for k in KINDS}
    commands = []
    for ev in events:
        # The phone logs every request it serves; those go to the commands pane only, so the
        # log tab keeps the phone's own messages ("camera ready", "went to the background").
        m = HTTP_LINE.match(str(ev.get("message", ""))) if ev.get("type") == "log" else None
        if m:
            commands.append({"t": ev.get("t"), "id": ev.get("id"), "method": m[1], "path": m[2]})
        else:
            by[kind_of(ev)].append(ev)
    frames = build_frames(events, roots)
    return {
        "now": datetime.datetime.now().isoformat(timespec="seconds"),
        "phone": w.ph.base,
        "link": link,
        "status": status,
        "heartbeat": heartbeat,
        "battery": battery_trend(events, status),
        "events": {k: v[-MAX_PER_KIND:] for k, v in by.items() if v},
        "event_counts": {k: len(v) for k, v in by.items() if v} | {"requests": len(commands)},
        "kinds": KINDS,
        "commands": commands[-MAX_COMMANDS:],
        "frames": frames,
        "focus": build_focus(roots, pick, status, frames),
        "preview": w.preview_info(),
        "quicklook": ql,
    }


# ---------------------------------------------------------------------------
# Quick-look JPEGs from the DNGs on the Mac
# ---------------------------------------------------------------------------

class QuickLook:
    """Renders through quicklook.py, in-process if rawpy imports, else under ~/ASICAP/.venv.

    One render at a time: a page of new thumbnails must not take every core away from the
    focus scripts running on the same Mac.
    """

    def __init__(self, cache_dir):
        self.cache = cache_dir
        self.lock = threading.Lock()
        self.render = None
        self.python = None
        self.rendered = 0              # since the last terminal heartbeat
        try:
            sys.path.insert(0, HERE)
            from quicklook import render
            self.render = render
        except ImportError:
            if os.path.exists(VENV_PY):
                self.python = VENV_PY

    @property
    def available(self):
        return bool(self.render or self.python)

    def jpeg(self, dng, width):
        st = os.stat(dng)
        key = hashlib.sha1(f"{dng}|{st.st_mtime_ns}|{st.st_size}|{width}".encode()).hexdigest()[:20]
        out = os.path.join(self.cache, f"{os.path.basename(dng)[:-4]}_{width}_{key}.jpg")
        if os.path.exists(out):
            return out
        os.makedirs(self.cache, exist_ok=True)
        with self.lock:
            if os.path.exists(out):              # rendered while we waited for the lock
                return out
            t0 = time.time()
            part = out + ".part"
            if self.render:
                import cv2
                ok, buf = cv2.imencode(".jpg", self.render(dng, width), [cv2.IMWRITE_JPEG_QUALITY, 85])
                if not ok:
                    raise IOError("jpeg encode failed")
                with open(part, "wb") as f:
                    f.write(buf.tobytes())
            else:
                p = subprocess.run([self.python, "-I", os.path.join(HERE, "quicklook.py"), "--dng", dng,
                                    "--out", part, "--width", str(width)],
                                   capture_output=True, text=True, timeout=90)
                if p.returncode != 0:
                    raise IOError((p.stderr or p.stdout).strip().splitlines()[-1:] or ["quicklook failed"])
            os.replace(part, out)
            self.rendered += 1
            if time.time() - t0 > 5:
                log(f"quick-look {os.path.basename(dng)} @ {width}px took {time.time() - t0:.1f}s")
            return out


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

def _inside(path, roots):
    real = os.path.realpath(path)
    return real if any(real.startswith(r + os.sep) for r in roots) else None


class Handler(http.server.BaseHTTPRequestHandler):
    watch = None
    roots = ()
    ql = None
    preview_width = 900
    server_version = "skycam-dashboard/1"

    def log_message(self, fmt, *a):
        pass                  # the terminal is for the phone's story, not for page polls

    def _send(self, code, body, ctype="application/json", cache="no-store", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass              # the page navigated away mid-response; not a fault

    def _err(self, code, msg):
        return self._send(code, json.dumps({"error": msg}))

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            f = os.path.join(HERE, "dashboard.html")
            if not os.path.exists(f):
                return self._send(500, "dashboard.html missing next to dashboard.py", "text/plain")
            with open(f, "rb") as fh:
                return self._send(200, fh.read(), "text/html; charset=utf-8")
        if u.path == "/api/state":
            self.watch.touch_browser(self.client_address[0])
            try:
                s = build_state(self.watch, self.roots, q.get("focus", [None])[0], self.ql.available)
                return self._send(200, json.dumps(s, default=str))
            except Exception as e:
                log(f"state build failed: {type(e).__name__}: {e}")
                return self._err(500, str(e))
        if u.path == "/preview.jpg":
            jpg, at, err = self.watch.preview(self.preview_width)
            if not jpg:
                return self._err(503, err or "no preview yet")
            return self._send(200, jpg, "image/jpeg", extra={
                "X-Preview-Age": f"{time.time() - at:.1f}", "X-Preview-Error": (err or "")[:200]})
        if u.path == "/ql":
            return self._quicklook(q.get("p", [""])[0], q.get("w", ["480"])[0])
        if u.path == "/img":
            return self._image(q.get("p", [""])[0])
        return self._err(404, "not found")

    def _quicklook(self, p, w):
        """Only DNGs inside --frames: the path comes from the page, so it is not trusted."""
        real = _inside(p, self.roots) if p else None
        if not real or not real.endswith(".dng") or not os.path.isfile(real):
            return self._err(403 if p and not real else 404, "not a DNG under --frames")
        if not self.ql.available:
            return self._err(501, "no rawpy here and no ~/ASICAP/.venv")
        try:
            width = max(64, min(2400, int(w)))
            out = self.ql.jpeg(real, width)
        except Exception as e:
            log(f"quick-look failed for {os.path.basename(real)}: {e}")
            return self._err(500, str(e))
        with open(out, "rb") as f:
            return self._send(200, f.read(), "image/jpeg", cache="private, max-age=86400")

    def _image(self, p):
        real = _inside(p, self.roots) if p else None
        if not real or not os.path.isfile(real):
            return self._err(403 if p and not real else 404, "not an image under --frames")
        ctype = mimetypes.guess_type(real)[0] or ""
        if not ctype.startswith("image/"):
            return self._err(415, "not an image")
        with open(real, "rb") as f:
            return self._send(200, f.read(), ctype)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def heartbeat_loop(w, ql, every):
    """One line every `every` seconds, so a quiet terminal still says the dashboard is alive."""
    while True:
        time.sleep(every)
        with w.lock:
            s, hb = w.status or {}, w.heartbeat or {}
            ok, fails, err = w.status_err is None and w.status, w.status_fails, w.status_err
            sse, ev_age, last_id = w.sse_connected, _age(w.event_at), w.last_id
        pw, cam, seq = s.get("power") or {}, s.get("camera") or {}, s.get("sequence") or {}
        if ok:
            bat = pw.get("battery", hb.get("battery", -1))
            lp = cam.get("lens_position")
            line = (f"♥ phone ok  battery {bat * 100:.0f}%{' (charging)' if pw.get('charging') else ''}  "
                    f"thermal {pw.get('thermal')}  free {(s.get('storage') or {}).get('free_bytes', 0) / 1e9:.1f} GB  "
                    f"focus {lp if lp is None else round(lp, 3)}  seq {seq.get('state', 'idle')} "
                    f"{seq.get('index', 0)}/{seq.get('total', 0)}")
        else:
            line = f"♥ phone NOT answering ({fails} tries: {err})"
        line += (f"  |  events {'live' if sse else 'DOWN'}, last #{last_id}"
                 f"{'' if ev_age is None else f' {ev_age:.0f}s ago'}  browsers {w.browsers_now()}")
        if ql.rendered:
            line += f"  quick-looks +{ql.rendered}"
            ql.rendered = 0
        log(line)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0],
                                 epilog="Read-only: sends GET requests only.")
    ap.add_argument("--port", type=int, default=8768)
    ap.add_argument("--bind", default="127.0.0.1", help="0.0.0.0 to reach it from the phone on the balcony")
    ap.add_argument("--host", help="phone address (default: ~/.config/skycam.json)")
    ap.add_argument("--phone-port", type=int, help="phone port (default: ~/.config/skycam.json, 8070)")
    ap.add_argument("--token", help="phone token (default: ~/.config/skycam.json)")
    ap.add_argument("--frames", action="append", default=None,
                    help="directory to look for downloaded DNGs and focus results in (repeatable; "
                         "default: the repo)")
    ap.add_argument("--data", default=DATA, help="where the event log is appended")
    ap.add_argument("--cache", default=CACHE, help="where quick-look JPEGs are kept")
    ap.add_argument("--status-every", type=float, default=5, help="seconds between GET /status")
    ap.add_argument("--preview-width", type=int, default=900)
    ap.add_argument("--heartbeat-every", type=float, default=HEARTBEAT_EVERY,
                    help="seconds between terminal heartbeat lines")
    ap.add_argument("--once", action="store_true", help="dump one state as JSON and exit")
    a = ap.parse_args()

    cfg = skycam.load_cfg()
    ph = ReadOnlyPhone(a.host or cfg.get("host"), a.phone_port or cfg.get("port", 8070),
                       a.token or cfg.get("token"))
    roots = tuple(os.path.realpath(os.path.expanduser(r)) for r in (a.frames or [REPO]))
    w = Watch(ph, a.data, a.status_every)
    ql = QuickLook(a.cache)

    if a.once:
        w.poll_status_once()
        try:
            w.backfill()
        except Exception as e:
            w.note(f"backfill failed: {e}")
        print(json.dumps(build_state(w, roots, None, ql.available), indent=1, default=str))
        return 0

    log(f"phone {ph.base} (GET only)  frames under {', '.join(roots)}  events -> {a.data}")
    log(f"quick-look: {'in-process' if ql.render else ql.python or 'unavailable (no rawpy, no venv)'}")
    w.start()
    threading.Thread(target=heartbeat_loop, args=(w, ql, a.heartbeat_every), daemon=True).start()
    Handler.watch, Handler.roots, Handler.ql, Handler.preview_width = w, roots, ql, a.preview_width
    with Server((a.bind, a.port), Handler) as srv:
        log(f"dashboard on http://{'localhost' if a.bind in ('127.0.0.1', '0.0.0.0') else a.bind}:{a.port}")
        if a.bind == "127.0.0.1":
            log("--bind 0.0.0.0 to open it to the phone on the LAN")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
