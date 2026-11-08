"""Build the README and catalog artwork in assets/.

    python scripts/build_readme_art.py MET_DIR

MET_DIR holds two public-domain originals from The Met's Open Access collection (CC0), unchanged:
  hermes.jpg  Marble statue of Hermes, after Polykleitos, 1st-2nd c. CE. The Met 56.234.15 (object 254925),
              https://images.metmuseum.org/CRDImages/gr/original/DP253642.jpg
  krater.jpg  Terracotta bell-krater, the Return of Persephone, Persephone Painter, ca. 440 BCE.
              The Met 28.57.23 (object 252973), https://images.metmuseum.org/CRDImages/gr/original/DP116943.jpg

Writes:
  assets/art/krater.jpg           the painted scene band, toned
  assets/story.png                what a backup status, a health check and a drill each report
  assets/pipeline.png             the six stages of a drill

The cover (assets/banner.jpg) is built by scripts/build_halftone_art.py.

Needs Pillow, Microsoft Edge or Chrome (headless, for type and line-art) and engine/report_fonts.css for
Courier Prime and JetBrains Mono. Deterministic:
no random placement, so a rebuild gives the same images.
"""

from __future__ import annotations

import base64
import io
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
ART = ASSETS / "art"

# The README panels' language: black ground, cream type, blueprint blue, yellow for the verdict.
BLACK, CREAM, BLUE, YELLOW, RED, DIM = "#0b0b0e", "#efe6d2", "#4d6bff", "#ffd21f", "#ff5a4e", "#8c8676"
KRATER_BOX = (0.262, 0.335, 0.738, 0.664)


# -- source treatments -----------------------------------------------------------------------------------


def krater_band(src: Path) -> Image.Image:
    im = Image.open(src).convert("RGB")
    w, h = im.size
    im = im.crop(tuple(int(f * d) for f, d in zip(KRATER_BOX, (w, h, w, h))))
    im.thumbnail((1600, 1600), Image.LANCZOS)
    # Where the bowl curves inward the museum's pale backdrop shows in the bottom corners;
    # fill only that backdrop with the bowl's own black. The painting is untouched.
    for corner in ((2, im.height - 3), (im.width - 3, im.height - 3)):
        if sum(im.getpixel(corner)) > 600:
            ImageDraw.floodfill(im, corner, (14, 11, 10), thresh=60)
    return im.filter(ImageFilter.UnsharpMask(radius=1.2, percent=60, threshold=2))


def data_uri(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


# -- rendering -------------------------------------------------------------------------------------------

def browser() -> str:
    for c in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              shutil.which("google-chrome") or "", shutil.which("chromium") or "", shutil.which("microsoft-edge") or ""):
        if c and Path(c).exists():
            return c
    sys.exit("needs Microsoft Edge or Chrome to render the artwork")


def render(html: str, width: int, height: int, scale: int = 2) -> Image.Image:
    with tempfile.TemporaryDirectory() as tmp:
        page, shot = Path(tmp) / "page.html", Path(tmp) / "shot.png"
        page.write_text(html, encoding="utf-8")
        subprocess.run([browser(), "--headless=new", "--disable-gpu", "--hide-scrollbars",
                        "--default-background-color=00000000",  # keeps rounded corners transparent
                        f"--force-device-scale-factor={scale}", f"--window-size={width},{height}",
                        "--virtual-time-budget=2000", f"--screenshot={shot}", page.as_uri()],
                       check=True, capture_output=True, timeout=180)
        return Image.open(shot).convert("RGBA").copy()


def page(body: str, width: int, height: int, extra_css: str = "") -> str:
    fonts = (ROOT / "engine" / "report_fonts.css").read_text(encoding="utf-8")
    return f"""<!doctype html><meta charset="utf-8"><style>{fonts}
*{{box-sizing:border-box}}
html,body{{margin:0;width:{width}px;height:{height}px;overflow:hidden;background:transparent}}
.stage{{position:relative;width:{width}px;height:{height}px;overflow:hidden}}
.mono{{font-family:"RP JetBrains Mono",Consolas,monospace}}
.serif{{font-family:"RP Courier Prime","Courier New",monospace}}
svg{{position:absolute;inset:0;overflow:visible}}
{extra_css}</style><body><div class="stage">{body}</div></body>"""


GRAIN = """<svg width="100%" height="100%" style="position:absolute;inset:0;mix-blend-mode:screen;opacity:.09">
<filter id="g"><feTurbulence type="fractalNoise" baseFrequency=".9" numOctaves="2" stitchTiles="stitch"/>
<feColorMatrix values="0 0 0 0 1  0 0 0 0 1  0 0 0 0 1  0 0 0 1.4 -.35"/></filter>
<rect width="100%" height="100%" filter="url(#g)"/></svg>"""

