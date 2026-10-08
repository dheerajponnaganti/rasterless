#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pillow", "numpy", "potracer", "resvg-py"]
# ///
"""Convert a flat icon (PNG/JPG/WebP) to a clean SVG.

usage: rasterless.py IN [IN ...] [-o OUT] [--colors N] [--design] [--keep-bg] [--preview]

  -o OUT       output file, or a folder (required form for several inputs); default: next to IN
  --colors N   force exactly N icon colors instead of auto-detecting
  --design     exact colors + width/height (for Figma/Illustrator); default is web-ready
  --keep-bg    keep a solid background instead of dropping it
  --preview    also write a preview PNG (original | SVG render | mismatches in red) to a temp folder
"""
import argparse
import io
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

try:
    import potrace
except ImportError:  # no pip in web sandboxes: use the bundled copy
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent / "vendor"))
    import potrace
try:
    import resvg_py
except ImportError:  # optional: only needed for the check
    resvg_py = None

WORK_SIZE = 1024   # upscale target, for sub-pixel edges
MERGE_DIST = 20    # same-color distance, lossy inputs
MERGE_CLEAN = 12   # same, clean inputs above SMALL_SIDE
MIN_SHARE = 0.001  # min share of flat pixels per color
MAX_COLORS = 24    # past this, the smallest colors merge
GROW_DIST = 24     # unexplained-pixel distance
GROW_DIST_JPEG = 0  # off: JPEG bleed fakes colors
GROW_DIST_WEBP = 40  # WebP bleeds less
JPEG_BLUR = 0.4   # pre-snap blur, in source pixels
WEBP_BLUR = 0.0   # same for lossy WebP
GRID = 1024      # coordinate steps across the icon
SPECK = 2.0       # min shape area, in source pixels
MODE_CLEAN = 5    # mode filter size, removes slivers
MODE_LOSSY = 5    # same for JPEG/lossy WebP
SMALL_SIDE = 64   # gentler cleanup at or below this size
MODE_SMALL = 3
SPECK_SMALL = 0.5
MAX_UPSCALE = 4   # more adds nodes, not accuracy
OPT_TOL = 0.2     # potrace curve tolerance


def load(path):
    im = Image.open(path)
    lossy = "jpeg" if im.format == "JPEG" else "webp" if im.format == "WEBP" and _webp_lossy(Path(path).read_bytes()) else None
    return im.convert("RGBA"), lossy


def _webp_lossy(data):
    """Lossy WebP has a 'VP8 ' chunk, lossless 'VP8L'."""
    pos = 12
    while pos + 8 <= len(data):
        tag, size = data[pos:pos + 4], int.from_bytes(data[pos + 4:pos + 8], "little")
        if tag in (b"VP8 ", b"VP8L"):
            return tag == b"VP8 "
        pos += 8 + size + (size & 1)
    return False


def detect_background(rgba):
    """Return (transparent, bg_rgb)."""
    a = rgba[..., 3]
    border = np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]])
    if (border < 250).mean() > 0.5 or (a < 250).mean() > 0.05:
        return True, None
    rgb = rgba[..., :3].astype(np.float32)
    # corners first, then the border
    k = max(1, min(rgb.shape[:2]) // 32)
    corners = [rgb[:k, :k], rgb[:k, -k:], rgb[-k:, :k], rgb[-k:, -k:]]
    corners = [c.reshape(-1, 3).mean(0) for c in corners]
    for c in corners:
        agree = [o for o in corners if np.linalg.norm(o - c) < 30]
        if len(agree) >= 3:
            return False, np.mean(agree, axis=0)
    edge = np.concatenate([rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]])
    return False, np.median(edge, axis=0)


