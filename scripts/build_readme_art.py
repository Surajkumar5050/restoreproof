"""Build the README and catalog artwork in assets/.

    python scripts/build_readme_art.py MET_DIR

MET_DIR holds two public-domain originals from The Met's Open Access collection (CC0), unchanged:
  hermes.jpg  Marble statue of Hermes, after Polykleitos, 1st-2nd c. CE. The Met 56.234.15 (object 254925),
              https://images.metmuseum.org/CRDImages/gr/original/DP253642.jpg
  krater.jpg  Terracotta bell-krater, the Return of Persephone, Persephone Painter, ca. 440 BCE.
              The Met 28.57.23 (object 252973), https://images.metmuseum.org/CRDImages/gr/original/DP116943.jpg

Writes:
  assets/art/krater.jpg           the painted scene band, toned
  assets/banner.jpg               2:1 cover, used by the plugin catalog and as the README hero
  assets/story.png                what a backup status, a health check and a drill each report
  assets/pipeline.png             the six stages of a drill

Needs Pillow, numpy, Microsoft Edge or Chrome (headless, for type and line-art), Franklin Gothic Heavy
(ships with Windows) and engine/report_fonts.css for Courier Prime and JetBrains Mono. Deterministic:
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

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
ART = ASSETS / "art"

# The catalog's shared poster language: black ground, cream engraving, red sun, blueprint blue, yellow type.
BLACK, CREAM, SUN, BLUE, YELLOW, RED, DIM = "#0b0b0e", "#efe6d2", "#d6372b", "#4d6bff", "#ffd21f", "#ff5a4e", "#8c8676"
KRATER_BOX = (0.262, 0.335, 0.738, 0.664)


# -- source treatments -----------------------------------------------------------------------------------

def engrave(src: Path, width: int, period: float) -> Image.Image:
    """The statue as cream line-art: an angled line screen that bends with the form and thickens with light."""
    im = Image.open(src).convert("RGB")
    w, h = im.size
    crop = im.crop((int(w * 0.243), int(h * 0.062), int(w * 0.798), int(h * 0.392)))  # head, shoulders, drape
    ss = 2  # supersample, then reduce, for clean line edges
    crop = crop.resize((width * ss, round(crop.height * width * ss / crop.width)), Image.LANCZOS)
    a = np.asarray(crop).astype(np.float32) / 255.0
    lum = 0.2126 * a[..., 0] + 0.7152 * a[..., 1] + 0.0722 * a[..., 2]
    warm = a[..., 0] - a[..., 2]  # marble is warm; the museum backdrop is neutral grey

    m = Image.fromarray(((warm > 0.045) * 255).astype(np.uint8))
    m = m.filter(ImageFilter.MedianFilter(9)).filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(9))
    mask = np.asarray(m.filter(ImageFilter.GaussianBlur(3 * ss))).astype(np.float32) / 255.0
    mask = np.clip((mask - 0.35) / 0.3, 0, 1)

    tone = Image.fromarray((lum * 255).astype(np.uint8)).filter(
        ImageFilter.UnsharpMask(radius=40 * ss, percent=160, threshold=0))
    t = np.asarray(tone).astype(np.float32) / 255.0
    lo, hi = np.percentile(t[mask > 0.5], [3, 99.5])
    t = np.clip((t - lo) / (hi - lo), 0, 1) ** 1.25

    hh, ww = t.shape
    yy, xx = np.mgrid[0:hh, 0:ww].astype(np.float32)
    p = period * ss
    soft = np.asarray(Image.fromarray((t * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(14 * ss))) / 255.0
    aa = 1.6 / p
    ang = np.deg2rad(-28)
    u = xx * np.cos(ang) + yy * np.sin(ang) + soft * p * 2.2
    line = np.clip((t * 0.92 - np.abs(((u / p) % 1.0) - 0.5) * 2.0) / aa + 0.5, 0, 1)
    ang2 = np.deg2rad(52)
    u2 = xx * np.cos(ang2) + yy * np.sin(ang2) + soft * p
    hl = np.clip(((t - 0.72) * 2.2 - np.abs(((u2 / (p * 0.75)) % 1.0) - 0.5) * 2.0) / aa + 0.5, 0, 1)

    ink = Image.fromarray((np.maximum(line, hl) * mask * 255).astype(np.uint8))
    ink = ink.resize((width, round(hh / ss)), Image.LANCZOS)
    out = Image.new("RGB", ink.size, tuple(int(CREAM[i:i + 2], 16) for i in (1, 3, 5)))
    out.putalpha(ink)
    return out


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
.poster{{font-family:"Franklin Gothic Heavy","Franklin Gothic","Arial Black",sans-serif;font-weight:900}}
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

def cover(figure: Image.Image) -> Image.Image:
    W, H = 1600, 800
    fw = 720
    fh = round(figure.height * fw / figure.width)
    fx, fy = (W - fw) // 2 + 6, 46
    uri = data_uri(figure)
    b1, b2 = 386, 412  # the slice of the figure that slips: the data that did not come back

    files = []
    for i in range(96):  # 24 x 4 cells; the run in red is where the missing folder sits
        col, row = i % 24, i // 24
        gone = 74 <= i <= 79
        x, y = 74 + col * 15, 520 + row * 15
        files.append(f'<rect x="{x}" y="{y}" width="11" height="11" fill="{RED if gone else "none"}" '
                     f'stroke="{RED if gone else BLUE}" stroke-width="1" opacity="{1 if gone else .55}"/>')

    term = [
        ("$ hermes restoreproof run", CREAM),
        ("preflight  ✓ artifacts intact", BLUE),
        ("isolate    ✓ internal network", BLUE),
        ("restore    ✓ 1,290 rows · 1,149 files", BLUE),
        ("boot       ✓ GET /health 200", BLUE),
        ("verify     ✗ 55 invoice PDFs missing", RED),
        ("           ✗ INV-2152 returns 404", RED),
    ]
    term_svg = "".join(f'<text x="92" y="{226 + i * 27}" fill="{c}" class="mono" font-size="14.5" '
                       f'xml:space="preserve">{t}</text>' for i, (t, c) in enumerate(term))

    body = f"""
