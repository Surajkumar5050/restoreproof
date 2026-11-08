"""Build RestoreProof's halftone art in the Hermes marketplace style: lit marble in pale dots on
an electric-blue ground, cut with a few glitch bands.

    python scripts/build_halftone_art.py MET_DIR

MET_DIR holds hermes.jpg, the CC0 "Marble statue of Hermes" from The Met (object 254925,
https://images.metmuseum.org/CRDImages/gr/original/DP253642.jpg), downloaded unchanged.
Needs Pillow and numpy. Colors come from engine/palette.py (HALFTONE). Writes:

  engine/report_art.jpg   the evidence report's masthead (head and neck)
  assets/banner.jpg       2:1 cover for the README and the plugin catalog
  desktop/plugin.js       its EMBLEM constant: the masthead cropped to the face, inlined
"""

from __future__ import annotations

import base64
import io
import math
import re
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from engine.palette import HALFTONE  # noqa: E402

BUST_BOX = (0.355, 0.055, 0.650, 0.245)  # head and neck, as fractions of the original
TITLE_FONTS = (r"C:\Windows\Fonts\FRAHV.TTF", r"C:\Windows\Fonts\impact.ttf",
               "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
BODY_FONTS = (r"C:\Windows\Fonts\consolab.ttf", r"C:\Windows\Fonts\cour.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf")


def rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _blur(x: np.ndarray, radius: float) -> np.ndarray:
    g = Image.fromarray((np.clip(x, 0, 1) * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(radius))
    return np.asarray(g, dtype=np.float32) / 255


def statue_tone(src: Path, height: int) -> Image.Image:
    """Grayscale bust, lit marble on a dark ground: contrast is measured on the figure alone and
    local contrast is lifted so the eyes, nose and curls survive the dot screen."""
    im = Image.open(src).convert("L")
    w, h = im.size
    im = im.crop(tuple(int(f * d) for f, d in zip(BUST_BOX, (w, h, w, h))))
    im = im.resize((round(im.width * height / im.height), height), Image.LANCZOS)
    a = np.asarray(im, dtype=np.float32)
    edge = np.concatenate([a[:, :10].ravel(), a[:, -10:].ravel(), a[:10, :].ravel()])
    bg = float(np.percentile(edge, 85))
    figure = np.clip((a - bg - 4) / 24, 0, 1)  # 0 = museum backdrop, 1 = marble
    inside = a[figure > 0.9]
    lo, hi = (float(np.percentile(inside, 1)), float(np.percentile(inside, 99.5))) if inside.size else (0.0, 255.0)
    tone = np.clip((a - lo) / max(hi - lo, 1), 0, 1)
    radius = height / 27
    tone = _blur(np.clip(tone + 1.2 * (tone - _blur(tone, radius)), 0, 1), height / 650)
    out = figure * (0.10 + 0.90 * tone ** 1.1) + (1 - figure) * 0.24  # backdrop: the blue ground
    return Image.fromarray((np.clip(out, 0, 1) * 255).astype(np.uint8), "L")


def halftone(gray: Image.Image, cell: int, angle: float = 45) -> Image.Image:
    """Classic rotated dot screen: dark areas get big dots, light areas small or none."""
    w, h = gray.size
    border = int(np.median(np.asarray(gray)[:4]))  # fill the rotated corners with the backdrop, so no seam shows
    big = gray.rotate(angle, expand=True, fillcolor=border)
    bw, bh = big.size
    src = np.asarray(big, dtype=np.float32) / 255.0
    canvas = Image.new("L", (bw, bh), 0)  # 0 = paper, 255 = ink, as a mask
    d = ImageDraw.Draw(canvas)
    for y in range(0, bh, cell):
        for x in range(0, bw, cell):
            dark = 1.0 - float(src[y:y + cell, x:x + cell].mean())
            if dark < 0.04:
                continue
            r = cell * 0.62 * math.sqrt(dark)
            cx, cy = x + cell / 2, y + cell / 2
            d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=255)
    back = canvas.rotate(-angle, expand=True, fillcolor=0, resample=Image.BICUBIC)
    x0, y0 = (back.width - w) // 2, (back.height - h) // 2
    return back.crop((x0, y0, x0 + w, y0 + h))


def colorize(mask: Image.Image) -> Image.Image:
    paper = Image.new("RGB", mask.size, rgb(HALFTONE["paper"]))
    ink = Image.new("RGB", mask.size, rgb(HALFTONE["dots"]))
    return Image.composite(ink, paper, mask)


def glitch(im: Image.Image, bands: list[tuple[float, float, int]], bars: list[tuple[float, float, float, float]]):
    """Shift horizontal bands sideways and lay thin paper bars across: a misregistered, scanned look."""
    out = im.copy()
    w, h = im.size
    for top, height, dx in bands:
        y0, y1 = int(top * h), int((top + height) * h)
        strip = im.crop((0, y0, w, y1))
        out.paste(strip, (dx, y0))
        if dx > 0:
            out.paste(im.crop((w - dx, y0, w, y1)), (0, y0))
        elif dx < 0:
            out.paste(im.crop((0, y0, -dx, y1)), (w + dx, y0))
    d = ImageDraw.Draw(out)
    for x, y, bw, bh in bars:
        d.rectangle((int(x * w), int(y * h), int((x + bw) * w), int((y + bh) * h)), fill=rgb(HALFTONE["paper"]))
    return out


def font(paths, size):
    for p in paths:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def masthead(src: Path) -> Image.Image:
    h = 450  # drawn at 3x; shown at 150px, so the dots stay crisp on high-density screens
    art = colorize(halftone(statue_tone(src, h), cell=4))
    return glitch(art, [(0.30, 0.05, 12), (0.62, 0.03, -9)], [(0.0, 0.74, 0.20, 0.010), (0.70, 0.20, 0.30, 0.008)])


def cover(src: Path) -> Image.Image:
    W, H = 1600, 800
    panel = 680  # every word sits left of here; the bust never sits under text
    canvas = Image.new("RGB", (W, H), rgb(HALFTONE["ground"]))
    art = colorize(halftone(statue_tone(src, H), cell=5))
    art = glitch(art, [(0.22, 0.04, 20), (0.47, 0.022, -14), (0.71, 0.045, 26)],
                 [(0.04, 0.60, 0.30, 0.006), (0.60, 0.15, 0.40, 0.005), (0.12, 0.90, 0.36, 0.005)])
    canvas.paste(art, (W - art.width, 0))
    d = ImageDraw.Draw(canvas)
    d.rectangle((0, 0, panel, H), fill=rgb(HALFTONE["ground"]))  # clean field behind the words
    ink, ink2 = rgb(HALFTONE["ink"]), rgb(HALFTONE["ink_2"])
    small = font(BODY_FONTS, 22)
    d.text((70, 78), "COMMUNITY PLUGIN · HERMES AGENT", font=small, fill=ink2)
    tag = font(BODY_FONTS, 27)
    d.text((70, 380), "Backups are claims.", font=tag, fill=ink)
    d.text((70, 416), "Drills are proof.", font=tag, fill=ink)
    title = font(TITLE_FONTS, 128)
    d.text((64, 470), "RESTORE", font=title, fill=ink)
    d.text((64, 606), "PROOF", font=title, fill=ink)
    right = max(d.textbbox((64, 470), "RESTORE", font=title)[2], d.textbbox((70, 78), "COMMUNITY PLUGIN · HERMES AGENT", font=small)[2])
    assert right < panel - 20, f"text overflows the panel ({right}px)"
    assert W - art.width >= panel - 140, "the bust reaches under the words"
    return canvas


def emblem(art: Image.Image) -> str:
    """The Desktop pane banner and drill-view badge: the masthead cropped around the face, as a data URL."""
    w, h = art.size
    band = round(w / 2.8)  # the pane shows it 2.8:1; the badge crops the same image square
    top = round(h * 0.40 - band / 2)
    buf = io.BytesIO()
    art.crop((0, top, w, top + band)).save(buf, "JPEG", quality=78, optimize=True, progressive=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def write_emblem(url: str) -> None:
    plugin = ROOT / "desktop" / "plugin.js"
    text = plugin.read_text(encoding="utf-8")
    new, n = re.subn(r"^const EMBLEM = '[^']*'$", f"const EMBLEM = '{url}'", text, flags=re.M)
    assert n == 1, "desktop/plugin.js needs exactly one `const EMBLEM = '...'` line"
    plugin.write_text(new, encoding="utf-8")


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    src = Path(sys.argv[1]) / "hermes.jpg"
    art = masthead(src)
    art.save(ROOT / "engine" / "report_art.jpg", quality=82, optimize=True, progressive=True)
    write_emblem(emblem(art))
    cover(src).save(ROOT / "assets" / "banner.jpg", quality=88, optimize=True, progressive=True)
    for f in (ROOT / "engine" / "report_art.jpg", ROOT / "assets" / "banner.jpg"):
        print(f"{f.relative_to(ROOT)}  {f.stat().st_size // 1024} KB  {Image.open(f).size}")


if __name__ == "__main__":
    main()
