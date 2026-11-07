"""The one place RestoreProof's standalone colors are defined.

Values marked with a Hermes skin key are copied unchanged from Hermes's Classic skin
(hermes_cli/skin_engine.py) and its goldenrod light variant. The rest are steps between them.
Inside Hermes Desktop none of this applies: the Desktop UI uses the app's theme variables.
"""

from __future__ import annotations

# Screen, dark. Most of every surface is GROUND and INK; color is reserved for results.
DARK = {
    "ground": "#1a1a2e",   # completion_menu_bg
    "inset": "#212138",    # code blocks and transcripts only
    "rule": "#35334f",     # hairlines
    "rule_2": "#4a4766",   # table header rules
    "ink": "#fff8dc",      # banner_text
    "ink_2": "#d8cfb2",    # body copy
    "muted": "#a8a08a",    # labels, 6.5:1 on ground
    "tick": "#6f6a58",     # present-file marks in the file strip
    "gold": "#ffd700",     # banner_title, the wordmark only
    "bronze": "#cd7f32",   # banner_border, top and bottom rules
    "label": "#daa520",    # ui_label, section numbers
    "ok": "#4caf50",       # ui_ok
    "warn": "#ffa726",     # ui_warn
    "err": "#ef5350",      # ui_error
    "neutral": "#8b8682",  # session_border, BLOCKED and INCONCLUSIVE
}

# Paper and light mode, from Hermes's goldenrod light skin. Amber is one step darker than ui_warn
# so small text passes 4.5:1 on paper.
LIGHT = {
    "ground": "#fbf8ef",
    "inset": "#f3eedf",
    "rule": "#e3d9c2",
    "rule_2": "#cfc3a6",
    "ink": "#2b2416",
    "ink_2": "#5c4718",    # banner_text (light)
    "muted": "#7a6a48",
    "tick": "#b9ad8f",
    "gold": "#a97e10",     # ui_label (light); banner_title is too faint for a 15px wordmark on paper
    "bronze": "#c8961e",   # banner_title (light)
    "label": "#a97e10",    # ui_label (light)
    "ok": "#2e7d32",       # ui_ok (light)
    "warn": "#b45309",     # from ui_warn (light) #d97706
    "err": "#c62828",      # ui_error (light)
    "neutral": "#6b6560",
}

VERDICT_KEY = {"PASS": "ok", "PARTIAL": "warn", "FAIL": "err", "BLOCKED": "neutral", "INCONCLUSIVE": "neutral"}


def verdict_color(verdict: str, palette: dict = DARK) -> str:
    return palette[VERDICT_KEY.get(verdict, "neutral")]


def css_vars(palette: dict) -> str:
    return ";".join(f"--{k.replace('_', '-')}:{v}" for k, v in palette.items())
