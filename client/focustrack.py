#!/usr/bin/env python3
"""Focus V-curve from a SkyCam bracket, measuring the SAME stars in every frame.

    ~/ASICAP/.venv/bin/python3 -u client/focustrack.py --dir test/sky/coarse [--ref-lens 0.90]

Stars are picked in the frame nearest --ref-lens (default: middle of the bracket) as the brightest
isolated, unsaturated sources wider than a hot pixel; each frame is aligned to it by phase
correlation (the phone may have been nudged), then every star's half-flux radius is measured at
its re-centred position. The median HFR per lens position is the V-curve; smaller is sharper.
"""
import argparse, glob, json, os
import numpy as np, cv2
from focusraw import green_half

ap = argparse.ArgumentParser()
ap.add_argument("--dir", required=True)
ap.add_argument("--ref-lens", type=float)
ap.add_argument("--stars", type=int, default=25)
a = ap.parse_args()

files = sorted(glob.glob(os.path.join(a.dir, "*.dng")))
lens = [json.load(open(f[:-4] + ".json"))["lens_position"] for f in files]
imgs = []
for f in files:
    g, sat = green_half(f)
    bg = cv2.GaussianBlur(cv2.resize(cv2.resize(g, None, fx=1/16, fy=1/16, interpolation=cv2.INTER_AREA), (g.shape[1], g.shape[0])), (0, 0), 8)
    imgs.append((g - bg, sat))
ref_lens = a.ref_lens if a.ref_lens is not None else float(np.median(lens))
ri = int(np.argmin([abs(l - ref_lens) for l in lens]))
R = np.mean([im for im, _ in imgs], axis=0)
sat = imgs[ri][1]
noise = 1.4826 * np.median(np.abs(R - np.median(R)))
sm = cv2.GaussianBlur(R, (0, 0), 1.5)
mx = cv2.dilate(sm, np.ones((31, 31), np.uint8))
H, W = R.shape
yy, xx = np.mgrid[-14:15, -14:15]; rr = np.hypot(yy, xx)

def hfr(img, y, x):
    """Flux-weighted RMS radius about the centroid (continuous, unlike a half-flux radius on
    integer pixel radii) and the fraction of the star's flux in its brightest pixel."""
    s = img[y - 14:y + 15, x - 14:x + 15]
    s = s - np.median(s[rr > 11])
    w = np.clip(s, 0, None) * (rr <= 7)
    tot = w.sum()
    if tot <= 0: return np.nan, 0
    cy = (w * yy).sum() / tot; cx = (w * xx).sum() / tot
    rms = np.sqrt((w * ((yy - cy) ** 2 + (xx - cx) ** 2)).sum() / tot)
    return rms, w.max() / tot

cands = []
for y, x in np.argwhere((sm == mx) & (sm > 5 * noise)):
    if y < 40 or x < 40 or y > H - 41 or x > W - 41: continue
    if max(im[y - 3:y + 4, x - 3:x + 4].max() for im, _ in imgs) >= 0.85 * sat: continue
    h, peakfrac = hfr(R, y, x)
    if not np.isfinite(h) or peakfrac > 0.5: continue          # hot pixel: all its light in one pixel
    cands.append((R[y - 2:y + 3, x - 2:x + 3].sum(), y, x))
cands.sort(reverse=True)
stars = [(y, x) for _, y, x in cands[:a.stars]]
print(f"{len(stars)} stars picked in the mean of all {len(files)} frames", flush=True)

def shift(A, B):
    f = lambda z: np.clip(cv2.GaussianBlur(z, (0, 0), 2), 0, None).astype(np.float32)
    (dx, dy), _ = cv2.phaseCorrelate(f(A), f(B))
    return dx, dy

rows = []
for f, l, (img, _) in zip(files, lens, imgs):
    dx, dy = shift(R, img)
    hs = []
    for y, x in stars:
        yc, xc = int(round(y + dy)), int(round(x + dx))
        if yc < 20 or xc < 20 or yc > H - 21 or xc > W - 21: continue
        win = cv2.GaussianBlur(img, (0, 0), 1.5)[yc - 6:yc + 7, xc - 6:xc + 7]
        py, px = np.unravel_index(np.argmax(win), win.shape)
        h, pf = hfr(img, yc - 6 + py, xc - 6 + px)
        if np.isfinite(h): hs.append((h, pf))
    hs = np.array(hs)
    rows.append((l, float(np.median(hs[:, 0])), float(np.median(hs[:, 1]))))
    print(f"lens {l:.4f}  RMS radius {np.median(hs[:, 0]):.3f} px  peak share {np.median(hs[:, 1]):.3f}  "
          f"({len(hs)} stars, shift {dx:+.1f},{dy:+.1f})", flush=True)

print("\nV-curve (median per lens position; RMS radius in 2x2-binned px, smaller = sharper):", flush=True)
pts = []
for l in sorted(set(r[0] for r in rows)):
    v = float(np.median([r[1] for r in rows if r[0] == l]))
    pf = float(np.median([r[2] for r in rows if r[0] == l]))
    pts.append((l, v))
    print(f"  {l:.4f}  RMS {v:.3f}  peak {pf:.3f}  " + "#" * int(v * 15), flush=True)
L, V = np.array(pts).T
i = int(np.argmin(V))
lo, hi = max(0, i - 3), min(len(L), i + 4)
c = np.polyfit(L[lo:hi], V[lo:hi], 2)
vertex = -c[1] / (2 * c[0]) if c[0] > 0 else L[i]
print(f"\nsmallest measured: lens {L[i]:.4f} (RMS {V[i]:.3f});  parabola through the 7 points around it: "
      f"best lens {vertex:.4f} = {vertex * 255:.1f}/255", flush=True)
