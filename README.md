# SkyCam

iPhone camera app for the night sky, driven from the Mac over Wi-Fi. It replaces NightCap, whose
"infinity" focus was wrong on the iPhone Air (FOC 100 gave ~25 px star discs; FOC 92 was sharp)
and which recorded neither the focus position nor anything better than 8-bit TIFFs.

- **Bayer RAW (DNG)** from the main wide camera, plus optional JPEG/HEIC.
- **Manual control**: lens position (0 = closest .. 1 = furthest), exposure time, ISO, EV bias, WB.
- **Sequences** run on the phone, so they survive the Mac or Wi-Fi dropping out: N frames, focus
  brackets, explicit per-frame lists, intervals.
- **Every frame writes `<base>.json`** next to its DNG with the lens position, exposure, ISO and WB.
- **HTTP on port 8070**, advertised over Bonjour as `_skycam._tcp`. The Mac downloads each file as
  soon as it is written: no iCloud lag (NightCap frames took 4–5 min to arrive).
- Live progress over Server-Sent Events, with a heartbeat every 5 s (battery, thermal, focus, sequence).

## Install / refresh (every 7 days with free signing)

    ./install.sh                 # xcodegen + xcodebuild + devicectl install + launch

The first time, you need to do four things on the phone itself:
1. Settings → Privacy & Security → **Developer Mode** → On (the phone restarts).
2. Keep the phone **unlocked** while `install.sh` runs.
3. If iOS says "Untrusted Developer": Settings → General → VPN & Device Management → trust
   *Apple Development: mads@dorup.dk*.
4. When SkyCam first opens, **Allow** camera and **Allow** local network.

## Mac client

    python3 -u client/skycam.py pair            # reads token + IP from the app via devicectl
    python3 -u client/skycam.py status
    python3 -u client/skycam.py set --focus 0.92 --exp 1 --iso 3200
    python3 -u client/skycam.py af              # autofocus once; prints the lens position it chose
    python3 -u client/skycam.py bracket --focus-from 0.86 --focus-to 0.98 --focus-step 0.01 --exp 1 --iso 3200 --out focus/
    python3 -u client/skycam.py shoot --count 60 --exp 1 --iso 3200 --focus 0.92 --out frames/ --delete
    python3 -u client/skycam.py preview --out preview.jpg
    python3 -u client/skycam.py events

## Dashboard

A page to follow a session live, from the Mac or from a phone on the balcony:

    python3 -u dashboard/dashboard.py --port 8768                    # http://localhost:8768
    python3 -u dashboard/dashboard.py --port 8768 --bind 0.0.0.0     # also reachable from other devices on the LAN

It shows the phone (battery and time left, thermal, free space, lens position, exposure, ISO, WB,
`stale_discards`, sequence progress), the live view, the newest `focustrack.py` / `focusraw.py`
V-curve under `--frames` (default: the repo; looks for `track.txt` / `focus.txt` written with
`| tee`), the newest frames with a quick-look rendered from each DNG already on the Mac, every
request the phone logged, and every event split by type. A heartbeat line goes to the terminal
every 15 s.

It is **read-only**: it sends GET requests only (`/status`, `/events`, `/events/poll`,
`/preview.jpg`), so it can run beside a sequence another client is driving. It does not poll
`/files`, because the phone logs every such request as an event. Connection details come from
`~/.config/skycam.json`; the token stays on the Mac. Events are also appended to
`dashboard/data/events-YYYY-MM-DD.jsonl`. Quick-looks need rawpy: when the interpreter running the
dashboard lacks it, they are rendered with `~/ASICAP/.venv/bin/python3`.

## HTTP API

All routes except `GET /ping` need `Authorization: Bearer <token>`. The token is on the phone screen
and in the app's `Documents/connection.json`.

| Route | |
|---|---|
| `GET /status` | camera (lens position, exposure, ISO, WB, limits), sequence, battery, thermal, storage |
| `POST /settings` | `{"focus": 0.92 \| "auto" \| "lock", "exposure": 1.0 \| "auto", "iso": 3200, "bias": 0, "wb": "auto" \| "lock" \| {"temp": 4000, "tint": 0} \| {"gains": [r,g,b]}}` |
| `POST /af` | `{"x":0.5,"y":0.5,"lock":true}` → `{"lens_position": …}` |
| `POST /capture` | one frame; optional settings plus `raw`, `jpeg`, `heic`, `tag`; returns when the files are written |
| `POST /sequence` | program: `count`, `frames`, `focus_bracket`, `per_value`, `repeat`, `interval`, `settle`, settings |
| `GET /sequence`, `POST /sequence/stop` | progress / stop |
| `GET /files[?after=name]`, `GET /files/<name>`, `DELETE /files/<name>`, `POST /files/delete` | storage |
| `GET /preview.jpg?w=1280` | latest live-view frame |
| `GET /events` (SSE), `GET /events/poll?since=<id>` | frame, sequence_started/done, note, error, log, heartbeat |
| `GET /log?n=200` | app log |

Values outside the camera's limits are clamped, never passed through: an out-of-range value makes
AVFoundation crash the app. `GET /status` → `camera.limits` reports the real ranges, including the
longest single exposure. For longer integrations, shoot a sequence and stack the frames.

## How the camera behaves (measured 2026-10-19, iPhone Air, iOS 26.6.1)

- **Longest single exposure 1 s**, ISO 33–7392 (the same 7392 cap NightCap had). No ProRAW on the Air.
- **DNG**: 4224×3024 Bayer BGGR, 12-bit (black 528, white 4095), lossless JPEG, ~9.8 MB. Wi-Fi
  downloads run at 23–31 MB/s, so they keep up with 1 s frames (~10 MB/s needed).
- **Lens position moves in steps of 1/255** (0.90 → 0.902). The sidecar records the real value.
- **The photo output returns the newest frame of the running stream**, even with zero shutter lag
  off. Asked twice within one exposure, it returns the same frame twice; asked right after a
  settings change, it can return a frame exposed before the change. SkyCam therefore waits until
  the live-view stream has a frame that is both new and timestamped after the last focus or
  exposure change, and it drops any duplicate that still gets through (`stale_discards` in
  `/status`). Result: back-to-back frames exactly 1.000 s apart, i.e. the sensor is exposing
  100% of the time. The first frame after a focus change comes ~2.5 s later.
- Each sidecar has `frame_ts` (sensor timestamp, host clock), `request_ts` and `valid_after_ts`,
  so frame timing can be audited afterwards.

The app has to stay in the foreground (iOS stops the camera in the background). Use **Dim screen**
at night.
