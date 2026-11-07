"""Build the README's animated SVGs and the catalog banner.

    python scripts/build_readme_svgs.py

Reads assets/art/hero-bg.jpg (from scripts/build_readme_art.py) and the font subsets in
engine/report_fonts.css (SIL OFL 1.1, see licenses/). Writes:
  assets/hero.svg    animated hero for the README
  assets/drill.svg   animated replay of a real drill transcript
  assets/banner.jpg  1280x640 still of the hero for the plugin catalog (needs Edge or Chrome, Pillow)

GitHub renders SVG images with their CSS animations, and fonts and images embedded as data URIs,
so nothing here makes a network request.
"""

from __future__ import annotations

import base64
import html
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"

INK, PANEL, LINE = "#08080F", "#11111D", "#2A2940"
GOLD, BRONZE, GOLDENROD, CORNSILK, DIM = "#FFD21F", "#CD7F32", "#DAA520", "#FFF4DA", "#9C9277"
OK, WARN, ERR, BLUE = "#4CAF50", "#FFA726", "#EF5350", "#1F4BFF"


def font_faces() -> str:
    css = (ROOT / "engine" / "report_fonts.css").read_text(encoding="utf-8")
    faces = re.findall(r"@font-face\{[^}]*\}", css)
    out = []
    for face in faces:
        face = face.replace('"RP Courier Prime"', '"RPCP"').replace('"RP JetBrains Mono"', '"RPJB"')
        out.append(face)
    return "".join(out)


def b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


MOTION = f"""<g clip-path="url(#sun)"><g class="scan"><rect x="640" y="20" width="620" height="46" fill="url(#beam)"/>
<rect x="640" y="65" width="620" height="2" fill="{GOLD}"/></g></g>
<rect class="flick" x="884" y="148" width="12" height="12" fill="{GOLD}"/>
<rect class="flick2" x="1150" y="210" width="16" height="16" fill="{BLUE}"/>"""


# -- hero ----------------------------------------------------------------------------------------------

def hero_svg(still: bool = False) -> str:
    """still=True drops the motion layers, for the catalog banner."""
    bg = b64(ASSETS / "art" / "hero-bg.jpg")
    motion = "" if still else MOTION
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 640" width="1280" height="640" role="img"
 aria-label="RestoreProof. Backups are claims. Drills are proof. A Hermes Agent plugin.">
<style>{font_faces()}
.cp{{font-family:"RPCP","Courier New",monospace}} .jb{{font-family:"RPJB",ui-monospace,monospace}}
.title{{font-weight:700;font-size:132px;letter-spacing:-2px}}
@keyframes jitter{{0%,86%,100%{{transform:translate(0,0)}}87%{{transform:translate(-7px,2px)}}88%{{transform:translate(5px,-1px)}}
  89%{{transform:translate(-3px,0)}}90%{{transform:translate(0,0)}}96%{{transform:translate(4px,1px)}}97%{{transform:translate(0,0)}}}}
@keyframes scan{{0%{{transform:translateY(0);opacity:0}}6%{{opacity:1}}88%{{opacity:1}}100%{{transform:translateY(560px);opacity:0}}}}
@keyframes blink{{0%,49%{{opacity:1}}50%,100%{{opacity:0}}}}
@keyframes flick{{0%,100%{{opacity:1}}40%{{opacity:1}}41%{{opacity:0}}52%{{opacity:0}}53%{{opacity:1}}}}
@keyframes found{{0%,18%{{opacity:0}}19%,100%{{opacity:1}}}}
.ghost{{animation:jitter 6s steps(1) infinite}}
.scan{{animation:scan 5.5s cubic-bezier(.45,0,.55,1) infinite}}
.cursor{{animation:blink 1.1s steps(1) infinite}}
.flick{{animation:flick 3.7s steps(1) infinite}} .flick2{{animation:flick 5.3s steps(1) infinite 1.2s}}
@media (prefers-reduced-motion:reduce){{.ghost,.scan,.cursor,.flick,.flick2{{animation:none}}}}
</style>
<defs><linearGradient id="beam" x1="0" y1="0" x2="0" y2="1">
<stop offset="0" stop-color="{GOLD}" stop-opacity="0"/><stop offset=".7" stop-color="{GOLD}" stop-opacity=".22"/>
<stop offset="1" stop-color="{GOLD}" stop-opacity=".9"/></linearGradient>
<clipPath id="sun"><circle cx="948" cy="292" r="300"/></clipPath></defs>
<image href="data:image/jpeg;base64,{bg}" width="1280" height="640"/>

{motion}