def estimate_palette(rgba, transparent, bg, n_colors=None, lossy=None):
    """Flat colors from interior pixels."""
    small = Image.fromarray(rgba)
    small.thumbnail((512, 512), Image.LANCZOS)
    px = np.asarray(small).astype(np.float32)
    rgb, a = px[..., :3], px[..., 3]
    # flat: the 3x3 neighborhood barely varies
    pad = np.pad(rgb, ((1, 1), (1, 1), (0, 0)), mode="edge")
    win = np.lib.stride_tricks.sliding_window_view(pad, (3, 3), axis=(0, 1))
    flat = (win.max(axis=(-1, -2)) - win.min(axis=(-1, -2))).max(axis=-1) < 24
    opaque = a >= 250 if transparent else np.ones(a.shape, bool)
    pts = rgb[flat & opaque]
    merge = merge_dist(lossy, max(rgba.shape[:2]))
    # shares count icon pixels only; bg stays in pts to absorb halos
    icon_n = len(pts) if bg is None else int((((pts - bg) ** 2).sum(-1) >= merge ** 2).sum())
    if len(pts) < 16:  # nothing flat: use all solid pixels
        pts = rgb[a >= 128] if transparent else rgb.reshape(-1, 3)

    if n_colors:  # forced count: plain k-means
        centers = _peaks(pts, merge=0)[:n_colors]
    else:
        centers, counts = _peaks(pts, merge=merge, min_count=MIN_SHARE * icon_n, with_counts=True,
                                 step=8 if merge < MERGE_DIST else 16)
        if bg is not None:
            # drop backdrop halos
            centers = _drop_bg_halos(centers, counts, bg, MIN_SHARE * len(pts))
    # k-means on nearby pixels only, so shades don't drift
    for _ in range(4):
        idx = _nearest(pts, centers)
        near = ((pts - centers[idx]) ** 2).sum(-1) < (MERGE_DIST / 2) ** 2
        centers = np.array([pts[(idx == k) & near].mean(0) if ((idx == k) & near).any() else c
                            for k, c in enumerate(centers)])
    if not n_colors:  # merge peaks that converged
        merged = []
        for c in centers:
            if all(np.linalg.norm(c - o) >= merge for o in merged):
                merged.append(c)
        centers = np.array(merged)
        grow = {"jpeg": GROW_DIST_JPEG, "webp": GROW_DIST_WEBP}.get(lossy, GROW_DIST)
        if grow:
            centers = _grow(rgb[opaque], centers, bg, grow)
    # unexplained share: high means gradients
    d = np.min([((pts - c) ** 2).sum(-1) for c in centers], axis=0)
    if len(pts) < 500:  # too few interior pixels to judge
        return centers, 0.0
    return centers, (d > 6 ** 2).mean()  # flat < 16%, gradients > 45%


def merge_dist(lossy, side):
    """Tight merging only for large clean inputs."""
    return MERGE_CLEAN if not lossy and side > SMALL_SIDE else MERGE_DIST


def _drop_bg_halos(centers, counts, bg, min_count):
    keep = []
    for c, count in zip(centers, counts):
        if count < min_count and keep:
            seg_d = []
            for o in keep:
                seg = o - bg
                t = np.clip((c - bg) @ seg / max(seg @ seg, 1e-6), 0, 1)
                seg_d.append(np.linalg.norm(c - bg - t * seg))
            if min(seg_d) < 16:
                continue
        keep.append(c)
    return np.array(keep)