# Ink texture for printed elements (sun, title): knocks tiny specks out, like a worn press print.
PRESS = """<filter id="press" x="0" y="0" width="100%" height="100%">
<feTurbulence type="fractalNoise" baseFrequency="1.1" numOctaves="3" seed="4" result="n"/>
<feColorMatrix in="n" type="matrix" values="0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0 0 0 -9 7.2" result="m"/>
<feComposite in="SourceGraphic" in2="m" operator="in"/></filter>"""


def icon(kind: str, x: float, y: float, s: float = 1.0) -> str:
    """Line-art icons, drawn on a 40-unit box, blueprint style."""
    g = {
        "archive": '<rect x="4" y="8" width="32" height="9"/><path d="M7 17v17h26V17M16 23h8"/>',
        "db": '<ellipse cx="20" cy="9" rx="13" ry="5"/><path d="M7 9v22c0 2.8 5.8 5 13 5s13-2.2 13-5V9'
              'M7 20c0 2.8 5.8 5 13 5s13-2.2 13-5"/>',
        "folder": '<path d="M4 11h12l3 4h17v19H4z"/><path d="M4 15h32"/>',
        "app": '<rect x="4" y="7" width="32" height="26"/><path d="M4 13h32M9 19h14M9 24h20M9 28h10"/>',
        "probe": '<circle cx="17" cy="17" r="10"/><path d="M24.5 24.5L35 35"/>',
        "shield": '<path d="M20 4l14 5v9c0 9-6 15-14 18C12 33 6 27 6 18V9z"/><path d="M13 19l5 5 9-10"/>',
        "net": '<circle cx="20" cy="20" r="15"/><path d="M5 20h30M20 5c-6 8-6 22 0 30M20 5c6 8 6 22 0 30"/>',
        "clock": '<circle cx="20" cy="21" r="14"/><path d="M20 12v10l7 4M16 4h8"/>',
        "broom": '<path d="M27 4L18 22M10 22h16l3 13H7z"/><path d="M13 27v8M18 27v8M23 27v8"/>',
    }[kind]
    return (f'<g transform="translate({x} {y}) scale({s})" fill="none" stroke="{BLUE}" stroke-width="{1.6 / s:.2f}" '
            f'stroke-linejoin="round" stroke-linecap="round">{g}</g>')


def cross(x: float, y: float, r: float = 7) -> str:
    return f'<path d="M{x - r} {y}h{2 * r}M{x} {y - r}v{2 * r}" stroke="{BLUE}" stroke-width="1" opacity=".7"/>'


# -- the cover -------------------------------------------------------------------------------------------


# -- explanatory graphics --------------------------------------------------------------------------------

PANEL_CSS = f"""
.card{{position:absolute;inset:0;background:{BLACK};border-radius:18px;overflow:hidden}}
.k{{font:600 12px/1 "RP JetBrains Mono",monospace;letter-spacing:.16em;color:{BLUE}}}
"""


# Explanatory graphics are laid out at the width GitHub shows a README image (about 880px) and
# rendered at 2x, so their 13px text stays 13px on screen and sharp on high-density displays.

def story() -> Image.Image:
    W, H = 900, 336
    cols = [
        (40, "THE BACKUP JOB", "“success”", "a file was written",
         [("db.sql.gz        24 KB", CREAM), ("uploads.tar.gz   43 KB", DIM), ("digest recorded  ✓", DIM)], "archive"),
        (330, "THE HEALTH CHECK", "200 OK", "the process answers",
         [("GET /health", CREAM), ("→ 200 application/json", DIM), ('{"status": "ok"}', DIM)], "net"),
        (620, "THE DRILL", "PARTIAL", "what customers get",
         [("✗ 55 invoice PDFs missing", RED), ("✗ INV-2152 → 404", RED), ("✓ 11 other checks pass", DIM)], "shield"),
    ]
    blocks = []
    for x, k, big, sub, lines, ic in cols:
        partial = big == "PARTIAL"
        rows = "".join(f'<text x="{x}" y="{222 + i * 22}" class="mono" font-size="13" fill="{c}" '
                       f'xml:space="preserve">{t}</text>' for i, (t, c) in enumerate(lines))
        blocks.append(f"""
<text x="{x}" y="62" class="mono" font-size="11" letter-spacing="2" fill="{BLUE}">{k}</text>
{icon(ic, x + 210, 40, 0.75)}
<path d="M{x} 78H{x + 240}" stroke="{BLUE}" stroke-width="1"/>
<text x="{x}" y="138" class="serif" font-size="36" font-weight="{700 if partial else 400}"
  letter-spacing="{3 if partial else 0}" fill="{YELLOW if partial else CREAM}">{big}</text>
<text x="{x}" y="166" class="mono" font-size="12" fill="{DIM}">{sub}</text>
<path d="M{x} 188H{x + 240}" stroke="{BLUE}" stroke-width="1" opacity=".35"/>
{rows}""")
    arrows = "".join(f'<path d="M{x} 126h20" stroke="{BLUE}" stroke-width="1.5"/>'
                     f'<path d="M{x + 15} 121l6 5-6 5" fill="none" stroke="{BLUE}" stroke-width="1.5"/>'
                     for x in (292, 582))
    body = f"""<div class="card"></div>
<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}">{''.join(blocks)}{arrows}
<path d="M40 292H860" stroke="{BLUE}" stroke-width="1" stroke-dasharray="2 6" opacity=".5"/>
<text x="40" y="318" class="mono" font-size="12.5" fill="{CREAM}">Same backup, same night.
<tspan fill="{DIM}">Only the drill rebuilt the app and checked what customers would get.</tspan></text>
</svg>"""
    return render(page(body, W, H, PANEL_CSS), W, H)


