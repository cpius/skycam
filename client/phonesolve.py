#!/usr/bin/env python3
"""Blind plate solve of a SkyCam wide-field stack, then: where is the camera pointing, and how far
and which way to Andromeda (or any --target).

    ~/ASICAP/.venv/bin/python3 -u client/phonesolve.py --stack test/sky/solve/stack_linear.npy \
        --meta test/sky/solve --out test/sky/solve/solved

Input is dngstack.py's half-size linear stack (sensor orientation). The scale is known (the main
camera's 68.16 deg horizontal field), so pairs of the brightest detections are matched to Yale BSC
pairs of the same angular separation; each candidate rotation is scored by how many detections
land on catalogue stars, and the winner is refined (rotation + focal length + radial distortion).
Pointing is converted to altitude/azimuth for the frames' capture time at Copenhagen.
"""
import argparse, csv, glob, json, math, os, datetime as dt
import numpy as np, cv2
from scipy import ndimage, optimize
from scipy.spatial import cKDTree

ap = argparse.ArgumentParser()
ap.add_argument("--stack", required=True)
ap.add_argument("--meta", required=True, help="folder with the frames' JSON sidecars (capture time)")
ap.add_argument("--out", required=True)
ap.add_argument("--catalog", default=os.path.join(os.path.dirname(__file__), "..", "catalog", "bsc_v6.csv"))
ap.add_argument("--hfov", type=float, default=68.157, help="horizontal field of view, deg")
ap.add_argument("--lat", type=float, default=55.6896); ap.add_argument("--lon", type=float, default=12.5551)
ap.add_argument("--no-mask", action="store_true", help="treat the whole frame as sky")
ap.add_argument("--top", type=int, default=8, help="brightest detections tried as pair seeds")
ap.add_argument("--target", default="M31"); ap.add_argument("--target-ra", type=float, default=10.6847)
ap.add_argument("--target-dec", type=float, default=41.2690)
a = ap.parse_args()

img = np.load(a.stack); g = img[..., 1].astype(np.float64); H, W = g.shape
f0 = (W / 2) / math.tan(math.radians(a.hfov / 2)); cx0, cy0 = (W - 1) / 2, (H - 1) / 2

