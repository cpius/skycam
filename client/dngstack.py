#!/usr/bin/env python3
"""Star-registered, sigma-clipped stack of SkyCam DNGs from a fixed phone (linear, 16-bit in).

    ~/ASICAP/.venv/bin/python3 -u client/dngstack.py --dir test/sky/stack60 --out test/sky/stack60/stack
    # long runs with foreground in view (the M31 recipe):
    ~/ASICAP/.venv/bin/python3 -u client/dngstack.py --dir run2 run3 --out final/stack \
        --solution solve/solved.json --flats flats/ --self-dark 800

Each DNG is demosaiced at half size (2x2 -> 1 pixel, 2016x1512 on the iPhone Air) with a linear
curve, no auto-brightening and the white balance it was shot with, then registered onto the
middle frame and combined with a two-pass kappa-sigma mean. Writes <out>_linear.npy,
<out>_16.tif (linear), <out>_stretched.jpg, <out>_single_stretched.jpg and <out>_compare.jpg.

Vignetting: Apple calibrates the lens and stores a FixVignetteRadial opcode (OpcodeList3) in every
DNG; rawpy/LibRaw ignore it, which leaves the corners at ~27% of the centre on the iPhone Air.
This script applies it (corner gain ~3.9x) unless --no-vignette.

--darks: median of the dark frames, subtracted on the raw mosaic before demosaicing.
--flats: frames of an evenly lit white surface at the same focus (iPad full-white under a sheet of
paper: ISO 800, 16 ms = 44% of full scale). Their median, normalised per Bayer position, divides
every frame on the raw mosaic and replaces Apple's vignette model (also fixes colour shading).
Obstructed frames (a box flap or hand lit in front of the lens) are dropped unless --keep-obstructed.
--self-dark N: build the sensor's fixed pattern from N of the light frames themselves instead of
--darks. Stars drift across the sensor during a long run while the bias pattern stays put, so a
3-sigma-clipped mean of the UNALIGNED frames (stars, planes and satellites masked per frame) is
sky + pattern; its high-pass per Bayer plane is subtracted from every frame. On the M31 run 30
darks were too few -- their own ~0.8 ADU noise is fixed to the sensor too and walked into the same
diagonal streaks -- while --self-dark 800 removed them. Saved as <out>_fixedpattern.npy; reuse it
with --self-dark-file. Without the per-frame source mask, bright-star trails leak into the
pattern and print thin dark lines along the drift.
Foreground mask (unless --no-foreground-mask): railings and walls are fixed on the sensor while the
stars move, so in the median of 25 unaligned frames they stand out; those areas become "no data" in
every frame, so the stack never mixes them into the sky. --mask-only writes <out>_fgmask.jpg and stops.

--solution (phonesolve.py JSON): needed when the sky drifts far across the frame or there is
foreground in view. Frames are undistorted with the solved focal length and k1, and each frame's
star motion is PREDICTED from its timestamp -- the camera is fixed to the Earth, so the sky turns
about the celestial pole at the sidereal rate -- and stars are matched only near the prediction,
then fitted with a homography. Without this, a railing's sharp texture wins the matching: the
"alignment" locks onto the foreground (still ~0.5 px rms!) and every star trails. If the phone
itself moved, an extra offset is searched around the prediction. Check stack stars are points.
"""
import argparse, glob, json, math, os, struct, subprocess, time
import numpy as np, cv2, rawpy
from scipy import ndimage
from scipy.spatial import cKDTree

ap = argparse.ArgumentParser()
ap.add_argument("--dir", required=True, nargs="+", help="one or more folders of DNGs (e.g. two runs on the same field)")
ap.add_argument("--out", required=True)
ap.add_argument("--kappa", type=float, default=2.5)
ap.add_argument("--nstars", type=int, default=400)
ap.add_argument("--radius", type=float, default=25, help="match radius, px")
ap.add_argument("--min-inliers", type=int, default=12, help="RANSAC inliers needed to accept a frame's alignment")
ap.add_argument("--solution", help="phonesolve.py JSON (f_px, k1): undistort every frame first and align with a full homography -- needed when the sky drifts far across the field")
ap.add_argument("--flats", help="folder of flat frames (same focus; even white light, ~45%% of full scale): every frame is divided by their normalised median on the raw data. Replaces Apple's vignette correction, and also fixes colour shading, dust and pixel response")
ap.add_argument("--self-dark", type=int, default=0, metavar="N",
                help="build the sensor-fixed pattern from N of the light frames themselves (sigma-clipped mean of the "
                     "UNALIGNED frames, high-pass per Bayer plane) and subtract it; replaces --darks, whose own noise "
                     "is fixed to the sensor and walks into streaks when there are only a few dozen")
