"""Terminal rendering. Colors come from engine/palette.py (Hermes's Classic skin).

Layout: a stage column, a result glyph, the assertion, and its duration right-aligned. Color sits
on the glyph, on a failed assertion's name and on the verdict; everything else is ink or muted.

Safe everywhere: plain text when output is piped or NO_COLOR is set, ASCII glyphs when the console
cannot encode Unicode (old Windows code pages), and it never raises on an unencodable character.
"""

from __future__ import annotations

import os
import sys
import textwrap

from .palette import DARK, verdict_color
from .report import STAGE_LABEL, headline, objective_rows
from .util import human_duration

GOLD, BRONZE, GOLDENROD = DARK["gold"], DARK["bronze"], DARK["label"]
CORNSILK, DIM = DARK["ink"], DARK["muted"]
OK, WARN, ERR, NEUTRAL = DARK["ok"], DARK["warn"], DARK["err"], DARK["neutral"]
VERDICT_COLOR = {v: verdict_color(v) for v in ("PASS", "PARTIAL", "FAIL", "BLOCKED", "INCONCLUSIVE")}
TONE = {"ok": OK, "warn": WARN, "bad": ERR, "muted": DIM}

WIDTH = 80          # box width
NAME_COL = 60       # assertion column width before the duration
STAGE_COL = 10


def _enable_windows_vt() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


class Term:
    def __init__(self, stream=None) -> None:
        self.stream = stream or sys.stdout
        try:
            self.stream.reconfigure(errors="replace")  # never crash on an unencodable glyph
        except Exception:
            pass
        enc = (getattr(self.stream, "encoding", None) or "ascii")
        try:
            "✓✗–─╭╰│·“”●".encode(enc)
            self.unicode = True
        except (UnicodeEncodeError, LookupError):
            self.unicode = False
        tty = hasattr(self.stream, "isatty") and self.stream.isatty()
        self.color = tty and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"
        if self.color:
            _enable_windows_vt()
        u = self.unicode
        self.g = {"PASS": "✓" if u else "+", "FAIL": "✗" if u else "x", "SKIPPED": "–" if u else "-",
                  "ERROR": "!", "h": "─" if u else "-", "tl": "╭" if u else "+", "bl": "╰" if u else "+",
                  "v": "│" if u else "|", "dot": "·" if u else "-", "lq": "“" if u else '"',
                  "rq": "”" if u else '"', "now": "●" if u else "*", "bar": "│" if u else "|"}
        self._stage_shown = None

    def c(self, text: str, hex_color: str | None = None, bold: bool = False) -> str:
        if not self.color or (not hex_color and not bold):
            return text
        codes = []
        if bold:
            codes.append("1")
        if hex_color:
            r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
            codes.append(f"38;2;{r};{g};{b}")
        return f"\033[{';'.join(codes)}m{text}\033[0m"

    def out(self, line: str = "") -> None:
        print(line, file=self.stream, flush=True)

    def wordmark(self) -> str:
        return self.c("RESTOREPROOF", GOLD, bold=True)

    # -- live drill ------------------------------------------------------------------------------------

    def header(self, app: str, recovery: str, run_id: str) -> None:
        d = self.g["dot"]
        self.out()
        self.out(self.wordmark() + "  " + self.c(app, CORNSILK, bold=True)
                 + self.c(f" {d} recovery point {recovery} {d} run {run_id}", DIM))
        self.out()
        self._stage_shown = None

    def _row(self, stage: str, glyph: str, glyph_color: str, name: str, name_color: str | None,
             seconds: float | None) -> None:
        label = stage.lower() if stage != self._stage_shown else ""
        self._stage_shown = stage
        took = human_duration(seconds) if seconds is not None else ""
        pad = " " * max(2, NAME_COL - len(name))
        self.out(self.c(f"{label:<{STAGE_COL}}", DIM) + " " + self.c(glyph, glyph_color, bold=True) + " "
                 + self.c(name, name_color) + (self.c(pad + f"{took:>6}", DIM) if took else ""))

    def _detail(self, text: str) -> None:
        for line in textwrap.wrap(text, WIDTH - STAGE_COL - 3):
            self.out(" " * (STAGE_COL + 3) + self.c(line, DIM))

    def progress(self, event: dict) -> None:
        if event["kind"] != "check":
            return
        chk = event["check"]
        bad = chk["status"] in ("FAIL", "ERROR")
        color = OK if chk["status"] == "PASS" else (ERR if bad else DIM)
        name = chk["name"] + ("" if chk["mandatory"] else " (advisory)")
        self._row(STAGE_LABEL.get(chk["stage"], chk["stage"].title()), self.g.get(chk["status"], "?"), color,
                  name, ERR if bad else CORNSILK, chk.get("duration"))
        if chk["status"] != "PASS" or chk["stage"] == "RESTORING":
            self._detail(chk["summary"])

    def verdict(self, ev: dict, report_path: str) -> None:
        g = self.g
        v = ev["verdict"]
        color = VERDICT_COLOR.get(v, NEUTRAL)
        cl = ev.get("cleanup") or {}
        clean_seconds = sum(s["ended"] - s["started"] for s in ev.get("stages", [])
                            if s.get("stage") == "CLEANING" and "ended" in s) or None
        if cl.get("ok"):
            self._row("Clean up", g["PASS"], OK,
                      f"{cl.get('removed', 0)} temporary resources removed, restored data deleted", CORNSILK,
                      clean_seconds)
        else:
            self._row("Clean up", g["FAIL"], ERR, f"still present: {cl.get('residual')}", ERR, clean_seconds)

        bar = self.c(g["v"], color) + " "
        self.out()
        self.out(self.c(g["tl"] + g["h"], color) + " " + self.c(v, color, bold=True) + " "
                 + self.c(g["h"] * max(4, WIDTH - len(v) - 4), color))
        for line in textwrap.wrap(headline(ev), WIDTH - 4):
            self.out(bar + self.c(line, CORNSILK, bold=True))
        self.out(self.c(g["v"], color))
        claimed = ev.get("claimed_backup_status")
        if isinstance(claimed, dict) and claimed.get("status"):
            self.out(bar + self.c(f"{'backup job said':<17}", DIM) + f"{g['lq']}{claimed['status']}{g['rq']}")
        self.out(bar + self.c(f"{'drill found':<17}", DIM) + self.c(v, color, bold=True)
                 + f" in {human_duration(ev.get('total_seconds'))}")
        for name, measured, target, result, tone in objective_rows(ev):
            if measured == "—" and tone == "muted":
                continue
            target_txt = f"target {target}" if target != "—" else ""
            self.out(bar + self.c(f"{name.lower():<17}", DIM) + f"{measured:<9}"
                     + self.c(f"{target_txt:<13}", DIM) + self.c(result, TONE[tone]))
        if not cl.get("ok"):
            self.out(bar + self.c("cleanup incomplete: run hermes restoreproof cleanup", ERR, bold=True))
        self.out(self.c(g["bl"] + g["h"] * (WIDTH - 1), color))
        self.out(self.c("report  ", DIM) + report_path)
        self.out()