# --- detections ---------------------------------------------------------------------------
bg = cv2.resize(ndimage.median_filter(cv2.resize(g, (W // 8, H // 8), interpolation=cv2.INTER_AREA), size=9), (W, H))
s = g - bg
dog = ndimage.gaussian_filter(s, 1.0) - ndimage.gaussian_filter(s, 4)
sig = 1.4826 * np.median(np.abs(dog - np.median(dog)))
# Sky mask: walls, roofs and ledges are bright and textured; sky is dark and smooth.
# Tile medians split into two populations (Otsu on log level); grow the foreground by 40 px.
tile = 32
small = cv2.resize(g, (W // tile, H // tile), interpolation=cv2.INTER_AREA)
lv = np.log(np.clip(small - np.percentile(small, 1) + 1e-6, 1e-6, None))
u8 = cv2.normalize(lv, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
thr, _ = cv2.threshold(u8, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
fg_small = u8 > thr
if a.no_mask or fg_small.mean() > 0.6 or np.percentile(lv, 90) - np.percentile(lv, 10) < 1.0:
    fg_small[:] = False                                         # no clear foreground: all sky
fg = cv2.resize(fg_small.astype(np.uint8), (W, H), interpolation=cv2.INTER_NEAREST)
fg = cv2.dilate(fg, np.ones((81, 81), np.uint8)).astype(bool)
print(f"sky mask: {100 * (1 - fg.mean()):.0f}% of the frame is sky", flush=True)
cv2.imwrite(a.out + "_skymask.png", (~fg).astype(np.uint8) * 255)
pk = np.argwhere((dog == ndimage.maximum_filter(dog, 11)) & (dog > 7 * sig) & ~fg)
det = []
for y, x in pk:
    if not (6 <= y < H - 6 and 6 <= x < W - 6): continue
    w = np.clip(s[y - 4:y + 5, x - 4:x + 5], 0, None); t = w.sum()
    if t <= 0 or w.max() / t > 0.6: continue                     # hot pixel
    ring = s[y - 6:y + 7, x - 6:x + 7].copy(); ring[2:11, 2:11] = np.nan
    if np.nanstd(ring) > 3 * sig: continue                        # on an edge / foreground texture
    yy, xx = np.mgrid[-4:5, -4:5]
    mx_, my_ = (w * xx).sum() / t, (w * yy).sum() / t
    cxx = (w * (xx - mx_) ** 2).sum() / t; cyy = (w * (yy - my_) ** 2).sum() / t; cxy = (w * (xx - mx_) * (yy - my_)).sum() / t
    l1, l2 = np.linalg.eigvalsh([[cxx, cxy], [cxy, cyy]])[::-1]
    if l2 <= 0 or l1 / l2 > 2.5: continue                         # elongated: an edge, not a star
    det.append((t, x + mx_, y + my_))
det.sort(reverse=True); det = np.array(det)
print(f"{len(det)} detections", flush=True)

def cam_vec(xy, f, cx, cy, k1=0.0):
    x = (xy[:, 0] - cx) / f; y = (xy[:, 1] - cy) / f
    r2 = x * x + y * y; d = 1 + k1 * r2                           # undo radial distortion (first order)
    v = np.stack([x / d, y / d, np.ones_like(x)], 1)
    return v / np.linalg.norm(v, axis=1, keepdims=True)

# --- catalogue ------------------------------------------------------------------------------
cat = []
for r in csv.DictReader(open(a.catalog)):
    try: cat.append((float(r["Vmag"]), float(r["RAJ2000"]), float(r["DEJ2000"]), r["Name"][3:].strip(), r["HR"]))   # drop the Flamsteed number
    except ValueError: pass
V = np.array([c[0] for c in cat]); RA = np.radians([c[1] for c in cat]); DE = np.radians([c[2] for c in cat])
CV = np.stack([np.cos(DE) * np.cos(RA), np.cos(DE) * np.sin(RA), np.sin(DE)], 1)
bright = np.where(V < 4.2)[0]; faint_tree = cKDTree(CV[V < 5.5]); faint_idx = np.where(V < 5.5)[0]

# --- pair matching --------------------------------------------------------------------------
D = cam_vec(det[:, 1:3], f0, cx0, cy0)
top = D[:a.top]
B = CV[bright]
pa = np.arccos(np.clip(B @ B.T, -1, 1))
ii, jj = np.where(np.triu(np.ones_like(pa, bool), 1))
pairs = np.stack([ii, jj, pa[ii, jj]], 1); pairs = pairs[np.argsort(pairs[:, 2])]

def triad(a1, a2, b1, b2):
    """Rotation taking camera vectors (a1,a2) onto sky vectors (b1,b2)."""
    def frame(u, v):
        t1 = u; t2 = np.cross(u, v); t2 /= np.linalg.norm(t2); return np.stack([t1, t2, np.cross(t1, t2)], 1)
    return frame(b1, b2) @ frame(a1, a2).T

def score(R, n=60, tol=math.radians(0.35)):
    sky = (R @ D[:n].T).T
    d, _ = faint_tree.query(sky, distance_upper_bound=tol)
    return int(np.isfinite(d).sum())

best = (0, None)
for i in range(len(top)):
    for j in range(i + 1, len(top)):
        th = math.acos(np.clip(top[i] @ top[j], -1, 1))
        lo, hi = np.searchsorted(pairs[:, 2], [th - math.radians(0.5), th + math.radians(0.5)])
        for p, q, _ in pairs[lo:hi]:
            for b1, b2 in ((B[int(p)], B[int(q)]), (B[int(q)], B[int(p)])):
                R = triad(top[i], top[j], b1, b2)
                if not np.all(np.isfinite(R)): continue          # coincident detections
                sc = score(R)
                if sc > best[0]: best = (sc, R)
print(f"best rough pose: {best[0]} of 60 detections on catalogue stars", flush=True)
R = best[1]

# --- refine: rotation + focal length + k1 ---------------------------------------------------
def match(R, f, cx, cy, k1, tol):
    sky = (R @ cam_vec(det[:, 1:3], f, cx, cy, k1).T).T
    d, j = faint_tree.query(sky, distance_upper_bound=tol)
    ok = np.isfinite(d)
    return np.where(ok)[0], faint_idx[j[ok]]

def rotvec_to_R(rv):
    return cv2.Rodrigues(np.asarray(rv, float))[0]

f, cx, cy, k1 = f0, cx0, cy0, 0.0
rv = cv2.Rodrigues(R)[0].ravel()
for tol in (0.35, 0.25, 0.15, 0.10):
    di, ci = match(rotvec_to_R(rv), f, cx, cy, k1, math.radians(tol))
    def resid(p):
        Rm = rotvec_to_R(p[:3]); sky = (Rm @ cam_vec(det[di, 1:3], p[3], cx, cy, p[4]).T).T
        return (sky - CV[ci]).ravel()
    sol = optimize.least_squares(resid, np.r_[rv, f, k1], x_scale=[1e-2, 1e-2, 1e-2, 10, 0.01])
    rv, f, k1 = sol.x[:3], sol.x[3], sol.x[4]
    rms = math.degrees(math.sqrt(np.mean(np.sum(resid(sol.x).reshape(-1, 3) ** 2, 1)))) * 3600
    print(f"tol {tol:.2f} deg: {len(di)} matches, f {f:.1f} px, k1 {k1:+.4f}, rms {rms:.0f}\" ({rms / (3600 * math.degrees(1 / f)):.2f} px)", flush=True)
R = rotvec_to_R(rv)

# --- geometry ----------------------------------------------------------------------------------
def radec(v): return (math.degrees(math.atan2(v[1], v[0])) % 360, math.degrees(math.asin(np.clip(v[2], -1, 1))))
def sky_of_pixel(x, y): return R @ cam_vec(np.array([[x, y]]), f, cx, cy, k1)[0]
centre = sky_of_pixel(cx, cy)
times = []
for js in glob.glob(os.path.join(a.meta, "*.json")):
    m = json.load(open(js))
    if "captured_at" in m: times.append(dt.datetime.fromisoformat(m["captured_at"].replace("Z", "+00:00")))
t = min(times) + (max(times) - min(times)) / 2

def altaz(v, when):
    ra, de = map(math.radians, radec(v))
    jd = when.timestamp() / 86400 + 2440587.5; d = jd - 2451545.0
    lst = math.radians((280.46061837 + 360.98564736629 * d + a.lon) % 360)
    ha = lst - ra; la = math.radians(a.lat)
    alt = math.asin(math.sin(la) * math.sin(de) + math.cos(la) * math.cos(de) * math.cos(ha))
    az = math.atan2(-math.cos(de) * math.sin(ha), math.sin(de) * math.cos(la) - math.cos(de) * math.sin(la) * math.cos(ha))
    return math.degrees(alt), math.degrees(az) % 360

def compass(az):
    return ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"][int((az + 11.25) // 22.5) % 16]

tv = np.array([math.cos(math.radians(a.target_dec)) * math.cos(math.radians(a.target_ra)),
               math.cos(math.radians(a.target_dec)) * math.sin(math.radians(a.target_ra)), math.sin(math.radians(a.target_dec))])
sep = math.degrees(math.acos(np.clip(centre @ tv, -1, 1)))
calt, caz = altaz(centre, t); talt, taz = altaz(tv, t)
cam_t = R.T @ tv                                               # target in camera frame
inside = None
if cam_t[2] > 0:
    xn, yn = cam_t[0] / cam_t[2], cam_t[1] / cam_t[2]
    tx, ty = cx + f * xn, cy + f * yn
    inside = (0 <= tx < W and 0 <= ty < H, tx, ty)
# Phone "top edge" direction: portrait-up = raw -x (DNG orientation 6). Where on the horizon does it point?
up = sky_of_pixel(cx - 200, cy); ualt, uaz = altaz(up, t)
# Direction to the target in portrait screen terms (x right, y down): portrait (x', y') = (H-1-y, x)
dirtxt = ""
if cam_t[2] > 0:
    px, py = (H - 1 - ty) - (H - 1 - cy), tx - cx              # portrait offsets
    ang = (math.degrees(math.atan2(px, -py)) + 360) % 360       # 0 = toward the phone's top edge, 90 = screen-right
    dirtxt = f"{ang:.0f} deg clockwise from the phone's top edge (as seen on the screen)"
res = {"time_utc": t.isoformat(), "centre_radec": radec(centre), "centre_altaz": [calt, caz],
       "target": a.target, "target_altaz": [talt, taz], "separation_deg": sep,
       "target_in_frame": bool(inside and inside[0]), "target_pixel": inside[1:] if inside else None,
       "phone_top_edge_altaz": [ualt, uaz], "direction_on_screen": dirtxt, "f_px": f, "k1": k1,
       "cx": cx, "cy": cy, "R_cam_to_sky": R.tolist(), "image_wh": [W, H]}
json.dump(res, open(a.out + ".json", "w"), indent=2)
print(f"\ncamera points at RA {radec(centre)[0]:.1f} Dec {radec(centre)[1]:+.1f} = alt {calt:.0f} deg, az {caz:.0f} deg ({compass(caz)}) at {t:%H:%M} UTC", flush=True)
print(f"phone's top edge (camera end) leans toward az {uaz:.0f} ({compass(uaz)})", flush=True)
print(f"{a.target}: alt {talt:.0f} deg, az {taz:.0f} ({compass(taz)}); {sep:.1f} deg from the frame centre; "
      + ("IN the frame at pixel (%.0f, %.0f)" % inside[1:] if inside and inside[0] else "outside the frame")
      + (f"; {dirtxt}" if dirtxt else ""), flush=True)

# --- annotated portrait image ----------------------------------------------------------------
x = img - np.stack([cv2.resize(ndimage.median_filter(cv2.resize(img[..., c], (W // 16, H // 16), interpolation=cv2.INTER_AREA), size=9), (W, H)) for c in range(3)], -1)
n = 1.4826 * np.median(np.abs(x[..., 1] - np.median(x[..., 1]))); top_ = np.percentile(x, 99.99)
z = np.clip((x + 2 * n) / (top_ + 2 * n), 0, 1); beta = (top_ + 2 * n) / (2 * n)
v8 = (np.clip(np.arcsinh(z * beta) / np.arcsinh(beta) * 1.4, 0, 1) * 255).astype(np.uint8)
v8 = np.ascontiguousarray(cv2.cvtColor(v8, cv2.COLOR_RGB2BGR))
names = {"Alp And": "Alpheratz", "Bet And": "Mirach", "Gam1And": "Almach", "Alp Cas": "Schedar", "Bet Cas": "Caph",
         "Gam Cas": "Navi", "Del Cas": "Ruchbah", "Alp Per": "Mirfak", "Bet Per": "Algol", "Alp Cyg": "Deneb",
         "Alp Lyr": "Vega", "Alp Aql": "Altair", "Alp Aur": "Capella", "Alp Tau": "Aldebaran", "Alp UMi": "Polaris",
         "Alp Peg": "Markab", "Bet Peg": "Scheat", "Gam Peg": "Algenib", "Alp Ari": "Hamal", "Bet Ari": "Sheratan",
         "Alp Cep": "Alderamin", "Gam Cyg": "Sadr", "Eta Tau": "Pleiades", "Alp Tri": "Mothallah", "Bet Tri": "Bet Tri",
         "Eps Cyg": "Gienah", "Del And": "Del And", "Eps Per": "Eps Per", "Bet Aur": "Menkalinan", "Alp Cet": "Menkar"}
Rinv = R.T
def to_px(v):
    c = Rinv @ v
    if c[2] <= 0.2: return None
    xn, yn = c[0] / c[2], c[1] / c[2]; return cx + f * xn, cy + f * yn
for k in np.where(V < 3.0)[0]:
    p = to_px(CV[k])
    if p and 0 <= p[0] < W and 0 <= p[1] < H:
        nm = cat[k][3]
        lab = names.get(nm, nm)
        cv2.circle(v8, (int(p[0]), int(p[1])), 12, (90, 200, 255), 1, cv2.LINE_AA)
        cv2.putText(v8, lab, (int(p[0]) + 14, int(p[1]) + 5), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (90, 200, 255), 1, cv2.LINE_AA)
pt = to_px(tv)
if pt and 0 <= pt[0] < W and 0 <= pt[1] < H:
    cv2.ellipse(v8, (int(pt[0]), int(pt[1])), (30, 14), 0, 0, 360, (80, 255, 80), 2, cv2.LINE_AA)
    cv2.putText(v8, a.target, (int(pt[0]) + 34, int(pt[1]) + 6), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (80, 255, 80), 2, cv2.LINE_AA)
elif cam_t[2] > -0.9:
    dx, dy = cam_t[0], cam_t[1]; nrm = math.hypot(dx, dy) or 1
    ex = int(cx + dx / nrm * min(W, H) * 0.42); ey = int(cy + dy / nrm * min(W, H) * 0.42)
    cv2.arrowedLine(v8, (int(cx), int(cy)), (ex, ey), (80, 255, 80), 4, cv2.LINE_AA, tipLength=0.08)
    cv2.putText(v8, f"{a.target} {sep:.0f} deg this way", (ex + 10, ey), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (80, 255, 80), 2, cv2.LINE_AA)
cv2.drawMarker(v8, (int(cx), int(cy)), (255, 255, 255), cv2.MARKER_CROSS, 30, 1)
portrait = cv2.rotate(v8, cv2.ROTATE_90_CLOCKWISE)            # DNG orientation 6 -> as the phone shows it
cv2.imwrite(a.out + "_annotated.jpg", portrait, [cv2.IMWRITE_JPEG_QUALITY, 90])
print(f"wrote {a.out}_annotated.jpg and {a.out}.json", flush=True)