def semi_levels(rgba):
    """Flat semi-transparent fills as (rgb, alpha)."""
    small = Image.fromarray(rgba)
    small.thumbnail((512, 512), Image.LANCZOS)
    px = np.asarray(small).astype(np.float32)
    pad = np.pad(px, ((1, 1), (1, 1), (0, 0)), mode="edge")
    win = np.lib.stride_tricks.sliding_window_view(pad, (3, 3), axis=(0, 1))
    rng = win.max(axis=(-1, -2)) - win.min(axis=(-1, -2))
    a = px[..., 3]
    semi = (rng[..., :3].max(-1) < 24) & (rng[..., 3] < 12) & (a >= 20) & (a < 235)
    pts = px[semi]
    need = max(50, 0.005 * (a >= 20).sum())
    levels = []
    while len(pts) >= need and len(levels) < 3:
        bins = (pts[:, 3] // 24).astype(int)
        top = np.bincount(bins).argmax()
        mid = np.median(pts[bins == top, 3])
        grp = pts[np.abs(pts[:, 3] - mid) <= 16]
        if len(grp) < need:
            break
        levels.append((np.median(grp[:, :3], axis=0), float(np.median(grp[:, 3]))))
        pts = pts[np.abs(pts[:, 3] - mid) > 16]
    return levels


def _blend_dist(pts, centers):
    """Distance to the nearest color or two-color blend, and its label."""
    best = np.full(len(pts), np.inf, np.float32)
    lab = np.zeros(len(pts), np.int32)
    for i, c in enumerate(centers):
        d = np.sqrt(((pts - c) ** 2).sum(-1))
        better = d < best
        best[better], lab[better] = d[better], i
    for i in range(len(centers)):
        for j in range(i + 1, len(centers)):
            seg = centers[j] - centers[i]
            t = np.clip(((pts - centers[i]) @ seg) / max(seg @ seg, 1e-6), 0, 1)
            d = np.sqrt(((pts - centers[i] - t[:, None] * seg) ** 2).sum(-1))
            better = d < best
            best[better] = d[better]
            lab[better] = np.where(t[better] < 0.5, i, j)
    return best, lab


def _grow(pts, centers, bg, dist):
    """Add colors that are neither a palette color nor a blend (thin details)."""
    if len(pts) > 60000:
        pts = pts[np.random.default_rng(0).choice(len(pts), 60000, replace=False)]
    centers = list(centers)
    anchors = centers + ([bg] if bg is not None else [])
    need = max(4, 0.003 * len(pts))
    while len(centers) < MAX_COLORS + 1:
        d, _ = _blend_dist(pts, np.array(anchors))
        resid = pts[d > dist]
        if len(resid) < need:
            break
        c = _peaks(resid, merge=0, min_share=0)[0]
        near = resid[((resid - c) ** 2).sum(-1) < dist ** 2]
        if len(near) < need:
            break
        c = near.mean(0)
        centers.append(c)
        anchors.insert(len(centers) - 1, c)
    return np.array(centers)


def _peaks(pts, merge, min_share=MIN_SHARE, min_count=None, with_counts=False, step=16):
    q = (pts // step).astype(np.int32)  # step 8 separates near-whites
    n = 256 // step
    keys = (q[:, 0] * n + q[:, 1]) * n + q[:, 2]
    uniq, inv, counts = np.unique(keys, return_inverse=True, return_counts=True)
    need = min_share * len(pts) if min_count is None else min_count
    chosen, sizes = [], []
    for b in np.argsort(-counts):
        if counts[b] < need and chosen:
            break
        c = pts[inv == b].mean(0)
        if all(np.linalg.norm(c - o) >= merge for o in chosen):
            chosen.append(c)
            sizes.append(counts[b])
    return (np.array(chosen), sizes) if with_counts else np.array(chosen)


def _nearest(pts, centers):
    best = np.full(len(pts), np.inf, np.float32)
    idx = np.zeros(len(pts), np.int32)
    for k, c in enumerate(centers):
        d = ((pts - c) ** 2).sum(-1)
        better = d < best
        best[better], idx[better] = d[better], k
    return idx


def label_pixels(img, palette, transparent, bg, lossy=None, semi=()):
    """Snap upscaled pixels to palette indices; -1 is background."""
    w, h = img.size
    scale = min(WORK_SIZE / max(w, h), MAX_UPSCALE)
    big = img.resize((round(w * scale), round(h * scale)), Image.LANCZOS)
    blur = {"jpeg": JPEG_BLUR, "webp": WEBP_BLUR}.get(lossy, 0)
    if blur:
        big = big.filter(ImageFilter.GaussianBlur(blur * scale))
    big = np.asarray(big).astype(np.float32)
    rgb, a = big[..., :3].reshape(-1, 3), big[..., 3].reshape(-1)
    centers = palette if bg is None else np.vstack([palette, bg])
    idx = _nearest(rgb, centers)
    # edge blends go to the nearer end, never a third color
    far = np.sqrt(((rgb - centers[idx]) ** 2).sum(-1)) > 12
    if far.any() and len(centers) > 1:
        idx[far] = _blend_dist(rgb[far], centers)[1]
    if bg is not None:
        idx[idx == len(palette)] = -1
    if transparent and semi:
        # nearest opacity level
        levels = np.array([0.0] + [s[1] for s in semi] + [255.0])
        lvl = np.abs(a[:, None] - levels[None]).argmin(1)
        idx[lvl == 0] = -1
        for j, (c, _) in enumerate(semi):
            idx[lvl == j + 1] = len(palette) + (0 if bg is None else 1) + j
    elif transparent:
        idx[a < 128] = -1
    lab = idx.reshape(big.shape[:2])
    # mode filter removes noise and slivers
    size = int(MODE_SMALL if max(w, h) <= SMALL_SIDE else MODE_LOSSY if lossy else MODE_CLEAN)
    if size > 1:
        lab = np.asarray(Image.fromarray((lab + 1).astype(np.uint8)).filter(ImageFilter.ModeFilter(size))).astype(np.int32) - 1
    if bg is not None:  # close the gap left by the bg slot
        lab[lab > len(palette)] -= 1
    return lab, scale


def border_connected(mask):
    """Mask pixels connected to the image border."""
    m = Image.fromarray(np.pad(mask, 1, constant_values=True).astype(np.uint8) * 255).copy()
    ImageDraw.floodfill(m, (0, 0), 128)
    return (np.asarray(m) == 128)[1:-1, 1:-1]


def trace(mask, scale, decimals, speck):
    """Trace a mask into SVG path data."""
    # potracer treats False as ink, hence ~mask
    args = dict(turdsize=max(2, int(scale * scale * speck)), turnpolicy=potrace.POTRACE_TURNPOLICY_MINORITY,
                alphamax=1.0, opttolerance=OPT_TOL * max(1, scale / 2))
    try:
        plist = potrace.Bitmap(~mask).trace(opticurve=True, **args)
    except ValueError:  # potracer bug in curve merging
        plist = potrace.Bitmap(~mask).trace(opticurve=False, **args)
    cmds = []  # (command, numbers), relative
    cur = (0.0, 0.0)

    def pt(p):
        return (round(p.x / scale, decimals), round(p.y / scale, decimals))

    for curve in plist:
        start = pt(curve.start_point)
        cmds.append(("m", [start[0] - cur[0], start[1] - cur[1]]))
        cur = start
        for seg in curve.segments:
            if seg.is_corner:
                for q in (pt(seg.c), pt(seg.end_point)):
                    cmds.append(("l", [q[0] - cur[0], q[1] - cur[1]]))
                    cur = q
            else:  # relative to rounded absolutes: no drift
                c1, c2, e = pt(seg.c1), pt(seg.c2), pt(seg.end_point)
                cmds.append(("c", [v for q in (c1, c2, e) for v in (q[0] - cur[0], q[1] - cur[1])]))
                cur = e
        cmds.append(("z", []))
    return serialize(cmds, decimals), sum(len(c.segments) for c in plist)


def serialize(cmds, decimals):
    """Compact path data."""
    out, prev_cmd, prev = [], None, ""
    for c, vals in cmds:
        if c != prev_cmd or c in "mz":
            out.append(c)
            prev = c
        prev_cmd = c
        for v in vals:
            n = num(v, decimals)
            # separator only where numbers would merge
            if prev and prev[-1] not in "mlcz" and not n.startswith("-") and not (n.startswith(".") and "." in prev):
                out.append(" ")
            out.append(n)
            prev = n
    return "".join(out)


def num(v, decimals):
    s = f"{v:.{decimals}f}"
    if "." in s:  # keep "120" intact
        s = s.rstrip("0").rstrip(".")
    if s in ("-0", ""):
        return "0"
    return s.replace("0.", ".", 1) if s.startswith(("0.", "-0.")) else s


def hexc(c):
    return "#{:02x}{:02x}{:02x}".format(*(int(round(min(255, max(0, v)))) for v in c))


def is_dark_neutral(c):
    return max(c) < 90 and max(c) - min(c) < 30


def convert(src, dst, n_colors=None, design=False, keep_bg=False, preview=False):
    img, lossy = load(src)
    rgba = np.asarray(img)
    transparent, bg = detect_background(rgba)
    bg_color = bg
    if keep_bg:
        bg = None
    palette, unexplained = estimate_palette(rgba, transparent, bg, n_colors, lossy)
    if bg is not None:  # drop the background color
        palette = np.array([c for c in palette if np.linalg.norm(c - bg) >= merge_dist(lossy, max(rgba.shape[:2]))]) if len(palette) > 1 else palette
    warn = ""
    if unexplained > 0.3:
        warn = f"gradient or shading detected ({unexplained:.0%} of pixels between flat colors): approximated with flat bands"
    semi = semi_levels(rgba) if transparent else []
    lab, scale = label_pixels(img, palette, transparent, bg, lossy, semi)
    alpha = [255.0] * len(palette) + [s[1] for s in semi]
    if semi:
        palette = np.vstack([palette] + [s[0][None] for s in semi])
    if bg is not None:
        # border-connected bg is backdrop; enclosed bg is a shape
        enclosed = (lab == -1) & ~border_connected(lab == -1)
        if enclosed.any() and len(np.unique(lab[lab >= 0])) > 1:
            palette = np.vstack([palette, bg])
            alpha.append(255.0)
            lab[enclosed] = len(palette) - 1
    areas = [(lab == k).sum() for k in range(len(palette))]
    order = [k for k in np.argsort(areas)[::-1] if areas[k] > 0]
    cap = max(MAX_COLORS, n_colors or 0)  # --colors can exceed the cap
    if len(order) > cap:  # merge extras into the nearest kept color
        kept, dropped = order[:cap], order[cap:]
        for k in dropped:
            near = kept[int(np.argmin([np.linalg.norm(palette[k] - palette[j]) for j in kept]))]
            lab[lab == k] = near
        order = kept
    if len(order) >= cap and not n_colors:  # hitting the cap means not flat
        warn = f"{cap}+ colors: gradients or shading, approximated with flat bands"
    order = [k for k in order if alpha[k] < 250] + [k for k in order if alpha[k] >= 250]  # see-through layers go underneath
    if keep_bg and bg_color is not None:  # kept background goes at the bottom
        b = int(np.argmin([np.linalg.norm(c - bg_color) for c in palette]))
        order = [b] + [k for k in order if k != b]

    w, h = img.size
    decimals = max(0, int(np.ceil(np.log10(GRID / max(w, h)))))
    # mono: one color, maybe at several opacities
    mono = all(np.linalg.norm(palette[k] - palette[order[0]]) < MERGE_DIST for k in order)
    paths, nodes = [], 0
    for i, k in enumerate(order):
        # extend under later solid layers, so no gaps
        mask = (lab == k) | np.isin(lab, [j for j in order[i + 1:] if alpha[j] >= 250])
        d, n = trace(mask, scale, decimals, SPECK_SMALL if max(w, h) <= SMALL_SIDE else SPECK)
        nodes += n
        fill = "currentColor" if mono and not design and is_dark_neutral(palette[k]) else hexc(palette[k])
        op = f' fill-opacity="{num(alpha[k] / 255, 2)}"' if alpha[k] < 250 else ""
        paths.append(f'<path fill="{fill}"{op} fill-rule="evenodd" d="{d}"/>')

    size = f' width="{w}" height="{h}"' if design else ""
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}"{size}>{"".join(paths)}</svg>\n'
    Path(dst).write_text(svg)

    # gray backdrop for transparent inputs exposes holes
    backdrop = tuple(int(v) for v in bg_color) if bg_color is not None else (128, 128, 128)
    check = verify_note(img, svg, dst, preview, backdrop)
    kind = "mono" if mono else f"{len(order)} colors"
    cols = " ".join(hexc(palette[k]) + (f"@{alpha[k] / 255:.0%}" if alpha[k] < 250 else "") for k in order)
    print(f"{dst}: {kind} [{cols}], {nodes} segments, "
          f"{len(svg.encode()) / 1024:.1f} KB, {check}" + (f"  WARNING: {warn}" if warn else ""))


def verify_note(img, svg, dst, preview, backdrop):
    """Check, save the preview, return the report text."""
    if resvg_py is None:
        return "not verified (install resvg-py for the check and preview)"
    mismatch, pv = verify(img, svg, preview, backdrop)
    note = f"mismatch {mismatch:.2%}"
    if preview:
        pv_path = Path(tempfile.gettempdir()) / "rasterless-previews" / (Path(dst).stem + ".preview.png")
        pv_path.parent.mkdir(exist_ok=True)
        pv.save(pv_path)
        note += f", preview {pv_path}"
    return note


def verify(img, svg, want_preview, backdrop):
    """Render the SVG back and count visible misses."""
    w, h = img.size
    out = Image.open(io.BytesIO(bytes(resvg_py.svg_to_bytes(svg_string=svg, width=w, height=h)))).convert("RGBA")
    white = Image.new("RGBA", (w, h), backdrop + (255,))
    a = np.asarray(Image.alpha_composite(white, img)).astype(np.int16)[..., :3]
    b = np.asarray(Image.alpha_composite(white, out)).astype(np.int16)[..., :3]
    bad = np.abs(a - b).max(-1) > 64
    # ignore the 1px anti-aliasing fringe
    core = bad & (np.roll(bad, 1, 0) | np.roll(bad, -1, 0)) & (np.roll(bad, 1, 1) | np.roll(bad, -1, 1))
    pv = None
    if want_preview:
        diff = np.asarray(Image.alpha_composite(white, img).convert("L").convert("RGB")).copy()
        diff = (diff * 0.35 + 165).astype(np.uint8)
        diff[core] = (230, 30, 30)
        tiles = [Image.alpha_composite(white, img).convert("RGB"), Image.alpha_composite(white, out).convert("RGB"),
                 Image.fromarray(diff)]
        t = 320
        pv = Image.new("RGB", (t * 3 + 16, t), "#888")
        for i, tile in enumerate(tiles):
            pv.paste(tile.resize((t, round(t * h / w)), Image.NEAREST if w < t else Image.LANCZOS), (i * (t + 8), 0))
    return core.mean(), pv


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("-o", "--out")
    ap.add_argument("--colors", type=int)
    ap.add_argument("--design", action="store_true")
    ap.add_argument("--keep-bg", action="store_true")
    ap.add_argument("--preview", action="store_true")
    args = ap.parse_args()
    out_dir = None
    if args.out and (len(args.inputs) > 1 or Path(args.out).is_dir() or args.out.endswith("/")):
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
    for f in args.inputs:
        dst = out_dir / (Path(f).stem + ".svg") if out_dir else (args.out or Path(f).with_suffix(".svg"))
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        convert(f, dst, args.colors, args.design, args.keep_bg, args.preview)


if __name__ == "__main__":
    main()
