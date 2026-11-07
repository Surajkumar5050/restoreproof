"""Build assets/drill.svg: an animated replay of a real drill transcript for the README.

    python scripts/build_readme_svgs.py

Uses the font subsets in engine/report_fonts.css (SIL OFL 1.1, see licenses/). GitHub renders SVG
images with their CSS animations and with fonts embedded as data URIs, so nothing here makes a network
request. Viewers who ask for reduced motion see the finished transcript. The cover and the other README
graphics are built by scripts/build_readme_art.py.
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"

# Same ground and line colors as the other README graphics; result colors from engine/palette.py.
BLACK, LINE, BLUE, CREAM, DIM = "#0b0b0e", "#23233a", "#4d6bff", "#efe6d2", "#8c8676"
YELLOW, OK, WARN, ERR = "#ffd21f", "#4caf50", "#ffa726", "#ef5350"


def font_faces() -> str:
    css = (ROOT / "engine" / "report_fonts.css").read_text(encoding="utf-8")
    faces = re.findall(r"@font-face\{[^}]*\}", css)
    return "".join(f.replace('"RP JetBrains Mono"', '"RPJB"') for f in faces if "JetBrains" in f)


# Copied from a real run of the demo's broken backup (rp-20261107-095753-f1aa).
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
    """Split one transcript line into (text, color, weight) runs, colored as engine/term.py colors them."""
    if line.startswith("RESTOREPROOF"):
        return [("RESTOREPROOF", YELLOW, "700"), (line[len("RESTOREPROOF"):], DIM, "400")]
    if line.startswith(("╭", "╰", "│")):
        if line.startswith("╭"):
            a, b = line.split("PARTIAL", 1)
            return [(a, WARN, "400"), ("PARTIAL", WARN, "700"), (b, WARN, "400")]
        bar, body = line[:1], line[1:]
        if body.strip().startswith(("The app boots", "its database")):
            return [(bar, WARN, "400"), (body, CREAM, "700")]
        if "drill found" in body:
            a, b = body.split("PARTIAL", 1)
            return [(bar, WARN, "400"), (a, DIM, "400"), ("PARTIAL", WARN, "700"), (b, CREAM, "400")]
        if "not met" in body:
            a, b = body.split("not met", 1)
            return [(bar, WARN, "400"), (a, DIM, "400"), ("not met" + b, WARN, "400")]
        if body.rstrip().endswith(" met"):
            return [(bar, WARN, "400"), (body[:-4], DIM, "400"), (body[-4:], OK, "400")]
        if "backup job said" in body:
            a, b = body.split("“", 1)
            return [(bar, WARN, "400"), (a, DIM, "400"), ("“" + b, CREAM, "400")]
        return [(bar, WARN, "400"), (body, DIM, "400")]
    label, rest = line[:11], line[11:]
    runs = [(label, DIM, "400")]
    m = re.match(r"(\s*)([✓✗])( .*?)(\s{2,}\S+)?$", rest)
    if m:
        good = m.group(2) == "✓"
        runs += [(m.group(1), DIM, "400"), (m.group(2), OK if good else ERR, "700"),
                 (m.group(3), CREAM if good else ERR, "400")]
        if m.group(4):
            runs.append((m.group(4), DIM, "400"))
    else:
        runs.append((rest, DIM, "400"))
    return runs


def drill_svg() -> str:
    lines = TRANSCRIPT.split("\n")
    lh, top, left = 21, 92, 32
    width, height = 960, top + lh * (len(lines) + 1) + 28
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
    cmd = "hermes restoreproof run examples/billing-portal/restoreproof.yaml"
    type_end = 1.4 / cycle * 100
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img"
 aria-label="A RestoreProof drill: the backup restores and the app boots, but 55 invoice files are missing, so the verdict is PARTIAL.">
<style>{font_faces()}
text{{font-family:"RPJB",ui-monospace,Consolas,monospace;font-size:13.4px}}
{''.join(keyframes)}
@keyframes type{{0%{{width:0}}{type_end:.2f}%,93%{{width:{len(cmd) * 8.1 + 8}px}}97%,100%{{width:0}}}}
.typed{{animation:type {cycle}s steps({len(cmd)}) infinite}}
@keyframes blink{{0%,49%{{opacity:1}}50%,100%{{opacity:0}}}} .cur{{animation:blink 1s steps(1) infinite}}
@media (prefers-reduced-motion:reduce){{[class^="l"],.typed,.cur{{animation:none}}}}
</style>
<rect width="{width}" height="{height}" rx="18" fill="{BLACK}"/>
<text x="{left}" y="38" fill="{BLUE}" font-size="11.5" letter-spacing="2.2">TERMINAL · A REAL RUN OF THE DEMO</text>
<text x="{width - left}" y="38" fill="{DIM}" font-size="11.5" text-anchor="end">broken backup, nightly-2026-11-06</text>
<rect x="{left}" y="52" width="{width - 2 * left}" height="1" fill="{LINE}"/>
<clipPath id="typing"><rect class="typed" x="{left + 16}" y="64" height="22" width="{len(cmd) * 8.1 + 8}"/></clipPath>
<text x="{left}" y="80" fill="{YELLOW}">$</text>
<text x="{left + 16}" y="80" fill="{CREAM}" clip-path="url(#typing)">{cmd}</text>
{''.join(rows)}
<text class="cur" x="{left}" y="{top + lh * (len(lines) + 1)}" fill="{YELLOW}">▍</text>
</svg>
"""


def main() -> None:
    out = ASSETS / "drill.svg"
    out.write_text(drill_svg(), encoding="utf-8")
    print(f"assets/drill.svg  {out.stat().st_size // 1024} KB")


if __name__ == "__main__":
    sys.exit(main())
