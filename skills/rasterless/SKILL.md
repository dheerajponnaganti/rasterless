---
name: rasterless
description: Convert icons, logos, and other flat art (PNG, JPG, WebP) into clean, small SVG files by tracing them with Potrace. Use this whenever the user wants to vectorize, trace, or convert an icon, glyph, logo mark, emoji, favicon, app icon, UI symbol, or flat illustration into SVG, including requests like "make this png an svg", "I need this icon as a vector", "convert my icons folder to svg", "turn this flaticon download into svg", or "get me an svg version of this logo", even if they never say "trace" or "vectorize". Handles single-color glyphs, flat multi-color icons, and shaded grayscale 3D-style illustrations, single files or whole folders. Not for photos or full-color painted illustrations.
license: MIT (scripts/vendor/potrace is GPL-2.0-or-later)
---

# Rasterless

Converts flat icons into SVGs that look like a designer drew them: one path per color, sub-pixel accurate edges, a `viewBox`, and no bloat. The script does the tracing. Your job is to run it, look at the result, fix the rare miss with a flag, and deliver.

Two scripts:
- `rasterless.py` for flat icons (the default, covered first below).
- `rasterless_shaded.py` for shaded grayscale art: 3D-style illustrations, glossy spheres, soft shadows. See **Shaded art** below.

## How it works (so you can reason about failures)

1. **Background detection.** Transparent PNGs have no background. In opaque images (JPG), the corner color is the background (falling back to the border color when the corners disagree), and only the background area connected to the image edge is dropped. Enclosed areas of that color (white teeth in a smiley on a white JPG) stay as real shapes. The exception is one-color glyphs: there, enclosed areas stay holes, like the inside of a magnifier lens.
2. **Palette.** Colors are estimated from interior pixels, so anti-aliased edge blends never become fake colors. Near-identical colors (JPEG noise) are merged. Thin details that never form an interior (1px lines on small icons) are added when their pixels cannot be explained as a blend of two known colors. Flat areas drawn at partial opacity (duotone fills) become their own layer with `fill-opacity`. Up to 24 colors are kept by default. When an icon has more, the smallest colors are merged into their nearest kept color.
3. **Upscale and snap.** The image is upscaled (at most 4×, toward 1024px) and every pixel snaps to a palette color. Edge pixels go to the nearer end of the blend they belong to, so no sliver of a third color appears between two colors. Boundaries land between pixels, which recovers sub-pixel edges. JPEGs get a slight blur first so edges follow the shape, not the compression noise.
4. **Trace.** Each color is traced with Potrace. Layers stack largest-first, and each layer extends under the solid ones painted above it, so neighboring colors never show hairline gaps. See-through layers go at the bottom.
5. **Verify.** The SVG is rendered back and compared with the input on the input's own backdrop (gray for transparent inputs, so a wrong hole shows). Visible mismatches are counted and drawn in red on the preview.

## Workflow

### 1. Run the converter

```bash
python3 <skill-dir>/scripts/rasterless.py INPUT.png --preview
```

- Several files or a glob work: `rasterless.py icons/*.png -o svg/ --preview`. With `-o` pointing at a folder, every SVG goes there. Without `-o`, each SVG lands next to its input.
- `-o out.svg` names the output for a single input.
- `--preview` writes the preview PNGs to a temp folder and prints each path, so the output folder holds only the SVGs.
- In a terminal that has `uv` (Claude Code, Codex), run `uv run <skill-dir>/scripts/rasterless.py ...` instead of `python3`. The script declares its own dependencies, so `uv` installs them on first run.
- It runs in a terminal and in web chat sandboxes (Claude.ai, ChatGPT). It needs `pillow` and `numpy`. `rasterless_shaded.py` also needs `opencv-python-headless` and `scipy`. Web sandboxes usually have all four. If one is missing and the sandbox can install packages, `pip install` it.
- Potrace is bundled in `scripts/vendor`, so nothing else is required. `resvg-py` is optional. Without it the SVG is still written, but the report says `not verified` and no preview is made. Try `pip install resvg-py` once. If that fails, tell the user the visual check was skipped.
- In a web sandbox, write the SVGs to the sandbox's output folder (`/mnt/data` in ChatGPT, `/mnt/user-data/outputs` in Claude.ai) and give the user the download link.
- If the user gives a URL, download the image into its own folder first, then convert it.

Each file prints one line:

```
home.svg: mono [#000000], 63 segments, 1.8 KB, mismatch 0.00%, preview /tmp/.../home.preview.png
burger.svg: 11 colors [#efa335 #000000 ...], 810 segments, 21.8 KB, mismatch 0.00%, preview /tmp/.../burger.preview.png
bell.svg: mono [#000000@20% #000000], 102 segments, 3.0 KB, mismatch 0.00%, preview /tmp/.../bell.preview.png
```

### 2. Look at the preview

Open the printed preview image and look at it. It shows three panels: original | SVG render | mismatches in red. Transparent icons sit on gray, so holes and missing white parts are visible.

