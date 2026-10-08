#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pillow", "numpy", "potracer", "resvg-py", "opencv-python-headless", "scipy"]
# ///
"""Convert a shaded grayscale illustration (3D-style art, glossy icons) to SVG.

usage: rasterless_shaded.py IN [IN ...] [-o OUT] [--levels N] [--blur S] [--spheres] [--preview]

  -o OUT       output file, or a folder (required form for several inputs); default: next to IN
  --levels N   gray bands (default 20); more = smoother shading and a bigger file
  --blur S     softening of band steps in px (default 1); 0 = hard bands
  --spheres    replace round glossy balls with real radial-gradient circles
  --preview    also write a preview PNG (original | SVG render | mismatches in red) to a temp folder
"""
import argparse
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from scipy import ndimage

import rasterless

COLOR_LIMIT = 20  # above this is a color image
BG_DIST = 8       # min distance from the background
REF = 1254        # size the radii were tuned at
SPECK = 6         # specks hide under the blur
rasterless.OPT_TOL = 1.0  # soft edges allow a looser fit


def grad_t(px, py, fx, fy):
    """radialGradient t for points (unit circle, focal fx, fy)."""
    dx, dy = px - fx, py - fy
    a = dx * dx + dy * dy
    b = 2 * (fx * dx + fy * dy)
    c = fx * fx + fy * fy - 1
    s = (-b + np.sqrt(b * b - 4 * a * c)) / (2 * a + 1e-12)
    return np.clip(1 / np.maximum(s, 1e-6), 0, 1)


def find_spheres(g):
    """Find glossy balls and fit one shared gradient."""
    h, w = g.shape
    side = max(h, w)
    c = cv2.HoughCircles(cv2.medianBlur(g, 5), cv2.HOUGH_GRADIENT, dp=1, minDist=side * 0.03, param1=120,
                         param2=40, minRadius=max(4, int(side * 0.014)), maxRadius=int(side * 0.05))
    yy, xx = np.mgrid[:h, :w]
    spheres = [(float(x), float(y), float(r)) for x, y, r in (c[0] if c is not None else [])
               if g[(xx - x) ** 2 + (yy - y) ** 2 < (0.6 * r) ** 2].mean() > 140]  # dark circles are not balls
    if not spheres:
        return [], ""
    P, V = [], []
    for x, y, r in spheres:
        m = (xx - x) ** 2 + (yy - y) ** 2 < (r - 1.5) ** 2
        P.append(np.stack([(xx[m] - x) / r, (yy[m] - y) / r], 1))
        V.append(g[m].astype(float))
    P, V = np.concatenate(P), np.concatenate(V)
    nb = 20
    best = None
    for fx in np.linspace(-0.9, 0.9, 37):  # grid search for the focal point
        for fy in np.linspace(-0.9, 0.9, 37):
            if fx * fx + fy * fy > 0.85 ** 2:  # outside the circle it renders solid
                continue
            bi = np.minimum((grad_t(P[:, 0], P[:, 1], fx, fy) * nb).astype(int), nb - 1)
            cnt = np.bincount(bi, minlength=nb)
            means = np.bincount(bi, V, nb) / np.maximum(cnt, 1)
            err = np.abs(means[bi] - V).mean()
            if best is None or err < best[0]:
                best = (err, fx, fy, means, cnt)
    _, fx, fy, means, cnt = best
    means = np.interp(np.arange(nb), np.flatnonzero(cnt), means[cnt > 0])  # fill empty bins
    stops = "".join(f'<stop offset="{(i + 0.5) / nb:.3f}" stop-color="{gray(v)}"/>' for i, v in enumerate(means))
    grad = f'<radialGradient id="sphere" fx="{0.5 + fx / 2:.3f}" fy="{0.5 + fy / 2:.3f}">{stops}</radialGradient>'
    return spheres, grad


def gray(v):
    v = int(round(v))
    return f"#{v:02x}{v:02x}{v:02x}"