<div style="position:absolute;inset:0;background:{BLACK}"></div>
<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}"><defs>{PRESS}</defs>
<path d="M0 400H{W}" stroke="{BLUE}" stroke-width="1" stroke-dasharray="2 7" opacity=".35"/>
<path d="M800 0V{H}" stroke="{BLUE}" stroke-width="1" stroke-dasharray="2 7" opacity=".22"/>
{cross(60, 60)}{cross(W - 60, 60)}{cross(60, 470)}{cross(W - 60, 470)}{cross(520, 140)}{cross(1080, 140)}
<circle cx="788" cy="232" r="186" fill="{SUN}" filter="url(#press)"/>
</svg>
<img src="{uri}" style="position:absolute;left:{fx}px;top:{fy}px;width:{fw}px;
  clip-path:inset(0 0 {fh - (b1 - fy)}px 0)">
<img src="{uri}" style="position:absolute;left:{fx}px;top:{fy}px;width:{fw}px;
  clip-path:inset({b2 - fy}px 0 0 0);
  -webkit-mask-image:linear-gradient(to bottom,#000 0,#000 {520 - fy}px,transparent {612 - fy}px)">
<img src="{uri}" style="position:absolute;left:{fx + 34}px;top:{fy}px;width:{fw}px;
  clip-path:inset({b1 - fy}px 0 {fh - (b2 - fy)}px 0)">
<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}">
<rect x="{fx + 120}" y="{b1 - 1}" width="210" height="8" fill="{YELLOW}"/>
<rect x="{fx + 430}" y="{b2 - 6}" width="160" height="6" fill="{BLUE}"/>
<rect x="{fx + 40}" y="{b1 + 9}" width="64" height="5" fill="{BLUE}"/>

<g>
<rect x="74" y="168" width="390" height="230" fill="none" stroke="{BLUE}" stroke-width="1.5"/>
<path d="M74 194H464" stroke="{BLUE}" stroke-width="1"/>
<text x="88" y="186" fill="{BLUE}" class="mono" font-size="12" letter-spacing="1.5">DRILL · billing-portal</text>
<text x="450" y="186" fill="{BLUE}" class="mono" font-size="12" text-anchor="end">nightly-2026-11-06</text>
{term_svg}
<path d="M74 430H464" stroke="{BLUE}" stroke-width="1" opacity=".6"/>
<text x="74" y="458" fill="{CREAM}" class="mono" font-size="13">1,204 files the database points to</text>
<text x="74" y="480" fill="{DIM}" class="mono" font-size="12">55 missing, all in invoices/2026/10/</text>
<path d="M74 498v6M464 498v6M74 501H464" stroke="{BLUE}" stroke-width="1" opacity=".6"/>
{''.join(files)}
</g>