<g transform="translate(72 0)">
<rect x="0" y="104" width="10" height="10" fill="#D63428"/>
<text class="jb" x="22" y="114" font-size="15" letter-spacing="5" fill="{GOLD}">HERMES AGENT PLUGIN</text>
<g class="ghost"><text class="cp title" x="-4" y="252" fill="{BLUE}" opacity=".85">RESTORE</text>
<text class="cp title" x="-4" y="372" fill="{BLUE}" opacity=".85">PROOF</text></g>
<text class="cp title" x="0" y="250" fill="{GOLD}">RESTORE</text>
<text class="cp title" x="0" y="370" fill="{GOLD}">PROOF</text>
<text class="cp" x="2" y="430" font-size="27" fill="{CORNSILK}">Backups are claims. Drills are proof.</text>
<rect x="2" y="474" width="560" height="1" fill="{LINE}"/>
<text class="jb" x="2" y="514" font-size="17" fill="{DIM}">the backup said <tspan fill="{CORNSILK}">“success”</tspan></text>
<text class="jb" x="2" y="548" font-size="17" fill="{DIM}">the drill found <tspan fill="{WARN}" font-weight="700">PARTIAL</tspan><tspan fill="{CORNSILK}"> · 55 invoices gone</tspan>
{"" if still else f'<tspan class="cursor" fill="{GOLD}"> ▍</tspan>'}</text>
</g>
</svg>
"""


# -- drill replay ------------------------------------------------------------------------------------

TRANSCRIPT = """\
RESTOREPROOF  billing-portal · recovery point nightly-2026-11-06 · run rp-20261107-095753-f1aa

preflight  ✓ database artifact is present and intact
           ✓ database dump format is readable                              <1ms
           ✓ uploads artifact is present and intact
           ✓ uploads archive is safe to extract                            32ms
           ✓ container images are present and pinned by digest
isolate    ✓ isolated network created (internal, no internet egress)      187ms
restore    ✓ database restored from backup                                 3.9s
             2 tables, 1,290 rows restored into a fresh PostgreSQL
           ✓ uploads restored                                              5.6s
             1,149 files (713,758 bytes) extracted to isolated scratch storage
boot       ✓ service boots and /health responds                            2.5s
verify     ✓ customers came back                                          203ms
           ✓ every invoice belongs to a customer                          203ms
           ✗ every invoice PDF referenced in the database exists          219ms
             55 of 1,204 files the database points to are missing from the
             backup; all of them under invoices/2026/10/
           ✗ a customer opens invoice INV-2152 and downloads the PDF      297ms
             stopped at step 2 of 2, GET /files/invoices/2026/10/INV-2152.pdf:
             got HTTP 404, expected status 200; content-type application/pdf
clean up   ✓ 4 temporary resources removed, restored data deleted          2.0s