Look at it even when the mismatch is 0%. The number only counts clear misses, and it means nothing for gradient icons, where banding passes as a match. A color that came out slightly wrong, or a small detail that vanished, is something only your eyes catch. For a batch, check every preview that has a warning, a mismatch above 0.5%, or a color count that looks wrong for the icon.

### 3. Fix misses with flags

| What you see | Cause | Fix |
|---|---|---|
| Two shades merged into one, or a detail color missing | Palette detection merged colors or missed a tiny one | `--colors N` with the true color count |
| A tiny feature (mouth, iris) took a neighbor's color, and the report shows 24 colors with a `24+ colors` warning | The icon has more flat colors than the default cap | `--colors N` above 24 (for example 28) |
| Extra near-duplicate colors | Noise was read as a separate color | `--colors N` with the true color count |
| Rounded-square or full-bleed background disappeared from an app icon | Solid background was treated as backdrop | `--keep-bg` (paints it as the bottom layer) |
| A white part touching the image edge is missing on a white JPG | It is connected to the white background, so it reads as backdrop | Ask for a transparent PNG, or use `--keep-bg` if a white square is acceptable |
| `WARNING: gradient or shading detected` or `24+ colors` on grayscale art | The art is shaded, not flat | Switch to `rasterless_shaded.py` (see **Shaded art**) |
| The same warning on a full-color image | The icon is not flat | Tell the user it was approximated with flat color bands. For a well-known brand logo (Instagram, Google), point them to the official SVG from the brand's press or brand page instead |

Re-run with the flag, then look at the preview again.

### Shaded art

Use it when the image is grayscale with smooth shading, or when `rasterless.py` warned about gradients on such an image. Flat tracing breaks this kind of art into speckled bands, and thin light gaps that touch the page come out as see-through holes.

```bash
python3 <skill-dir>/scripts/rasterless_shaded.py INPUT.png --preview
```

How it works:
1. The image is smoothed with an edge-preserving filter, which removes paper grain and keeps edges.
2. The gray range is cut into bands (default 20). Each band layer covers everything darker than it, so gaps between layers are impossible. The bottom layer is the whole silhouette.
3. Thin light gaps that open onto a plain background (a fissure between two halves) are sealed into the silhouette.
4. The bands get a 1px SVG blur so the steps read as smooth shading. They are clipped to a crisp outline.
5. Thin bright details (lines, frames, chip pins) are traced as one sharp layer on top.
6. With `--spheres`, round glossy balls are detected and replaced by circles that use a fitted radial gradient.

Flags:
- `--spheres`: use it when the art has glossy balls or nodes. Check the preview: every ball should be covered, and nothing else should turn into a ball.
- `--levels N`: more bands (28 or 32) for visible steps in large smooth areas. Fewer (12) for a smaller file.
- `--blur S`: 0 gives hard bands (no filter, best for design-tool import). Raise it to 1.5 or 2 for softer shading at the cost of softer edges.

Color images are skipped with a message, so use `rasterless.py` for those. Expect 300 KB to 1 MB for a detailed 1000px illustration. Tell the user that the result is a smooth approximation, that the blur filter renders in all browsers but may import differently in design tools, and that a soft shadow ends in a faint visible edge on a non-white background.

### 4. Choose the output mode

- **Default (web/app code).** Has a `viewBox` and no fixed size, so it scales with CSS. A dark single-color icon gets `fill="currentColor"`, so `color: red` in CSS recolors it. This includes duotone icons, whose lighter layer keeps its `fill-opacity`. A colored mono icon (for example a red heart) keeps its color.
- **`--design`** (Figma, Illustrator, print). Exact hex colors everywhere, plus `width` and `height` in pixels.

Use the default unless the user mentions a design tool or needs exact colors.

### 5. Deliver

Tell the user the output path, color count and size, and anything you noticed in the preview. Mention a warning if one fired. Do not paste the SVG path data into chat; it is long and useless to read.

When it fits the user's setup, add one line of usage advice:
- Web mono icon: CSS `color` recolors it.
- React: import with SVGR, or use `<img src>`. Pasting the markup inline into JSX breaks on `fill-rule`, which JSX spells `fillRule`.
- Downloaded icons (Flaticon, Icons8): their free licenses usually require attribution.

## Limits

- `rasterless.py` is built for flat icons: solid colors, crisp edges. Heavy gradients produce banded approximations, and the warning catches these.
- `rasterless_shaded.py` covers grayscale shading only. Tinted one-hue art counts as color and is skipped. Photos and full-color painted illustrations have no good automatic vector form. Offer a hand redraw with real SVG gradients instead.
- `--spheres` finds balls of about 1.5% to 5% of the image width. It can miss them on small inputs (300px and below).
- Tiny inputs (16 to 32px): single-color and simple icons trace well. Detailed multi-color icons lose thin parts (a 1px stripe blurs into its neighbors), because the pixels no longer carry that information. When the preview shows this, ask the user for a larger source (64px or more). Flaticon and Icons8 both offer 512px downloads.
- Output colors are measured from the image, so a JPG may give `#fecb4c` where the designer used `#ffcc4d`. Snap to known brand colors by hand if the user cares.
