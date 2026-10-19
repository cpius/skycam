#!/usr/bin/env python3
"""Quick-look JPEG from a SkyCam DNG: binned, background-subtracted, stretched, upright.

    ~/ASICAP/.venv/bin/python3 -u dashboard/quicklook.py --dng frame.dng --out frame.jpg --width 480

Works on the raw Bayer data: each 2x2 cell becomes one RGB pixel (the two greens averaged, the
as-shot WB applied), then area-averaged down to --width. The smooth sky (gradient, vignetting) is
subtracted per channel, so the sky comes out flat and neutral and only the stars stand out. The stretch puts
the sky background at --bg (0.10, not Siril's 0.25: a dark sky reads as a dark sky). Rotated
upright from the DNG's orientation flag (the phone shoots portrait into a landscape sensor).

Needs numpy, rawpy and cv2 (all in ~/ASICAP/.venv); dashboard.py calls it through that venv when
its own interpreter lacks them.
"""
import argparse
import sys

import cv2
import numpy as np
import rawpy


def render(dng, width, bg=0.10):
    with rawpy.imread(dng) as r:
        raw = r.raw_image_visible.astype(np.float32)
        black = float(np.mean(r.black_level_per_channel))
        pat = r.raw_pattern
        cdesc = r.color_desc.decode()
        flip = r.sizes.flip
        wb = np.array(r.camera_whitebalance[:3], np.float32)
    h, w = raw.shape[0] // 2 * 2, raw.shape[1] // 2 * 2
    raw = raw[:h, :w] - black
    rgb = np.zeros((h // 2, w // 2, 3), np.float32)
    n = np.zeros(3, np.float32)
    for dy in (0, 1):
        for dx in (0, 1):
            c = "RGB".index(cdesc[pat[dy, dx]])
            rgb[..., c] += raw[dy::2, dx::2]
            n[c] += 1
    rgb /= n
    if wb.min() > 0:
        rgb *= wb / wb[1]                  # as-shot WB, so the sky is not green
    hh, ww = rgb.shape[:2]
    if flip in (5, 6):                     # portrait: the long side becomes the height
        width_out = width
        height_out = round(width * ww / hh)
        small = cv2.resize(rgb, (height_out, width_out), interpolation=cv2.INTER_AREA)
        small = np.rot90(small, k=-1 if flip == 6 else 1)
    else:
        small = cv2.resize(rgb, (width, round(width * hh / ww)), interpolation=cv2.INTER_AREA)
        if flip == 3:
            small = small[::-1, ::-1]
    small = np.ascontiguousarray(small)

    # Subtract the smooth sky (skyglow gradient + vignetting) per channel: a 1/16-scale area
    # average barely sees the stars, and blurring it removes what is left of them.
    sh, sw = small.shape[:2]
    coarse = cv2.resize(small, (max(1, sw // 16), max(1, sh // 16)), interpolation=cv2.INTER_AREA)
    sky = cv2.resize(cv2.GaussianBlur(coarse, (0, 0), 2), (sw, sh), interpolation=cv2.INTER_LINEAR)
    x = small - sky
    noise = 1.4826 * np.median(np.abs(x - np.median(x))) + 1e-3
    top = max(np.percentile(x, 99.9), 30 * noise)
    # asinh is linear near zero and logarithmic above, so the sky noise stays a small grain
    # around `bg` while stars a thousand times brighter still fit under white.
    k = top / (3 * noise)
    y = np.clip(bg + (1 - bg) * np.arcsinh(k * x / top) / np.arcsinh(k), 0, 1)
    return (y[..., ::-1] * 255 + 0.5).astype(np.uint8)    # BGR for cv2


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dng", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--width", type=int, default=480)
    ap.add_argument("--bg", type=float, default=0.10, help="sky background level after the stretch")
    a = ap.parse_args()
    img = render(a.dng, a.width, a.bg)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        sys.exit("jpeg encode failed")
    with open(a.out, "wb") as f:
        f.write(buf.tobytes())
    print(f"{a.out}  {img.shape[1]}x{img.shape[0]}", flush=True)


if __name__ == "__main__":
    main()
