#!/usr/bin/env python3
"""Star size per frame from SkyCam DNGs, grouped by lens position: finds best focus in a bracket.

    ~/ASICAP/.venv/bin/python3 -u client/focusraw.py --dir test/bracket1
    ~/ASICAP/.venv/bin/python3 -u client/focusraw.py --dir test/bracket1 --montage test/bracket1/stars.jpg

Works on the raw Bayer data, not a demosaiced image: the two greens of each 2x2 cell are summed
into a half-resolution green image (2112x1512 for the iPhone Air), so pixels below are 2x2 sensor
pixels. For each frame, the brightest isolated unsaturated stars are measured (radial-profile FWHM
and half-flux radius); the median of the top --top stars is the score. Smaller is sharper.
The lens position comes from each frame's JSON sidecar.
"""
import argparse, glob, json, os, sys
import numpy as np, cv2, rawpy
from scipy import ndimage


def green_half(path):
    with rawpy.imread(path) as r:
        raw = r.raw_image_visible.astype(np.float32)
        black = float(np.mean(r.black_level_per_channel))
        white = float(r.white_level)
        pat = r.raw_pattern            # 2x2 colour indices; 1 and 3 are the greens
        cdesc = r.color_desc.decode()
    h, w = raw.shape[0] // 2 * 2, raw.shape[1] // 2 * 2
    raw = raw[:h, :w] - black
    g = np.zeros((h // 2, w // 2), np.float32)
    for dy in (0, 1):
        for dx in (0, 1):
            if cdesc[pat[dy, dx]] == "G":
                g += raw[dy::2, dx::2]
    return g, 2 * (white - black)


def measure(g, sat, top):
    bg = cv2.resize(ndimage.median_filter(g[::8, ::8], size=15), (g.shape[1], g.shape[0]), interpolation=cv2.INTER_LINEAR)
    r = g - bg
    noise = 1.4826 * np.median(np.abs(r - np.median(r))) + 1e-6
    sm = ndimage.gaussian_filter(r, 1.0)
    mx = ndimage.maximum_filter(sm, 25)
    pk = np.argwhere((sm == mx) & (sm > 10 * noise))
    H, W = g.shape
    R = 20
    yy, xx = np.mgrid[-R:R + 1, -R:R + 1]
    rr = np.hypot(yy, xx)
    out = []
    for y, x in pk:
        if y < R + 2 or x < R + 2 or y > H - R - 3 or x > W - R - 3:
            continue
        if g[y - 2:y + 3, x - 2:x + 3].max() >= 0.9 * sat:
            continue
        st = r[y - R:y + R + 1, x - R:x + R + 1]
        ann = st[(rr > 14) & (rr <= R)]
        if np.std(ann) > 3 * noise:
            continue                                   # not isolated
        st = st - np.median(ann)
        prof = np.array([st[(rr >= k - 0.5) & (rr < k + 0.5)].mean() for k in range(15)])
        peak = prof[0]
        if peak <= 0:
            continue
        below = np.where(prof < peak / 2)[0]
        if len(below) == 0 or below[0] < 1:
            continue
        k = below[0]
        fwhm = 2 * (k - 1 + (prof[k - 1] - peak / 2) / (prof[k - 1] - prof[k] + 1e-9))
        w_ = np.clip(st, 0, None) * (rr <= 12)
        tot = w_.sum()
        if tot <= 0:
            continue
        order = np.argsort(rr.ravel())
        cum = np.cumsum(w_.ravel()[order])
        hfr = rr.ravel()[order][np.searchsorted(cum, tot / 2)]
        out.append((tot, fwhm, hfr, peak, y, x))
    out.sort(reverse=True)
    sel = out[:top]
    if not sel:
        return None
    a = np.array([s[:4] for s in sel])
    return {"n": len(out), "fwhm": float(np.median(a[:, 1])), "hfr": float(np.median(a[:, 2])),
            "peak": float(np.median(a[:, 3])), "noise": float(noise), "stars": sel}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", required=True, help="folder of SkyCam .dng + .json files")
    ap.add_argument("--top", type=int, default=30, help="score from this many brightest stars")
    ap.add_argument("--montage", help="write a JPEG of the brightest stars per frame")
    a = ap.parse_args()

    files = sorted(glob.glob(os.path.join(a.dir, "*.dng")))
    if not files:
        sys.exit(f"no .dng in {a.dir}")
    rows, tiles = [], []
    for f in files:
        meta = {}
        js = f[:-4] + ".json"
        if os.path.exists(js):
            meta = json.load(open(js))
        g, sat = green_half(f)
        m = measure(g, sat, a.top)
        lp = meta.get("lens_position")
        name = os.path.basename(f)
        if m is None:
            print(f"{name}: no stars found", flush=True)
            continue
        rows.append((lp, m["fwhm"], m["hfr"], m["n"]))
        print(f"{name[:40]}  lens {lp:.4f}  stars {m['n']:4d}  FWHM {m['fwhm']:.2f} px  HFR {m['hfr']:.2f} px  "
              f"peak {m['peak']:.0f}  noise {m['noise']:.1f}", flush=True)
        if a.montage:
            row = []
            for tot, fw, hf, pk, y, x in m["stars"][:10]:
                t = g[y - 12:y + 13, x - 12:x + 13]
                t = np.clip((t - np.median(t)) / max(pk, 1), 0, 1)
                row.append(cv2.resize((t * 255).astype(np.uint8), (100, 100), interpolation=cv2.INTER_NEAREST))
            while len(row) < 10:
                row.append(np.zeros((100, 100), np.uint8))
            lab = np.full((100, 150), 30, np.uint8)
            cv2.putText(lab, f"{lp:.3f}", (8, 58), cv2.FONT_HERSHEY_SIMPLEX, 0.8, 255, 2)
            tiles.append(np.hstack([lab] + row))

    print("\nby lens position (median of frames):", flush=True)
    best = None
    for lp in sorted({r[0] for r in rows}):
        sel = [r for r in rows if r[0] == lp]
        fw = float(np.median([r[1] for r in sel])); hf = float(np.median([r[2] for r in sel]))
        n = int(np.median([r[3] for r in sel]))
        print(f"  {lp:.4f}  FWHM {fw:.2f}  HFR {hf:.2f}  stars {n}  ({len(sel)} frames)", flush=True)
        if best is None or hf < best[1]:
            best = (lp, hf)
    if best:
        print(f"\nsharpest: lens position {best[0]:.4f} (HFR {best[1]:.2f} px)", flush=True)
    if a.montage and tiles:
        cv2.imwrite(a.montage, np.vstack(tiles))
        print(f"montage: {a.montage}", flush=True)


if __name__ == "__main__":
    main()