ap.add_argument("--self-dark-file", help="reuse a fixed pattern saved by an earlier --self-dark run (<out>_fixedpattern.npy)")
ap.add_argument("--darks", help="folder of dark frames (lens covered, same exposure/ISO): their median is subtracted from every frame on the raw data")
ap.add_argument("--background", type=float, default=0.10, help="stretched sky level (0..1)")
ap.add_argument("--no-vignette", action="store_true", help="skip Apple's FixVignetteRadial correction")
ap.add_argument("--no-foreground-mask", action="store_true", help="keep railings/walls in the stack")
ap.add_argument("--mask-only", action="store_true", help="write <out>_fgmask.png and the median it came from, then stop")
ap.add_argument("--keep-obstructed", action="store_true", help="do not drop frames with something bright in front of the lens")
a = ap.parse_args()

files = sorted(f for d in a.dir for f in glob.glob(os.path.join(d, "*.dng")))
assert files, "no DNGs in " + " ".join(a.dir)
t0 = time.time()
def el(): return "[%5.0fs]" % (time.time() - t0)


def vignette_gain(f, shape):
    """Gain map from the DNG's FixVignetteRadial opcode (DNG spec 1.3): g = 1 + k0 r^2 + ... + k4 r^10,
    r = distance from (cx, cy) normalised by the farthest corner."""
    raw = subprocess.run(["exiftool", "-b", "-OpcodeList3", f], capture_output=True).stdout
    if len(raw) < 4:
        return None
    n = struct.unpack(">I", raw[:4])[0]; p = 4
    for _ in range(n):
        oid, _v, _fl, nb = struct.unpack(">IIII", raw[p:p + 16]); data = raw[p + 16:p + 16 + nb]; p += 16 + nb
        if oid == 3:
            k0, k1, k2, k3, k4, cx, cy = struct.unpack(">7d", data)
            H, W = shape
            yy, xx = np.mgrid[0:H, 0:W]
            dx, dy = ((xx + 0.5) / W - cx) * W, ((yy + 0.5) / H - cy) * H
            r2 = (dx * dx + dy * dy) / (max(cx, 1 - cx) ** 2 * W * W + max(cy, 1 - cy) ** 2 * H * H)
            return (1 + k0 * r2 + k1 * r2 ** 2 + k2 * r2 ** 3 + k3 * r2 ** 4 + k4 * r2 ** 5).astype(np.float32)
    return None


DARK = None
if a.darks:
    dfiles = sorted(glob.glob(os.path.join(a.darks, "*.dng")))
    assert dfiles, "no darks in " + a.darks
    stack_ = []
    for f in dfiles:
        with rawpy.imread(f) as r:
            stack_.append(r.raw_image_visible.astype(np.float32))
            black = float(np.mean(r.black_level_per_channel))
    DARK = np.median(stack_, axis=0) - black          # fixed pattern + hot pixels, black level removed
    del stack_
    print("master dark from %d frames: median %+.2f ADU, %d hot pixels above +50" %
          (len(dfiles), float(np.median(DARK)), int((DARK > 50).sum())), flush=True)

UNDIST = None
def undistort_maps(shape, f, k1):
    """Remap from a pinhole (rectilinear) image back into the lens's radially distorted one.
    phonesolve's model: x_undist = x_dist / (1 + k1 r_dist^2), r in units of f."""
    H, W = shape; cx, cy = (W - 1) / 2, (H - 1) / 2
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    xu, yu = (xx - cx) / f, (yy - cy) / f
    xd, yd = xu.copy(), yu.copy()
    for _ in range(8):                                  # fixed point: x_d = x_u (1 + k1 r_d^2)
        r2 = xd * xd + yd * yd
        xd, yd = xu * (1 + k1 * r2), yu * (1 + k1 * r2)
    return (xd * f + cx).astype(np.float32), (yd * f + cy).astype(np.float32)

