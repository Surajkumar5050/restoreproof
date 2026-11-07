"""Rebuild the README art in assets/ from two public-domain images in The Met's Open Access collection.

    python scripts/build_readme_art.py MET_DIR

MET_DIR holds the originals, downloaded unchanged:
  hermes.jpg  Marble statue of Hermes, after Polykleitos, 1st-2nd c. CE. The Met 56.234.15 (object 254925),
              https://images.metmuseum.org/CRDImages/gr/original/DP253642.jpg
  krater.jpg  Terracotta bell-krater, the Return of Persephone, Persephone Painter, ca. 440 BCE.
              The Met 28.57.23 (object 252973), https://images.metmuseum.org/CRDImages/gr/original/DP116943.jpg
Both are CC0 (Open Access). Needs Pillow and numpy.

Writes assets/art/bust.png (the duotone bust, transparent), assets/art/hero-bg.jpg (sun, bust, glitch,
grain baked together) and assets/art/krater.jpg (the scene band, toned). The animated SVGs that use
them are built by scripts/build_readme_svgs.py.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "art"

INK = (8, 8, 15)
SUN = (214, 52, 40)
GOLD = (255, 210, 31)
BLUE = (31, 75, 255)
CORNSILK = (255, 244, 218)

# Crop boxes as fractions of the original image (left, top, right, bottom).
BUST_BOX = (0.285, 0.062, 0.705, 0.292)  # head and shoulders only
KRATER_BOX = (0.262, 0.335, 0.738, 0.664)  # the painted scene band


def ramp(v: np.ndarray, stops: list[tuple[float, tuple[int, int, int]]]) -> np.ndarray:
    out = np.zeros(v.shape + (3,), dtype=np.float32)
    for c in range(3):
        out[..., c] = np.interp(v, [s for s, _ in stops], [col[c] for _, col in stops])
    return out


def duotone_bust(src: Path) -> Image.Image:
    im = Image.open(src).convert("RGB")
    w, h = im.size
    im = im.crop(tuple(int(f * d) for f, d in zip(BUST_BOX, (w, h, w, h))))
    lum = np.asarray(im.convert("L"), dtype=np.float32)
    # The museum backdrop is a mid-grey gradient; sample its edges as the black point.
    edge = np.concatenate([lum[:, :12].ravel(), lum[:, -12:].ravel(), lum[:12, :].ravel()])
    black = float(np.percentile(edge, 85)) + 6
    white = float(np.percentile(lum, 99.5))
    v = np.clip((lum - black) / max(white - black, 1), 0, 1) ** 0.85
    rgb = ramp(v, [(0.0, (40, 26, 14)), (0.35, (132, 86, 38)), (0.7, (233, 196, 128)), (1.0, CORNSILK)])
    alpha = np.clip((v - 0.03) / 0.22, 0, 1) * 255
    out = np.dstack([rgb, alpha]).astype(np.uint8)
    return Image.fromarray(out, "RGBA")


def hero_background(bust: Image.Image, size=(1280, 640), seed=7) -> Image.Image:
    rng = random.Random(seed)
    W, H = size
    canvas = Image.new("RGB", size, INK)
    draw = ImageDraw.Draw(canvas)
    cx, cy, r = 948, 292, 232
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=SUN)

    target_h = 612
    b = bust.resize((int(bust.width * target_h / bust.height), target_h), Image.LANCZOS)
    bx, by = cx - b.width // 2 + 6, H - target_h + 8

    # Glitch: a few horizontal slices of the bust shifted sideways, one tinted blue, one gold.
    slices = sorted(rng.sample(range(40, target_h - 60), 6))
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    layer.alpha_composite(b, (bx, by))
    for i, y0 in enumerate(slices):
        hgt = rng.randint(6, 22)
        strip = b.crop((0, y0, b.width, y0 + hgt))
        dx = rng.choice([-1, 1]) * rng.randint(14, 46)
        if i == 1 or i == 4:
            tint = BLUE if i == 1 else GOLD
            arr = np.asarray(strip).copy()
            mask = arr[..., 3] > 40
            arr[mask, :3] = (np.asarray(tint) * 0.85 + arr[mask, :3] * 0.15).astype(np.uint8)
            strip = Image.fromarray(arr, "RGBA")
        layer.alpha_composite(strip, (bx + dx, by + y0))
    canvas.paste(layer, (0, 0), layer)

    # Pixel blocks around the edges of the figure, in the marketplace's gold and blue.
    for _ in range(46):
        s = rng.choice([8, 10, 12, 16])
        x = rng.randint(bx - 70, bx + b.width + 40)
        y = rng.randint(70, H - 30)
        col = GOLD if rng.random() < 0.55 else BLUE
        if rng.random() < 0.3:
            draw.rectangle((x, y, x + s, y + s), outline=col, width=2)
        else:
            draw.rectangle((x, y, x + s, y + s), fill=col)
    # A small grid of outlined cells, like a data table being rebuilt.
    gx, gy = 1104, 470
    for row in range(3):
        for col in range(5):
            x, y = gx + col * 24, gy + row * 24
            filled = rng.random() < 0.18
            draw.rectangle((x, y, x + 18, y + 18), outline=BLUE, width=2, fill=GOLD if filled else None)

    # Film grain.
    noise = np.random.default_rng(seed).normal(0, 9, (H, W, 1))
    arr = np.clip(np.asarray(canvas, dtype=np.float32) + noise, 0, 255).astype(np.uint8)
    return Image.fromarray(arr, "RGB")


def krater_band(src: Path) -> Image.Image:
    im = Image.open(src).convert("RGB")
    w, h = im.size
    im = im.crop(tuple(int(f * d) for f, d in zip(KRATER_BOX, (w, h, w, h))))
    im.thumbnail((1600, 1600), Image.LANCZOS)
    # Where the bowl curves inward, the museum's pale backdrop shows in the bottom corners.
    # Flood-fill only that backdrop with the bowl's own black; the painting is untouched.
    for corner in ((2, im.height - 3), (im.width - 3, im.height - 3)):
        if sum(im.getpixel(corner)) > 600:
            ImageDraw.floodfill(im, corner, (14, 11, 10), thresh=60)
    return im.filter(ImageFilter.UnsharpMask(radius=1.2, percent=60, threshold=2))


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    met = Path(sys.argv[1])
    OUT.mkdir(parents=True, exist_ok=True)
    bust = duotone_bust(met / "hermes.jpg")
    bust.save(OUT / "bust.png", optimize=True)
    hero_background(bust).save(OUT / "hero-bg.jpg", quality=86, optimize=True, progressive=True)
    krater_band(met / "krater.jpg").save(OUT / "krater.jpg", quality=86, optimize=True, progressive=True)
    for f in sorted(OUT.iterdir()):
        print(f"{f.relative_to(ROOT)}  {f.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