╭─ PARTIAL ──────────────────────────────────────────────────────────────────
│ The app boots, but the business does not fully come back: 55 of 1,204 files
│ its database points to are gone.
│
│ backup job said  “success”
│ drill found      PARTIAL in 24.1s
│ recovery time    21.8s    target 10m   not met: recovery incomplete
│ data-loss window 14h 09m  target 24h   met
╰────────────────────────────────────────────────────────────────────────────"""

# Seconds after the previous line, so the replay keeps the real rhythm (compressed).
PACE = {"preflight": 0.18, "isolate": 0.35, "restore": 1.1, "boot": 0.9, "verify": 0.6, "clean up": 0.7}


def classify(line: str) -> list[tuple[str, str, str]]:
    """Split one transcript line into (text, color, weight) runs."""
    if line.startswith("RESTOREPROOF"):
        rest = line[len("RESTOREPROOF"):]
        return [("RESTOREPROOF", GOLD, "700"), (rest, DIM, "400")]
    if line.startswith(("╭", "╰", "│")):
        if "PARTIAL" in line and line.startswith("╭"):
            a, b = line.split("PARTIAL", 1)
            return [(a, WARN, "400"), ("PARTIAL", WARN, "700"), (b, WARN, "400")]
        bar, body = line[:1], line[1:]
        if body.strip().startswith("The app boots") or body.strip().startswith("its database"):
            return [(bar, WARN, "400"), (body, CORNSILK, "700")]
        if "drill found" in body:
            a, b = body.split("PARTIAL", 1)
            return [(bar, WARN, "400"), (a, DIM, "400"), ("PARTIAL", WARN, "700"), (b, DIM, "400")]
        if "not met" in body:
            a, b = body.split("not met", 1)
            return [(bar, WARN, "400"), (a, DIM, "400"), ("not met" + b, ERR, "400")]
        if body.rstrip().endswith(" met"):
            return [(bar, WARN, "400"), (body[:-4], DIM, "400"), (body[-4:], OK, "400")]
        return [(bar, WARN, "400"), (body, DIM, "400")]
    label, rest = line[:11], line[11:]
    runs = []
    if label.strip():
        runs.append((label, GOLDENROD, "400"))
    else:
        runs.append((label, DIM, "400"))
    m = re.match(r"(\s*)([✓✗])( .*?)(\s{2,}\S+)?$", rest)
    if m:
        mark_color = OK if m.group(2) == "✓" else ERR
        runs += [(m.group(1), DIM, "400"), (m.group(2), mark_color, "700"),
                 (m.group(3), CORNSILK if m.group(2) == "✓" else ERR, "400")]
        if m.group(4):
            runs.append((m.group(4), DIM, "400"))
    else:
        runs.append((rest, DIM, "400"))
    return runs


def drill_svg() -> str:
    lines = TRANSCRIPT.split("\n")
    lh, top, left = 21, 86, 30
    width, height = 960, top + lh * (len(lines) + 1) + 26
    cycle = 19.0
    t = 1.6  # after the command has been typed
    stage = None
    rows, keyframes = [], []
    for i, line in enumerate(lines):
        label = line[:11].strip()
        if label in PACE:
            stage = label
        if i == 0:
            step = 0.25
        elif not line.strip():
            step = 0.05
        elif line.startswith("╭"):
            step = 0.9
        elif line.startswith(("│", "╰")):
            step = 0.06
        elif line.startswith("             "):
            step = 0.08
        else:
            step = PACE.get(stage, 0.2) if label else 0.16
        t += step
        p = t / cycle * 100
        keyframes.append(f"@keyframes l{i}{{0%,{p:.2f}%{{opacity:0}}{p + 0.4:.2f}%,93%{{opacity:1}}97%,100%{{opacity:0}}}}"
                         f".l{i}{{animation:l{i} {cycle}s linear infinite}}")
        spans = "".join(f'<tspan fill="{c}" font-weight="{w}">{html.escape(txt)}</tspan>' for txt, c, w in classify(line))
        rows.append(f'<text class="l{i}" x="{left}" y="{top + lh * (i + 1)}" xml:space="preserve">{spans}</text>')
    cmd = "hermes restoreproof run restoreproof.yaml"
    type_end = 1.4 / cycle * 100
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img"
 aria-label="A RestoreProof drill: the backup restores and the app boots, but 55 invoice files are missing, so the verdict is PARTIAL.">
<style>{font_faces()}
text{{font-family:"RPJB",ui-monospace,monospace;font-size:13.4px}}
{''.join(keyframes)}
@keyframes type{{0%{{width:0}}{type_end:.2f}%,93%{{width:{len(cmd) * 8.1 + 8}px}}97%,100%{{width:0}}}}
.typed{{animation:type {cycle}s steps({len(cmd)}) infinite}}
@keyframes blink{{0%,49%{{opacity:1}}50%,100%{{opacity:0}}}} .cur{{animation:blink 1s steps(1) infinite}}
@media (prefers-reduced-motion:reduce){{[class^="l"],.typed{{animation:none}}}}
</style>
<rect width="{width}" height="{height}" rx="12" fill="{INK}"/>
<rect x=".5" y=".5" width="{width - 1}" height="{height - 1}" rx="12" fill="none" stroke="{LINE}"/>
<text x="{left}" y="34" fill="{BRONZE}" font-size="12" letter-spacing="3">┊ HERMES AGENT · RESTOREPROOF</text>
<rect x="{left}" y="46" width="{width - 2 * left}" height="1" fill="{LINE}"/>
<clipPath id="typing"><rect class="typed" x="{left + 16}" y="58" height="22" width="0"/></clipPath>
<text x="{left}" y="74" fill="{GOLD}">$</text>
<text x="{left + 16}" y="74" fill="{CORNSILK}" clip-path="url(#typing)">{cmd}</text>
{''.join(rows)}
<text class="cur" x="{left}" y="{top + lh * (len(lines) + 1)}" fill="{GOLD}">▍</text>
</svg>
"""


# -- banner ----------------------------------------------------------------------------------------------

def browser() -> str | None:
    for c in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              shutil.which("google-chrome") or "", shutil.which("chromium") or "", shutil.which("microsoft-edge") or ""):
        if c and Path(c).exists():
            return c
    return None


def banner_png(svg_path: Path, out: Path) -> None:
    exe = browser()
    if not exe:
        print("no Edge/Chrome found; skipped banner.png")
        return
    page = ASSETS / "_banner.html"
    page.write_text(f'<!doctype html><meta charset="utf-8"><style>html,body{{margin:0;background:{INK}}}'
                    f'img{{display:block;width:1280px;height:640px}}</style><img src="{svg_path.name}">', encoding="utf-8")
    subprocess.run([exe, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1",
                    "--window-size=1280,640", "--virtual-time-budget=400", f"--screenshot={out}", page.as_uri()],
                   check=True, capture_output=True, timeout=120)
    page.unlink()


def main() -> None:
    (ASSETS / "hero.svg").write_text(hero_svg(), encoding="utf-8")
    (ASSETS / "drill.svg").write_text(drill_svg(), encoding="utf-8")
    still = ASSETS / "_hero-still.svg"
    still.write_text(hero_svg(still=True), encoding="utf-8")
    png = ASSETS / "_banner.png"
    banner_png(still, png)
    still.unlink()
    if png.exists():
        from PIL import Image

        Image.open(png).convert("RGB").save(ASSETS / "banner.jpg", quality=90, optimize=True, progressive=True)
        png.unlink()
    for name in ("hero.svg", "drill.svg", "banner.jpg"):
        f = ASSETS / name
        if f.exists():
            print(f"assets/{name}  {f.stat().st_size // 1024} KB")


if __name__ == "__main__":
    sys.exit(main())