FG = None
SELF = None
FLAT = None
if a.flats:
    ffiles = sorted(glob.glob(os.path.join(a.flats, "*.dng")))
    assert ffiles, "no flats in " + a.flats
    fl = []
    for f in ffiles:
        with rawpy.imread(f) as r:
            fl.append(r.raw_image_visible.astype(np.float32) - float(np.mean(r.black_level_per_channel)))
    FLAT = np.median(fl, axis=0); del fl
    hh, ww = FLAT.shape
    for dy in (0, 1):                                   # normalise each Bayer position to 1 at the centre,
        for dx in (0, 1):                               # so the colour balance is left alone
            plane = FLAT[dy::2, dx::2]
            plane /= np.median(plane[hh // 4 - 50:hh // 4 + 50, ww // 4 - 50:ww // 4 + 50])
    FLAT = np.clip(FLAT, 0.05, None)
    print("master flat from %d frames: corners at %.0f%% of the centre" % (len(ffiles), 100 * float(np.median(FLAT[:100, :100]))), flush=True)
    a.no_vignette = True                                # the flat already contains the vignetting

GAIN = None
def load(f):
    global GAIN
    with rawpy.imread(f) as r:
        if DARK is not None or FLAT is not None or SELF is not None:   # calibrate on the raw mosaic
            raw = r.raw_image_visible
            black = float(np.mean(r.black_level_per_channel))
            x = raw.astype(np.float32) - black
            if DARK is not None:
                x -= DARK
            if FLAT is not None:
                x /= FLAT
            if SELF is not None:
                x -= SELF
            raw[:] = np.clip(x + black, 0, 65535).astype(raw.dtype)
        rgb = r.postprocess(half_size=True, gamma=(1, 1), no_auto_bright=True, output_bps=16,
                            use_camera_wb=True, user_flip=0)
    img = rgb.astype(np.float32) / 65535.0
    if not a.no_vignette:
        if GAIN is None:
            GAIN = vignette_gain(f, img.shape[:2])
            print("vignette correction: " + ("none in DNG" if GAIN is None else
                  "corner gain %.2f" % GAIN[0, 0]), flush=True)
        if GAIN is not None:
            img *= GAIN[..., None]
    if UNDIST is not None:
        img = cv2.remap(img, UNDIST[0], UNDIST[1], cv2.INTER_LINEAR, borderValue=(-1, -1, -1))
    if FG is not None:
        img[FG] = -1                                    # foreground: no data, so it never enters the stack
    return img


def stars(img):
    g = img[..., 1]
    valid = cv2.erode((g > -0.5).astype(np.uint8), np.ones((31, 31), np.uint8)).astype(bool)
    g = np.where(g > -0.5, g, 0)
    d = ndimage.gaussian_filter(g, 1.0) - ndimage.gaussian_filter(g, 5)
    d[~valid] = 0                                         # undistortion leaves an empty (-1) border
    # Local noise: the vignette correction multiplies corner noise ~4x, so a global threshold
    # would turn the corners into a carpet of false stars.
    t = 48; Hh, Ww = d.shape
    tiles = np.abs(d[:Hh // t * t, :Ww // t * t]).reshape(Hh // t, t, Ww // t, t)
    sig = cv2.resize(np.median(tiles, axis=(1, 3)).astype(np.float32) * 1.4826, (Ww, Hh), interpolation=cv2.INTER_LINEAR) + 1e-9
    pk = np.argwhere((d == ndimage.maximum_filter(d, 9)) & (d > 8 * sig))
    pk = pk[(pk[:, 0] >= 4) & (pk[:, 0] < g.shape[0] - 4) & (pk[:, 1] >= 4) & (pk[:, 1] < g.shape[1] - 4)]
    if STATIC is not None and len(pk):
        # Drop foreground BEFORE ranking: a railing's texture outshines most stars and would
        # otherwise fill the whole top-N list.
        dd, _ = STATIC.query(pk[:, ::-1].astype(np.float32), distance_upper_bound=3.0)
        pk = pk[~np.isfinite(dd)]
    v = d[pk[:, 0], pk[:, 1]]
    pts = []
    yy, xx = np.mgrid[-3:4, -3:4]
    for y, x in pk[np.argsort(-v)]:
        if len(pts) >= a.nstars:
            break
        w = np.clip(d[y - 3:y + 4, x - 3:x + 4], 0, None); s = w.sum()
        if s <= 0 or w.max() / s > 0.6:
            continue                                      # hot pixel
        mx_, my_ = (w * xx).sum() / s, (w * yy).sum() / s
        cxx = (w * (xx - mx_) ** 2).sum() / s; cyy = (w * (yy - my_) ** 2).sum() / s; cxy = (w * (xx - mx_) * (yy - my_)).sum() / s
        l1, l2 = np.linalg.eigvalsh([[cxx, cxy], [cxy, cyy]])[::-1]
        if l2 <= 0 or l1 / l2 > 2.5:
            continue                                      # elongated: an edge, not a star
        pts.append((x + mx_, y + my_))
    return np.array(pts, np.float32).reshape(-1, 2)


def obstructed(f):
    """Something lit right in front of the lens (a box flap, a hand): a large patch far above the sky."""
    with rawpy.imread(f) as r:
        g = r.raw_image_visible[0::8, 1::8].astype(np.float32) - np.mean(r.black_level_per_channel)
    return np.mean(g > 4 * np.median(g) + 20) > 0.08

if not a.keep_obstructed:
    blocked = [f for f in files if obstructed(f)]
    if blocked:
        print("%s dropping %d of %d frames with something in front of the lens" % (el(), len(blocked), len(files)), flush=True)
    files = [f for f in files if f not in set(blocked)]
    assert files, "every frame is obstructed"

def starmap(pts, shape):
    m = np.zeros(shape, np.float32)
    for x, y in pts[:150]:
        if 0 <= int(y) < shape[0] and 0 <= int(x) < shape[1]: m[int(y), int(x)] = 1
    return cv2.GaussianBlur(m, (0, 0), 3)

def calibrated_mosaic(f):
    with rawpy.imread(f) as r:
        x = r.raw_image_visible.astype(np.float32) - float(np.mean(r.black_level_per_channel))
    if DARK is not None: x -= DARK
    if FLAT is not None: x /= FLAT
    return x

if a.self_dark_file:
    SELF = np.load(a.self_dark_file).astype(np.float32)
    print("fixed pattern loaded from %s" % a.self_dark_file, flush=True)
elif a.self_dark:
    # Stars drift across the sensor during a long run while bias/dark structure stays put, so a
    # sigma-clipped mean of unaligned frames is sky + that structure. Keep only its high-pass part
    # (per Bayer plane, so colours never mix): that is the fixed pattern, at the lights' own ISO,
    # exposure and temperature, with noise ~ 1/sqrt(N) of a frame.
    sel = [files[k] for k in np.linspace(0, len(files) - 1, min(a.self_dark, len(files))).astype(int)]

    def source_mask(x):
        """Stars, planes and satellites in one frame (per Bayer plane, local noise), grown by 6 px:
        a bright star trails across the sensor during the run and must not leak into the pattern."""
        m = np.zeros(x.shape, bool)
        for dy in (0, 1):
            for dx in (0, 1):
                pl = x[dy::2, dx::2]
                hp = pl - cv2.GaussianBlur(pl, (0, 0), 4)
                t = 32; h2, w2 = hp.shape
                tl = np.abs(hp[:h2 // t * t, :w2 // t * t]).reshape(h2 // t, t, w2 // t, t)
                sig = cv2.resize(np.median(tl, axis=(1, 3)).astype(np.float32) * 1.4826, (w2, h2), interpolation=cv2.INTER_LINEAR)
                pm = cv2.dilate((hp > 3.5 * sig).astype(np.uint8), np.ones((7, 7), np.uint8)).astype(bool)
                m[dy::2, dx::2] |= pm
        # a source in one colour plane masks the whole 2x2 cell
        cell = m[0::2, 0::2] | m[0::2, 1::2] | m[1::2, 0::2] | m[1::2, 1::2]
        return np.repeat(np.repeat(cell, 2, 0), 2, 1)[:x.shape[0], :x.shape[1]]

    S1 = None
    for k, f in enumerate(sel):
        x = calibrated_mosaic(f); ok = ~source_mask(x)
        if S1 is None: S1, S2, N1 = np.zeros(x.shape), np.zeros(x.shape), np.zeros(x.shape, np.float32)
        xd = np.where(ok, x, 0).astype(np.float64)
        S1 += xd; S2 += xd * xd; N1 += ok
        if k % 100 == 99: print("%s self-dark pass 1: %d/%d" % (el(), k + 1, len(sel)), flush=True)
    mu = S1 / np.maximum(N1, 1); sd = np.sqrt(np.maximum(S2 / np.maximum(N1, 1) - mu * mu, 0)) + 1e-3
    print("%s   sources masked: %.1f%% of samples" % (el(), 100 * (1 - N1.sum() / (N1.size * len(sel)))), flush=True)
    del S1, S2, N1
    C1 = np.zeros_like(mu); K1 = np.zeros_like(mu, dtype=np.float32)
    for k, f in enumerate(sel):
        x = calibrated_mosaic(f)
        keep = ~source_mask(x) & (np.abs(x - mu) <= 3 * sd)
        C1 += np.where(keep, x, 0); K1 += keep
        if k % 100 == 99: print("%s self-dark pass 2: %d/%d" % (el(), k + 1, len(sel)), flush=True)
    M_ = (C1 / np.maximum(K1, 1)).astype(np.float32); del C1, K1, mu, sd
    SELF = np.zeros_like(M_)
    for dy in (0, 1):
        for dx in (0, 1):
            pl = M_[dy::2, dx::2]
            SELF[dy::2, dx::2] = pl - cv2.GaussianBlur(pl, (0, 0), 6)
    amp = 1.4826 * float(np.median(np.abs(SELF[SELF.shape[0] // 4:-SELF.shape[0] // 4, SELF.shape[1] // 10:-SELF.shape[1] // 3])))
    print("%s fixed pattern from %d unaligned lights: %.2f ADU rms (subtracted from every frame)" % (el(), len(sel), amp), flush=True)
    np.save(a.out + "_fixedpattern.npy", SELF)
    if DARK is not None:
        print("   note: --darks is also set; their noise is now subtracted on top -- usually leave them out", flush=True)

STATIC = None
SOLVE_R = None
def frame_time(f):
    import datetime as _dt
    m = json.load(open(f[:-4] + ".json"))
    return _dt.datetime.fromisoformat(m["captured_at"].replace("Z", "+00:00")).timestamp()

def predicted_h(t, t0):
    """Homography taking (undistorted) pixels of a frame at time t onto the reference at t0, for a
    camera fixed to the Earth: the sky turns about the celestial pole at the sidereal rate."""
    w = 2 * math.pi / 86164.0905 * (t0 - t)
    Rz = np.array([[math.cos(w), -math.sin(w), 0], [math.sin(w), math.cos(w), 0], [0, 0, 1]])
    Q = SOLVE_R.T @ Rz.T @ SOLVE_R             # camera vector at t  ->  camera vector at t0
    Hm = KMAT @ Q @ np.linalg.inv(KMAT)
    return Hm / Hm[2, 2]

ref_i = len(files) // 2
if a.solution:
    import json as _json
    sol = _json.load(open(a.solution))
    with rawpy.imread(files[ref_i]) as r:
        hh, ww = r.sizes.iheight // 2, r.sizes.iwidth // 2
    UNDIST = undistort_maps((hh, ww), sol["f_px"], sol["k1"])
    import datetime as _dt
    SOLVE_T = _dt.datetime.fromisoformat(sol["time_utc"]).timestamp()
    SOLVE_R = np.array(sol["R_cam_to_sky"]) if "R_cam_to_sky" in sol else None
    KMAT = np.array([[sol["f_px"], 0, sol.get("cx", (ww - 1) / 2)], [0, sol["f_px"], sol.get("cy", (hh - 1) / 2)], [0, 0, 1]])
    print("undistorting with f %.1f px, k1 %+.4f; aligning with a homography" % (sol["f_px"], sol["k1"]), flush=True)
# Foreground (railings, walls) produces sharp "stars" that never move; aligning on them smears the
# sky. Points found at the same pixel in the first and last frame are static -- real stars drift
# ~5 px a minute -- so they are excluded from every frame's detections.
if (os.path.getmtime(files[-1]) - os.path.getmtime(files[0])) > 120 or len(files) > 60:
    _n = a.nstars; a.nstars = 3000
    p_first, p_last = stars(load(files[0])), stars(load(files[-1]))
    a.nstars = _n
    dd, _ = cKDTree(p_last).query(p_first, distance_upper_bound=1.5)
    static = p_first[np.isfinite(dd)]
    if len(static):
        STATIC = cKDTree(static)
    print("%s %d static (foreground) points excluded from alignment" % (el(), len(static)), flush=True)
if not a.no_foreground_mask and len(files) >= 20:
    # Railings and walls are fixed on the sensor while the stars move, so in the median of
    # frames spread over the run the stars vanish and the foreground stays. Mark 32-px tiles
    # whose level departs from a smooth sky fit, or that are textured, then grow the mask.
    idx = np.linspace(0, len(files) - 1, 25).astype(int)
    _self, SELF = SELF, None                            # judge foreground on frames with their normal texture
    med = np.median([load(files[k])[..., 1] for k in idx], axis=0)
    SELF = _self
    t = 32; Hh, Ww = med.shape; th, tw = Hh // t, Ww // t
    tiles = med[:th * t, :tw * t].reshape(th, t, tw, t).transpose(0, 2, 1, 3).reshape(th, tw, -1)
    valid_t = (tiles > -0.5).mean(2) > 0.5
    lev = np.where(valid_t, np.median(np.where(tiles > -0.5, tiles, np.nan), axis=2), np.nan)
    hp = med - cv2.GaussianBlur(np.where(med > -0.5, med, 0).astype(np.float32), (0, 0), 3)
    tex = np.nanstd(np.where(tiles > -0.5, hp[:th * t, :tw * t].reshape(th, t, tw, t).transpose(0, 2, 1, 3).reshape(th, tw, -1), np.nan), axis=2)
    yy, xx = np.mgrid[0:th, 0:tw] / np.array([th, tw])[:, None, None]
    A_ = np.stack([np.ones_like(xx), xx, yy, xx * xx, yy * yy, xx * yy], -1).reshape(-1, 6)
    good = valid_t.ravel() & np.isfinite(lev.ravel())
    for _ in range(5):                                  # robust quadratic sky fit
        c, *_ = np.linalg.lstsq(A_[good], lev.ravel()[good], rcond=None)
        fit = (A_ @ c).reshape(th, tw)
        rel = np.abs(lev / fit - 1)
        good = good & (rel.ravel() < 0.25) & (tex.ravel() < 3 * np.nanmedian(tex))
    full_t = (tiles > -0.5).all(2)                       # tiles cut by the undistortion border are not foreground
    fg_t = full_t & ((rel > 0.25) | (tex > 3 * np.nanmedian(tex)))
    fg = cv2.resize(fg_t.astype(np.uint8), (tw * t, th * t), interpolation=cv2.INTER_NEAREST)
    fg = cv2.copyMakeBorder(fg, 0, Hh - th * t, 0, Ww - tw * t, cv2.BORDER_REPLICATE)
    fg = cv2.dilate(cv2.morphologyEx(fg, cv2.MORPH_CLOSE, np.ones((65, 65), np.uint8)), np.ones((49, 49), np.uint8))
    FG = fg.astype(bool)
    print("%s foreground mask: %.1f%% of the frame (median of %d unaligned frames)" % (el(), 100 * FG.mean(), len(idx)), flush=True)
    m8 = np.clip(np.where(med > -0.5, med, 0) / max(np.percentile(med, 99.5), 1e-6), 0, 1) ** 0.5 * 255
    vis = cv2.cvtColor(m8.astype(np.uint8), cv2.COLOR_GRAY2BGR); vis[FG] = (0.5 * vis[FG] + [0, 0, 120]).astype(np.uint8)
    cv2.imwrite(a.out + "_fgmask.jpg", cv2.rotate(vis, cv2.ROTATE_90_CLOCKWISE))
    if a.mask_only:
        raise SystemExit(0)
ref = load(files[ref_i]); H, W = ref.shape[:2]
T_REF = frame_time(files[ref_i]) if a.solution else None
ref_pts = stars(ref); tree = cKDTree(ref_pts); ref_map = starmap(ref_pts, (H, W))
print("%s %d frames, reference %s (%d stars)" % (el(), len(files), os.path.basename(files[ref_i]), len(ref_pts)), flush=True)

M = []
for i, f in enumerate(files):
    img = ref if i == ref_i else load(f)
    p = stars(img)
    if SOLVE_R is not None:
        # Predict where each star should be from the sky's rotation, match near the prediction only
        # (foreground does not move with the sky, so it cannot match), then fit the residual.
        Hp = predicted_h(frame_time(f), T_REF)
        pp = cv2.perspectiveTransform(p[None].astype(np.float64), Hp)[0].astype(np.float32)
        if os.environ.get("DNGSTACK_DEBUG") and i != ref_i:
            c = cv2.perspectiveTransform(np.float64([[[W / 2, H / 2]]]), Hp)[0][0]
            best = max(((int(np.isfinite(tree.query(pp + np.float32([dx, dy]), distance_upper_bound=2.0)[0]).sum()), dx, dy)
                        for dx in range(-160, 161, 4) for dy in range(-160, 161, 4)))
            print("   debug: dt %+.1f min, predicted centre shift %+.1f %+.1f, best extra offset %+d %+d (%d of %d)"
                  % ((frame_time(f) - T_REF) / 60, c[0] - W / 2, c[1] - H / 2, best[1], best[2], best[0], len(p)), flush=True)
        d2, j2 = tree.query(pp, distance_upper_bound=6.0)
        ok2 = np.isfinite(d2)
        if ok2.sum() < a.min_inliers:
            # The phone itself moved (a sagging box): search for the extra offset around the prediction.
            q = pp[:150]
            best = max(((int(np.isfinite(tree.query(q + np.float32([dx, dy]), distance_upper_bound=2.0)[0]).sum()), dx, dy)
                        for dx in range(-200, 201, 3) for dy in range(-200, 201, 3)))
            if best[0] >= 15:
                pp = pp + np.float32([best[1], best[2]])
                d2, j2 = tree.query(pp, distance_upper_bound=4.0)
                ok2 = np.isfinite(d2)
                print("   %d: phone had moved %+d %+d px; re-matched %d stars" % (i, best[1], best[2], int(ok2.sum())), flush=True)
        Hm, inl2 = (None, None) if ok2.sum() < 8 else cv2.findHomography(p[ok2], ref_pts[j2[ok2]], cv2.RANSAC, 1.0)
        if Hm is not None and inl2.sum() >= a.min_inliers:
            sel = inl2.ravel() == 1
            res = np.hypot(*(cv2.perspectiveTransform(p[ok2][sel][None], Hm)[0] - ref_pts[j2[ok2]][sel]).T)
            dev = np.hypot(*(cv2.perspectiveTransform(np.float32([[W / 2, H / 2]])[None], Hm)[0] -
                             cv2.perspectiveTransform(np.float32([[W / 2, H / 2]])[None], Hp)[0]).T)[0]
            print("%s %2d: %3d/%3d matched (predicted sky motion), rms %.2f px, %.1f px off prediction"
                  % (el(), i, int(sel.sum()), len(p), np.sqrt((res ** 2).mean()), dev), flush=True)
            M.append(Hm); continue
        print("%s %2d: only %d stars near their predicted places, skipped" % (el(), i, int(ok2.sum())), flush=True)
        M.append(None); continue
    # Coarse shift first (frames from another run can be ~100 px away), then match within --radius.
    (sx, sy), _ = cv2.phaseCorrelate(starmap(p, (H, W)), ref_map)
    d, j = tree.query(p + np.float32([sx, sy]), distance_upper_bound=a.radius)
    ok = np.isfinite(d)
    A, inl = (None, None) if ok.sum() < 3 else cv2.estimateAffinePartial2D(
        p[ok], ref_pts[j[ok]], method=cv2.RANSAC, ransacReprojThreshold=3.0 if a.solution else 1.0, maxIters=5000)
    if A is not None and a.solution:
        # Re-match every star through the rough fit, then a homography (exact for a rotating
        # pinhole camera once the lens distortion is removed).
        d2, j2 = tree.query(cv2.transform(p[None], A)[0], distance_upper_bound=4.0)
        ok2 = np.isfinite(d2)
        Hm, inl2 = (None, None) if ok2.sum() < 8 else cv2.findHomography(p[ok2], ref_pts[j2[ok2]], cv2.RANSAC, 1.0)
        if Hm is not None and inl2.sum() >= a.min_inliers:
            res = np.hypot(*(cv2.perspectiveTransform(p[ok2][inl2.ravel() == 1][None], Hm)[0] - ref_pts[j2[ok2]][inl2.ravel() == 1]).T)
            print("%s %2d: %3d/%3d matched (homography), rms %.2f px" % (el(), i, int(inl2.sum()), len(p), np.sqrt((res ** 2).mean())), flush=True)
            M.append(Hm); continue
        A, inl = None, inl2 if inl2 is not None else inl
    if A is None or inl.sum() < a.min_inliers:
        print("%s %2d: %s, skipped" % (el(), i, "no fit" if A is None else "only %d stars matched" % inl.sum()), flush=True)
        M.append(None); continue
    sel = inl.ravel() == 1
    res = np.hypot(*(cv2.transform(p[ok][sel][None], A)[0] - ref_pts[j[ok]][sel]).T)
    print("%s %2d: %3d/%3d matched, rot %+.3f deg, shift %+5.1f %+5.1f px, rms %.2f px"
          % (el(), i, int(sel.sum()), len(p), math.degrees(math.atan2(A[1, 0], A[0, 0])), A[0, 2], A[1, 2],
             np.sqrt((res ** 2).mean())), flush=True)
    M.append(A)

good = [(f, A) for f, A in zip(files, M) if A is not None]
S = np.zeros((H, W, 3), np.float64); S2 = np.zeros_like(S); N = np.zeros((H, W, 1), np.float32)
def warped(f, A):
    if A.shape == (3, 3):
        return cv2.warpPerspective(load(f), A, (W, H), flags=cv2.INTER_LINEAR, borderValue=(-1, -1, -1))
    return cv2.warpAffine(load(f), A, (W, H), flags=cv2.INTER_LINEAR, borderValue=(-1, -1, -1))
for k, (f, A) in enumerate(good):
    w = warped(f, A); m = (w[..., :1] >= 0).astype(np.float32); w = np.where(w >= 0, w, 0)
    S += w; S2 += w.astype(np.float64) ** 2; N += m
    if k % 10 == 9: print("%s pass 1: %d/%d" % (el(), k + 1, len(good)), flush=True)
mean = S / np.maximum(N, 1); sd = np.sqrt(np.maximum(S2 / np.maximum(N, 1) - mean ** 2, 0)) + 1e-6
del S2
C = np.zeros_like(S); K = np.zeros((H, W, 3), np.float32)
for k, (f, A) in enumerate(good):
    w = warped(f, A)
    keep = (w >= 0) & (np.abs(w - mean) <= a.kappa * sd)
    C += np.where(keep, w, 0); K += keep
    if k % 10 == 9: print("%s pass 2: %d/%d" % (el(), k + 1, len(good)), flush=True)
stack = (C / np.maximum(K, 1)).astype(np.float32)
np.save(a.out + "_linear.npy", stack)
cv2.imwrite(a.out + "_16.tif", cv2.cvtColor(np.clip(stack * 65535, 0, 65535).astype(np.uint16), cv2.COLOR_RGB2BGR))
print("%s stacked %d of %d frames; %.2f%% of samples clipped" % (el(), len(good), len(files), 100 * (1 - K.sum() / (N.sum() * 3))), flush=True)


def stretch(img, ref_stats=None):
    """Subtract a smooth sky (gradient + vignetting), then an asinh stretch that puts the
    background at --background. Returns 8-bit RGB and the stats so a second image can share them."""
    sky = np.stack([cv2.resize(ndimage.median_filter(cv2.resize(img[..., c], (W // 16, H // 16), interpolation=cv2.INTER_AREA), size=9),
                               (W, H), interpolation=cv2.INTER_CUBIC) for c in range(3)], -1)
    x = img - sky
    if ref_stats is None:
        noise = 1.4826 * np.median(np.abs(x[..., 1] - np.median(x[..., 1])))
        top = np.percentile(x, 99.995)
        ref_stats = (noise, top)
    noise, top = ref_stats
    lo = -2 * noise
    z = np.clip((x - lo) / (top - lo), 0, 1)
    beta = 1 / max(1e-4, (2 * noise) / (top - lo))
    y = np.arcsinh(z * beta) / np.arcsinh(beta)
    bg = np.median(y)
    y = np.clip(y * (a.background / max(bg, 1e-6)) if bg > a.background else y, 0, 1)
    return (y * 255).astype(np.uint8), ref_stats


st8, stats = stretch(stack)
cv2.imwrite(a.out + "_stretched.jpg", cv2.cvtColor(st8, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 92])
single8, _ = stretch(ref, None)
cv2.imwrite(a.out + "_single_stretched.jpg", cv2.cvtColor(single8, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 92])
noise_single = 1.4826 * np.median(np.abs(np.diff(ref[..., 1], axis=1))) / math.sqrt(2)
noise_stack = 1.4826 * np.median(np.abs(np.diff(stack[..., 1], axis=1))) / math.sqrt(2)
print("%s pixel noise: single %.2e, stack %.2e -> %.1fx lower (sqrt(%d) = %.1f)"
      % (el(), noise_single, noise_stack, noise_single / noise_stack, len(good), math.sqrt(len(good))), flush=True)
cy, cx, s = H // 3, W // 2, 380
crop = lambda im: cv2.resize(im[cy - s // 2:cy + s // 2, cx - s // 2:cx + s // 2], (2 * s, 2 * s), interpolation=cv2.INTER_NEAREST)
cmp = np.hstack([crop(single8), np.full((2 * s, 12, 3), 60, np.uint8), crop(st8)])
cv2.putText(cmp, "1 frame, 1 s", (12, 34), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
cv2.putText(cmp, "stack, %d x 1 s" % len(good), (2 * s + 24, 34), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
cv2.imwrite(a.out + "_compare.jpg", cv2.cvtColor(cmp, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 92])
print("%s wrote %s_{linear.npy,16.tif,stretched.jpg,single_stretched.jpg,compare.jpg}" % (el(), a.out), flush=True)