<g>
<text x="1140" y="176" fill="{BLUE}" class="mono" font-size="12" letter-spacing="1.5">THE BACKUP SAID</text>
<text x="1140" y="206" fill="{CREAM}" class="serif" font-size="26">“success”</text>
{icon("archive", 1470, 168, 1.05)}
<path d="M1140 228H1526" stroke="{BLUE}" stroke-width="1"/>
<path d="M1333 236v22" stroke="{BLUE}" stroke-width="1.5"/><path d="M1327 252l6 7 6-7" fill="none" stroke="{BLUE}" stroke-width="1.5"/>
<rect x="1140" y="266" width="386" height="128" fill="none" stroke="{BLUE}" stroke-width="1.5" stroke-dasharray="6 5"/>
<text x="1154" y="288" fill="{BLUE}" class="mono" font-size="12" letter-spacing="1.5">ISOLATED · NO EGRESS</text>
{icon("db", 1166, 304, 1.15)}{icon("folder", 1282, 304, 1.15)}{icon("app", 1398, 304, 1.15)}
<text x="1189" y="380" fill="{BLUE}" class="mono" font-size="12" text-anchor="middle">restore</text>
<text x="1305" y="380" fill="{BLUE}" class="mono" font-size="12" text-anchor="middle">files</text>
<text x="1421" y="380" fill="{BLUE}" class="mono" font-size="12" text-anchor="middle">boot</text>
<path d="M1333 402v22" stroke="{BLUE}" stroke-width="1.5"/><path d="M1327 418l6 7 6-7" fill="none" stroke="{BLUE}" stroke-width="1.5"/>
<text x="1140" y="450" fill="{BLUE}" class="mono" font-size="12" letter-spacing="1.5">THE DRILL FOUND</text>
<text x="1140" y="478" fill="{BLUE}" class="mono" font-size="14.5" xml:space="preserve">✓ data    ✓ boot</text>
<text x="1140" y="502" fill="{RED}" class="mono" font-size="14.5" xml:space="preserve">✗ files   ✗ journey</text>
<rect x="1140" y="520" width="236" height="56" fill="none" stroke="{YELLOW}" stroke-width="2.5"/>
<text x="1258" y="559" fill="{YELLOW}" class="serif" font-size="32" font-weight="700" letter-spacing="4"
  text-anchor="middle">PARTIAL</text>
</g>

<text x="248" y="652" fill="{CREAM}" class="mono" font-size="15" letter-spacing="6">COMMUNITY PLUGIN · HERMES AGENT</text>
<text x="800" y="772" fill="{YELLOW}" class="poster" font-size="146" text-anchor="middle" letter-spacing="-1"
  filter="url(#press)">RESTOREPROOF</text>
<text x="1352" y="652" fill="{CREAM}" class="mono" font-size="13" text-anchor="end"
  >Backups are claims. Drills are proof.</text>
</svg>
{GRAIN}"""
    return render(page(body, W, H), W, H).convert("RGB")


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
        ("RESTORE", "db", "Dump into a throwaway", "PostgreSQL, files to scratch"),
        ("BOOT", "app", "Your image starts on the", "restored data; /health answers"),
        ("VERIFY", "probe", "SQL invariants, every file", "reference, real user journeys"),
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
    figure = engrave(met / "hermes.jpg", width=1440, period=9.0)  # drawn at 2x for the 2x render
    krater_band(met / "krater.jpg").save(ART / "krater.jpg", quality=86, optimize=True, progressive=True)

    big = cover(figure)
    big.resize((1600, 800), Image.LANCZOS).save(ASSETS / "banner.jpg", quality=88, optimize=True, progressive=True)
    for name, fn in (("story.png", story), ("pipeline.png", pipeline)):
        fn().save(ASSETS / name, optimize=True)  # kept at 2x: shown at half size, sharp on dense screens
    for f in ("art/krater.jpg", "banner.jpg", "story.png", "pipeline.png"):
        p = ASSETS / f
        print(f"assets/{f}  {p.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