def convert(src, dst, levels=20, blur=1.0, spheres_on=False, preview=False):
    img = Image.open(src).convert("RGBA")
    rgba = np.asarray(img)
    transparent, bg = rasterless.detect_background(rgba)
    flat = np.asarray(Image.alpha_composite(Image.new("RGBA", img.size, "white"), img).convert("RGB")).astype(float)
    alpha = rgba[..., 3] > 127
    art = alpha if transparent else np.abs(flat - bg).max(-1) > BG_DIST
    if art.any() and (flat.max(-1) - flat.min(-1))[art].mean() > COLOR_LIMIT:
        print(f"{src}: skipped, color image. rasterless_shaded handles grayscale art; use rasterless.py")
        return
    g = flat.mean(-1).astype(np.uint8)
    h, w = g.shape
    k = max(h, w) / REF
    scale = int(min(4, max(2, np.ceil(1024 / max(h, w)))))

    spheres, grad = find_spheres(g) if spheres_on else ([], "")
    s = g
    for _ in range(6):  # edge-preserving smoothing
        s = cv2.bilateralFilter(s, 0, 15, max(2.0, 8 * k))
    s = s.astype(float)
    yy, xx = np.mgrid[:h, :w]
    for x, y, r in spheres:  # balls are drawn on top, skip their bands
        s[(xx - x) ** 2 + (yy - y) ** 2 < (r - 2) ** 2] = 200
    s = cv2.resize(s, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)

    if transparent:
        fg = cv2.resize(alpha.astype(np.uint8), None, fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR) > 0
    else:
        fg = np.abs(s - float(np.mean(bg))) > BG_DIST
        # seal thin gaps so they don't trace as holes
        seal = max(1, round(5 * k * scale))
        fg = ndimage.binary_closing(fg, structure=ndimage.generate_binary_structure(2, 1), iterations=seal)
    fg = ndimage.binary_opening(ndimage.binary_fill_holes(fg), iterations=2)
    if not fg.any():
        print(f"{src}: skipped, no artwork found")
        return

    # thin bright lines stay sharp on top
    r = max(3, round(10 * k * scale))
    th = cv2.morphologyEx(s.astype(np.float32), cv2.MORPH_TOPHAT, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1,) * 2))
    light = ndimage.binary_opening(fg & (s > 150) & (th > 60), iterations=1)

    lo, hi = np.percentile(s[fg], [0.5, 99.5])
    band = np.digitize(s, np.linspace(lo, hi, levels + 1)[1:-1])
    # bleed past the outline so the blur has no fringe
    ext = ndimage.binary_dilation(fg, iterations=int(3 * blur * scale) + 2)
    _, (iy, ix) = ndimage.distance_transform_edt(~fg, return_indices=True)
    band = np.where(fg, band, band[iy, ix])

    def trace(m):
        return rasterless.trace(m, scale, 0, SPECK)[0]

    layers = []
    for b in range(band.max(), -1, -1):  # lightest first; the first is the silhouette
        sel = fg & (band == b)
        if sel.any():
            m = ext if not layers else ext & (band <= b)
            layers.append(f'<path fill="{gray(np.median(s[sel]))}" d="{trace(m)}"/>')
    soft = f'<filter id="soft" x="-5%" y="-5%" width="110%" height="110%"><feGaussianBlur stdDeviation="{blur:g}"/></filter>'
    body = "".join(layers)
    if blur > 0:
        body = f'<g clip-path="url(#outline)"><g filter="url(#soft)">{body}</g></g>'
    else:
        soft = ""
    top = f'<path fill="{gray(np.median(s[light]))}" d="{trace(light)}"/>' if light.any() else ""
    balls = "".join(f'<circle cx="{x:g}" cy="{y:g}" r="{r:g}"/>' for x, y, r in spheres)
    balls = f'<g fill="url(#sphere)">{balls}</g>' if balls else ""
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}">'
           f'<defs>{grad}<clipPath id="outline"><path d="{trace(fg)}"/></clipPath>{soft}</defs>'
           f'{body}{top}{balls}</svg>\n')
    Path(dst).write_text(svg)

    backdrop = (128, 128, 128) if transparent else tuple(int(v) for v in bg)
    check = rasterless.verify_note(img, svg, dst, preview, backdrop)
    print(f"{dst}: shaded, {len(layers)} gray bands" + (f", {len(spheres)} spheres" if spheres_on else "")
          + f", {len(svg.encode()) / 1024:.1f} KB, {check}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("-o", "--out")
    ap.add_argument("--levels", type=int, default=20)
    ap.add_argument("--blur", type=float, default=1.0)
    ap.add_argument("--spheres", action="store_true")
    ap.add_argument("--preview", action="store_true")
    args = ap.parse_args()
    out_dir = None
    if args.out and (len(args.inputs) > 1 or Path(args.out).is_dir() or args.out.endswith("/")):
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
    for f in args.inputs:
        dst = out_dir / (Path(f).stem + ".svg") if out_dir else (args.out or Path(f).with_suffix(".svg"))
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        convert(f, dst, args.levels, args.blur, args.spheres, args.preview)


if __name__ == "__main__":
    main()