def pipeline() -> Image.Image:
    W, H = 900, 420
    stages = [
        ("PREFLIGHT", "archive", "Artifacts exist, match the", "recorded digest, unpack safely"),
        ("ISOLATE", "net", "A fresh --internal network:", "no internet, no production"),
        ("RESTORE", "db", "Backup into a throwaway", "database, files to scratch"),
        ("BOOT", "app", "Your image starts on the", "restored data; /health answers"),
        ("VERIFY", "probe", "Data checks, every file", "reference, real user journeys"),
        ("CLEAN UP", "broom", "Everything removed, then", "listed again to prove it"),
    ]
    parts = []
    for i, (name, ic, l1, l2) in enumerate(stages):
        col, row = i % 3, i // 3
        x, y = 40 + col * 280, 40 + row * 150
        parts.append(f"""
{icon(ic, x, y, 0.9)}
<text x="{x + 52}" y="{y + 15}" class="mono" font-size="11" fill="{DIM}">0{i + 1}</text>
<text x="{x + 52}" y="{y + 33}" class="mono" font-size="14" font-weight="600" letter-spacing="1.5" fill="{CREAM}">{name}</text>
<text x="{x}" y="{y + 70}" class="mono" font-size="12.5" fill="{DIM}">{l1}</text>
<text x="{x}" y="{y + 90}" class="mono" font-size="12.5" fill="{DIM}">{l2}</text>""")
        if col < 2:
            ax = x + 172
            parts.append(f'<path d="M{ax} {y + 18}h68" stroke="{BLUE}" stroke-width="1.5"/>'
                         f'<path d="M{ax + 62} {y + 13}l6 5-6 5" fill="none" stroke="{BLUE}" stroke-width="1.5"/>')
    # the row break: from BOOT back to the start of the second row
    parts.append(f'<path d="M760 146v14H58v18" fill="none" stroke="{BLUE}" stroke-width="1.5" stroke-dasharray="3 4" '
                 f'opacity=".6"/><path d="M53 173l5 7 5-7" fill="none" stroke="{BLUE}" stroke-width="1.5" opacity=".6"/>')
    body = f"""<div class="card"></div>
<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}">{''.join(parts)}
<path d="M40 344H860" stroke="{BLUE}" stroke-width="1" stroke-dasharray="2 6" opacity=".5"/>
<text x="40" y="372" class="mono" font-size="12.5" fill="{CREAM}">Then it measures recovery time and the data-loss window,
<tspan fill="{DIM}">and</tspan></text>
<text x="40" y="394" class="mono" font-size="12.5" fill="{DIM}">writes a report with a sha256 you can verify.
<tspan fill="{CREAM}">No language model decides a verdict.</tspan></text>
</svg>"""
    return render(page(body, W, H, PANEL_CSS), W, H)


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    met = Path(sys.argv[1])
    ART.mkdir(parents=True, exist_ok=True)
    krater_band(met / "krater.jpg").save(ART / "krater.jpg", quality=86, optimize=True, progressive=True)
    for name, fn in (("story.png", story), ("pipeline.png", pipeline)):
        fn().save(ASSETS / name, optimize=True)  # kept at 2x: shown at half size, sharp on dense screens
    for f in ("art/krater.jpg", "story.png", "pipeline.png"):
        p = ASSETS / f
        print(f"assets/{f}  {p.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
