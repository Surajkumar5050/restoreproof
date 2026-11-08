"""Evidence outputs: evidence.json + evidence.sha256, report.md and report.html.

The HTML report is a case file: what the backup job claimed, what the drill found, and every
assertion behind the verdict. One sheet divided by rules, not cards. Color carries results only;
colors come from engine/palette.py (Hermes's Classic skin and its goldenrod light variant).
Courier Prime for prose, JetBrains Mono for data.
"""

from __future__ import annotations

import base64
import functools
import html
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, palette
from .palette import css_vars
from .util import canonical_json, human_duration, sha256_bytes

STAGES = [("PREFLIGHT", "Preflight"), ("PROVISIONING", "Isolate"), ("RESTORING", "Restore"),
          ("STARTING", "Boot"), ("VALIDATING", "Verify"), ("CLEANING", "Clean up")]
STAGE_LABEL = dict(STAGES)
GLYPH = {"PASS": "✓", "FAIL": "✗", "SKIPPED": "–", "ERROR": "!"}


# -- shared language -----------------------------------------------------------------------------------

def failed_mandatory(ev: dict) -> list[dict]:
    return [c for c in ev["checks"] if c["mandatory"] and c["status"] in ("FAIL", "ERROR")]


STATUS = {"PASS": "Recoverable", "PARTIAL": "Not fully recoverable", "FAIL": "Not recoverable",
          "BLOCKED": "The drill did not run", "INCONCLUSIVE": "No verdict"}


def status_phrase(verdict: str) -> str:
    """The verdict in words a person uses."""
    return STATUS.get(verdict, verdict.title())


def headline(ev: dict) -> str:
    """One plain sentence a non-engineer can act on. Built only from recorded facts."""
    v = ev["verdict"]
    failed = failed_mandatory(ev)
    if v == "PASS":
        return "Everything came back: the data, the files and the customer journey all work."
    if v == "BLOCKED":
        return f"The drill did not run: {ev['verdict_reason']}."
    if v == "INCONCLUSIVE":
        return f"There is not enough evidence for a verdict: {ev['verdict_reason']}."
    ref = next((c for c in failed if c["kind"] == "reference"), None)
    journey = next((c for c in failed if c["kind"] == "journey"), None)
    if v == "PARTIAL":
        if ref and ref.get("evidence", {}).get("references"):
            e = ref["evidence"]
            return (f"The app comes back, but {e['missing']:,} of the {e['references']:,} files it needs "
                    f"are missing from the backup.")
        if journey:
            return f"The app comes back, but a customer step fails: {journey['name']}."
        return f"The app comes back, but {failed[0]['name'] if failed else 'a required check'} fails."
    first = failed[0] if failed else None
    if first and first["stage"] == "PREFLIGHT":
        return f"This backup can't rebuild the app: {first['summary']}."
    if first and first["stage"] == "STARTING":
        return "The data restores, but the app doesn't start from it."
    if first:
        return f"This backup can't rebuild the app: {first['name']} failed."
    return f"This backup can't rebuild the app: {ev['verdict_reason']}."


def next_step(ev: dict) -> str | None:
    """The one thing to do next, from recorded facts only. Inferences are worded as likely, never certain."""
    v = ev["verdict"]
    if v == "PASS":
        return None
    if v == "BLOCKED":
        return f"{_sentence(ev['verdict_reason'])} Fix that, then run the drill again. Nothing was started."
    if v == "INCONCLUSIVE":
        return "Run the drill again. If it repeats, the technical details below show where it stopped."
    failed = failed_mandatory(ev)
    ref = next((c for c in failed if c["kind"] == "reference"), None)
    if ref:
        folders = [f for f, _ in (ref.get("evidence", {}).get("missing_by_folder") or [])]
        if len(folders) == 1:
            return (f"Every missing file is in {folders[0]}/. Your backup job is likely skipping that folder: "
                    f"check its include and exclude rules, then run this drill again.")
        if folders:
            return (f"Files are missing from {len(folders)} folders, most from {folders[0]}/. Check which "
                    f"folders your backup job includes, then run this drill again.")
    first = failed[0] if failed else None
    if first and first["stage"] == "PREFLIGHT":
        return f"{_sentence(first['summary'])} Take a fresh backup, then run this drill again."
    if first and first["stage"] == "STARTING":
        return ("The app doesn't start on the restored data. Its last log lines are under technical details; "
                "a missing setting or an incompatible schema are the usual causes.")
    journey = next((c for c in failed if c["kind"] == "journey"), None)
    if journey:
        steps = (journey.get("evidence") or {}).get("steps") or []
        bad = next((s for s in steps if not s.get("ok")), None)
        if bad:
            return (f"A customer request fails: {bad.get('method', '')} {bad.get('path', '')} returns "
                    f"{bad.get('status', 'no answer')}. Check that the backup holds what that request needs.")
    if first:
        return f"{_sentence(first['summary'])} Fix that, then run this drill again."
    return None


def objective_lines(ev: dict) -> list[str]:
    o = ev.get("objectives") or {}
    lines = []
    if o.get("measured_recovery_seconds") is not None:
        line = f"Recovery time {human_duration(o['measured_recovery_seconds'])}"
        if o.get("rto_target_text"):
            if o.get("rto_met"):
                state = "met"
            elif ev["verdict"] != "PASS":
                state = "not met: recovery incomplete"
            else:
                state = "not met"
            line += f" (target {o['rto_target_text']}, {state})"
        lines.append(line)
    if o.get("rpo_seconds") is not None:
        line = f"Data-loss window (RPO) {human_duration(o['rpo_seconds'])}"
        if o.get("rpo_target_text"):
            line += f" (target {o['rpo_target_text']}, {'met' if o.get('rpo_met') else 'not met'})"
        lines.append(line)
    elif o.get("rpo_basis"):
        lines.append(f"RPO {o['rpo_basis']}")
    return lines


def objective_rows(ev: dict) -> list[tuple[str, str, str, str, str]]:
    """(name, measured, target, result, tone) for recovery time and data-loss window.

    tone is one of ok, warn, bad, muted. Shared by the HTML report and the terminal."""
    o = ev.get("objectives") or {}
    rows = []
    rto = o.get("measured_recovery_seconds")
    if rto is None:
        res, tone = "not measured", "muted"
    elif not o.get("rto_target_text"):
        res, tone = "no target set", "muted"
    elif o.get("rto_met"):
        res, tone = "met", "ok"
    elif ev["verdict"] != "PASS":
        res, tone = "not met: recovery incomplete", "warn"
    else:
        res, tone = "not met", "bad"
    rows.append(("Recovery time", human_duration(rto) if rto is not None else "—",
                 o.get("rto_target_text") or "—", res, tone))
    rpo = o.get("rpo_seconds")
    if rpo is None:
        res, tone = (o.get("rpo_basis") or "not measured"), "muted"
    elif not o.get("rpo_target_text"):
        res, tone = "no target set", "muted"
    else:
        res, tone = ("met", "ok") if o.get("rpo_met") else ("not met", "bad")
    rows.append(("Data-loss window", human_duration(rpo) if rpo is not None else "—",
                 o.get("rpo_target_text") or "—", res, tone))
    return rows


def short_finding(c: dict) -> str:
    """One line a phone shows without wrapping much: what failed, in numbers where there are numbers."""
    e = c.get("evidence") or {}
    if c["kind"] == "reference" and e.get("references"):
        folders = e.get("missing_by_folder") or []
        where = (f" ({folders[0][0]}/)" if len(folders) == 1
                 else f" (most under {folders[0][0]}/)" if folders else "")
        return f"{e['missing']:,} of {e['references']:,} referenced files missing{where}"
    if c["kind"] == "journey" and e.get("steps"):
        bad = next((s for s in e["steps"] if not s.get("ok")), None)
        if bad:
            got = f"returns {bad['status']}" if bad.get("status") is not None else "gets no reply"
            return f"{bad.get('method', 'GET')} {bad.get('path')} {got}"
    return f"{c['name']} failed"


def phone_text(ev: dict, title: str = "RestoreProof") -> str:
    """Five or six short lines for Telegram and other chat apps: verdict first, then numbers, then the run."""
    failed = failed_mandatory(ev)
    mandatory = [c for c in ev["checks"] if c["mandatory"]]
    passed = sum(1 for c in mandatory if c["status"] == "PASS")
    out = [f"{title} · {ev['app']} · {ev['verdict']}"]
    if ev["verdict"] in ("BLOCKED", "INCONCLUSIVE"):
        out.append(headline(ev))
    out += [f"✗ {short_finding(c)}" for c in failed[:2]]
    out += objective_lines(ev)
    cl = ev.get("cleanup") or {}
    left = "Nothing left running." if cl.get("ok") else "Cleanup incomplete: run hermes restoreproof cleanup."
    out.append(f"{passed} of {len(mandatory)} checks passed. {left}")
    out.append(f"Report {ev['run_id']}")
    return "\n".join(out)


def summary_text(ev: dict) -> str:
    """Plain text for tool results, the terminal, chat apps and phones. Verdict first, facts after."""
    out = [f"RestoreProof · {ev['app']} · {ev['verdict']}", headline(ev), ""]
    failed = [c for c in ev["checks"] if c["mandatory"] and c["status"] in ("FAIL", "ERROR", "SKIPPED")]
    for c in failed[:6]:
        out.append(f"{GLYPH.get(c['status'], '?')} {c['name']}: {c['summary']}")
    passed = [c for c in ev["checks"] if c["status"] == "PASS"]
    if passed:
        kinds = []
        for kind, label in (("artifact", "artifacts"), ("data", "data"), ("files", "files"), ("service", "boot"),
                            ("reference", "file references"), ("journey", "journeys")):
            if any(c["kind"] == kind for c in passed):
                kinds.append(label)
        out.append(f"✓ {len(passed)} check{'s' if len(passed) != 1 else ''} passed"
                   + (f" ({', '.join(kinds)})" if kinds else ""))
    out.append("")
    claimed = ev.get("claimed_backup_status")
    if isinstance(claimed, dict) and claimed.get("status"):
        out.append(f"Backup job said: {claimed['status']} ({claimed.get('finished_at', 'time unknown')})")
    out += objective_lines(ev)
    cl = ev.get("cleanup") or {}
    if cl.get("ok"):
        out.append(f"Cleanup: {cl.get('removed', 0)} temporary resources removed, restored data deleted")
    else:
        out.append(f"Cleanup INCOMPLETE: still present {cl.get('residual')}; run `hermes restoreproof cleanup`")
    out.append(f"Run {ev['run_id']} · recovery point {(ev.get('recovery_point') or {}).get('label', 'n/a')}")
    return "\n".join(out)


def card(ev: dict) -> dict:
    """The compact, UI-ready view of a run shared by the Desktop pane, the inline card and tools."""
    o = ev.get("objectives") or {}
    claimed = ev.get("claimed_backup_status") if isinstance(ev.get("claimed_backup_status"), dict) else {}
    mandatory = [c for c in ev["checks"] if c["mandatory"]]
    findings = []
    for c in failed_mandatory(ev):
        e = c.get("evidence") or {}
        item = {"name": c["name"], "kind": c["kind"], "stage": STAGE_LABEL.get(c["stage"], c["stage"]),
                "summary": c["summary"]}
        if c["kind"] == "reference":
            item.update(missing=e.get("missing"), references=e.get("references"),
                        folders=(e.get("missing_by_folder") or [])[:3])
        if c["kind"] == "journey":
            item["steps"] = [{"method": s.get("method"), "path": s.get("path"), "status": s.get("status"),
                              "ok": bool(s.get("ok"))} for s in e.get("steps", [])]
        findings.append(item)
    cl = ev.get("cleanup") or {}
    return {
        "run_id": ev["run_id"], "app": ev["app"], "verdict": ev["verdict"], "headline": headline(ev),
        "status": status_phrase(ev["verdict"]), "next_step": next_step(ev), "reason": ev["verdict_reason"], "started_at": ev.get("started_at"), "trigger": ev.get("trigger"),
        "recovery_point": (ev.get("recovery_point") or {}).get("label"),
        "claimed": {"status": claimed.get("status"), "finished_at": claimed.get("finished_at")}
        if claimed.get("status") else None,
        "stages": stage_states(ev),
        "metrics": {
            "recovery": human_duration(o.get("measured_recovery_seconds")), "rto_target": o.get("rto_target_text"),
            "rto_met": o.get("rto_met"), "rpo": human_duration(o.get("rpo_seconds")),
            "rpo_target": o.get("rpo_target_text"), "rpo_met": o.get("rpo_met"),
            "total": human_duration(ev.get("total_seconds")),
        },
        "checks": {"passed": sum(1 for c in mandatory if c["status"] == "PASS"), "mandatory": len(mandatory)},
        "findings": findings,
        "cleanup": {"ok": bool(cl.get("ok")), "removed": cl.get("removed", 0)},
        "report": (ev.get("reports") or {}).get("html"),
    }


# -- files ---------------------------------------------------------------------------------------------

def evidence_digest(evidence: dict) -> str:
    """Content digest: sha256 of the canonical JSON of the evidence, excluding this field and paths."""
    body = {k: v for k, v in evidence.items() if k not in ("reports", "evidence_sha256")}
    return sha256_bytes(canonical_json(body))


def write_reports(evidence: dict, run_dir: Path) -> dict:
    paths = {"evidence": str(run_dir / "evidence.json"), "markdown": str(run_dir / "report.md"),
             "html": str(run_dir / "report.html")}
    evidence["reports"] = paths
    evidence["evidence_sha256"] = evidence_digest(evidence)
    data = json.dumps(evidence, indent=2, default=str).encode("utf-8")
    (run_dir / "evidence.json").write_bytes(data)
    file_sha = sha256_bytes(data)
    # `sha256sum -c evidence.sha256` verifies the file byte for byte.
    (run_dir / "evidence.sha256").write_text(f"{file_sha}  evidence.json\n", encoding="utf-8")
    (run_dir / "report.md").write_text(markdown(evidence, file_sha), encoding="utf-8")
    (run_dir / "report.html").write_text(render_html(evidence, file_sha), encoding="utf-8")
    return paths


def markdown(ev: dict, file_sha: str = "") -> str:
    out = [f"# {ev['app']}: {ev['verdict']}", "", f"**{headline(ev)}**", ""]
    claimed = ev.get("claimed_backup_status")
    if isinstance(claimed, dict) and claimed.get("status"):
        out += [f"> The backup job reported **{claimed['status']}** at {claimed.get('finished_at', '?')}.", ""]
    out += ["| | Stage | Check | Result |", "|---|---|---|---|"]
    for c in ev["checks"]:
        name = c["name"] + ("" if c["mandatory"] else " *(advisory)*")
        out.append(f"| {GLYPH.get(c['status'], '?')} | {STAGE_LABEL.get(c['stage'], c['stage'])} | {name} | "
                   f"{c['summary'].replace('|', '/')} |")
    out += ["", *[f"- {line}" for line in objective_lines(ev)]]
    cl = ev.get("cleanup") or {}
    out.append(f"- Cleanup: {'complete' if cl.get('ok') else 'INCOMPLETE'}, {cl.get('removed', 0)} temporary "
               f"resources removed")
    out += ["", f"Run `{ev['run_id']}` · contract `{ev['contract']['sha256'][:16]}` · "
                f"evidence.json sha256 `{file_sha}`", "",
            f"_{ev['environment']['isolation']}. {ev['environment']['strength']}._", ""]
    return "\n".join(out)


# -- html ----------------------------------------------------------------------------------------------

def _e(x) -> str:
    return html.escape(str(x if x is not None else ""))


def stage_states(ev: dict) -> list[dict]:
    durations = {}
    for s in ev.get("stages", []):
        if "ended" in s:
            durations[s["stage"]] = durations.get(s["stage"], 0) + s["ended"] - s["started"]
    out = []
    for key, label in STAGES:
        if key == "CLEANING":
            cl = ev.get("cleanup") or {}
            state = "pass" if cl.get("ok") else "fail"
        else:
            items = [c for c in ev["checks"] if c["stage"] == key and c["mandatory"]]
            if not items:
                state = "idle" if key not in durations else "pass"
            elif any(c["status"] in ("FAIL", "ERROR") for c in items):
                state = "fail"
            elif all(c["status"] == "SKIPPED" for c in items):
                state = "skip"
            else:
                state = "pass"
        out.append({"label": label, "state": state, "seconds": durations.get(key)})
    return out


def _when(iso) -> str:
    """'2026-11-07T08:42:35Z' -> '7 Nov 2026, 08:42 UTC'. Anything unparseable is shown as given."""
    if not iso:
        return ""
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return str(iso)
    if d.tzinfo is not None:
        d = d.astimezone(timezone.utc)
    return f"{d.day} {d:%b %Y, %H:%M} UTC"


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def _sentence(text: str) -> str:
    text = _cap(str(text or "").strip())
    return text if not text or text[-1] in ".!?" else text + "."


def _folder(path) -> str:
    return (str(path).rsplit("/", 1)[0] + "/") if path and "/" in str(path) else ""


def _file_strip(e: dict, uid: str) -> str:
    """Every referenced file as one square, in path order, wrapped into rows; missing files in red.

    Drawn as SVG patterns, so the size stays small however many files there are."""
    total = e.get("references") or 0
    runs = e.get("missing_runs")
    if not total or runs is None:
        return ""
    cols = max(100, math.ceil(math.sqrt(total * 8)))
    rows, cell = math.ceil(total / cols), 6
    full, rem = divmod(total, cols)
    present_rects = ""
    if full:
        present_rects += f'<rect x="0" y="0" width="{cols * cell}" height="{full * cell}" fill="url(#p{uid})"/>'
    if rem:
        present_rects += f'<rect x="0" y="{full * cell}" width="{rem * cell}" height="{cell}" fill="url(#p{uid})"/>'
    gone = ""
    for start, length in runs:
        i, end = start, min(start + length, total)
        while i < end:
            row, col = divmod(i, cols)
            take = min(end - i, cols - col)
            gone += (f'<rect x="{col * cell}" y="{row * cell}" width="{take * cell}" height="{cell}" '
                     f'fill="url(#m{uid})"/>')
            i += take
    present = total - e["missing"]
    shape = ("in one unbroken run" if len(runs) == 1
             else f"in {len(runs):,} separate runs" + (", first 200 drawn" if e.get("missing_runs_truncated") else ""))
    caption = ("Each square is one file the database points to, in path order. " if cols <= 250
               else "Every file the database points to, in path order. ")
    label = f"{total:,} referenced files in path order: {present:,} present, {e['missing']:,} missing {shape}."
    ends = ""
    if e.get("first_path") and e.get("last_path"):
        ends = (f'<div class="ends"><span>from {_e(_folder(e["first_path"]))}</span>'
                f'<span>to {_e(_folder(e["last_path"]))}</span></div>')
    svg = (f'<svg class="files" viewBox="0 0 {cols * cell} {rows * cell}" role="img" aria-label="{_e(label)}">'
           f'<defs><pattern id="p{uid}" width="{cell}" height="{cell}" patternUnits="userSpaceOnUse">'
           f'<rect width="{cell - 1}" height="{cell - 1}" style="fill:var(--tick)"/></pattern>'
           f'<pattern id="m{uid}" width="{cell}" height="{cell}" patternUnits="userSpaceOnUse">'
           f'<rect width="{cell - 1}" height="{cell - 1}" style="fill:var(--err)"/></pattern></defs>'
           f'{present_rects}{gone}</svg>')
    return (f'<figure class="strip">{svg}{ends}<figcaption>{caption}<span class="ink">{present:,} present</span>'
            f' · <span class="bad">{e["missing"]:,} missing</span>, {shape}.</figcaption></figure>')


def _fonts() -> str:
    """Embedded Courier Prime and JetBrains Mono subsets (see scripts/build_report_fonts.py).

    The report makes no network requests; without this file it falls back to system fonts."""
    try:
        return (Path(__file__).with_name("report_fonts.css")).read_text(encoding="utf-8")
    except OSError:
        return ""


PLAIN_STAGE = {"PREFLIGHT": "Check the backup", "PROVISIONING": "Seal a sandbox", "RESTORING": "Restore",
               "STARTING": "Start the app", "VALIDATING": "Try it as a customer", "CLEANING": "Clean up"}


def _say(seconds) -> str:
    """Durations the way people say them: '17 seconds', '4 minutes', '14 hours'."""
    if seconds is None:
        return "unknown"
    s = float(seconds)
    if s < 1:
        return "under a second"
    for limit, unit, size in ((60, "second", 1), (3600, "minute", 60), (172800, "hour", 3600)):
        if s < limit:
            n = round(s / size)
            return f"{n} {unit}{'' if n == 1 else 's'}"
    n = round(s / 86400)
    return f"{n} day{'' if n == 1 else 's'}"


def _art() -> str:
    """The halftone Hermes masthead (engine/report_art.jpg, built by scripts/build_halftone_art.py), base64."""
    try:
        return base64.b64encode(Path(__file__).with_name("report_art.jpg").read_bytes()).decode("ascii")
    except OSError:
        return ""


def _css() -> str:
    dark, light = css_vars(palette.DARK), css_vars(palette.LIGHT)
    return f"""
:root{{{dark};--display:"RP Courier Prime","Courier New",monospace;--mono:"RP JetBrains Mono",ui-monospace,monospace;
--sans:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}}
@media (prefers-color-scheme:light){{:root{{{light}}}}}
*{{box-sizing:border-box}}
html{{background:var(--ground)}}
body{{margin:0;color:var(--ink);font:16px/1.6 var(--sans);-webkit-font-smoothing:antialiased}}
.page{{max-width:780px;margin:0 auto;padding:0 24px 64px}}
.mast{{display:flex;height:150px;margin:0 -24px;background:var(--hb-ground);overflow:hidden}}
.mast .in{{flex:1;display:flex;flex-direction:column;justify-content:flex-end;padding:0 24px 18px;min-width:0}}
.mast img{{flex:none;height:150px;width:auto;display:block}}
.mark{{font:700 13px/1 var(--display);letter-spacing:.32em;color:var(--hb-ink)}}
.meta{{display:block;font:12px var(--mono);color:var(--hb-ink-2);margin-top:8px}}
.app{{margin-top:28px;font:14px var(--mono);color:var(--muted)}}
.status{{display:flex;align-items:center;gap:10px;margin-top:6px}}
.dot{{width:10px;height:10px;border-radius:50%;background:var(--v)}}
.phrase{{font-weight:600;color:var(--v)}}
.vw{{font:12px var(--mono);letter-spacing:.08em;color:var(--muted);border:1px solid var(--rule-2);padding:1px 6px;border-radius:4px}}
h1{{font:700 30px/1.3 var(--display);margin:12px 0 0;letter-spacing:-.01em}}
.lead{{color:var(--ink-2);margin:14px 0 0}}
.said{{font-family:var(--mono)}}
.nums{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-top:32px}}
.num{{background:var(--inset);border-radius:8px;padding:14px 16px}}
.nl{{font-size:13px;color:var(--muted)}}
.nv{{font:700 22px/1.3 var(--display);margin-top:2px}}
.nn{{font-size:12.5px;color:var(--muted);margin-top:2px}}
.fix{{margin-top:24px;border:1px solid var(--v);border-radius:8px;padding:14px 16px}}
.fix h2{{font:600 13px var(--sans);color:var(--v);margin:0;letter-spacing:.02em}}
.fix p{{margin:6px 0 0}}
.list{{list-style:none;margin:24px 0 0;padding:0}}
.list li{{display:flex;gap:10px;padding:8px 0;border-bottom:1px solid var(--rule)}}
.list li:last-child{{border-bottom:0}}
.g{{font-family:var(--mono);font-weight:700;flex:none;width:14px}}
.ok{{color:var(--ok)}}.bad{{color:var(--err)}}.mute{{color:var(--muted)}}
.fact{{display:block;font:13px/1.5 var(--mono);color:var(--muted);overflow-wrap:anywhere}}
.more{{margin-top:36px;border-top:1px solid var(--rule)}}
details{{border-bottom:1px solid var(--rule)}}
summary{{cursor:pointer;list-style:none;padding:14px 0;display:flex;gap:12px;font-weight:600}}
summary::-webkit-details-marker{{display:none}}
summary::after{{content:"+";font:400 18px/1 var(--mono);color:var(--muted)}}
details[open] summary::after{{content:"\\2013"}}
summary .s2{{font:400 13px var(--sans);color:var(--muted);margin-left:auto;padding-right:6px}}
.body{{padding:0 0 18px}}
.stage{{margin:14px 0 4px;font:600 13px var(--sans);color:var(--label)}}
.body .list{{margin-top:0}}
.strip{{margin:14px 0 0}}
.files{{width:100%;height:auto;display:block}}
.ends{{display:flex;justify-content:space-between;font:12px var(--mono);color:var(--muted);margin-top:6px}}
figcaption{{font-size:13px;color:var(--muted);margin-top:6px}}
.ink{{color:var(--ink)}}
table{{width:100%;border-collapse:collapse;font-size:13.5px}}
td{{padding:7px 8px 7px 0;border-bottom:1px solid var(--rule);vertical-align:top}}
td.k{{color:var(--muted);width:34%}}
code,.mono{{font:12.5px var(--mono);overflow-wrap:anywhere}}
.verify{{font:12.5px var(--mono);background:var(--inset);border-radius:6px;padding:8px 10px;margin-top:8px;overflow-wrap:anywhere}}
footer{{margin-top:40px;font-size:13px;color:var(--muted)}}
@media (max-width:640px){{.nums{{grid-template-columns:1fr}}h1{{font-size:24px}}.page{{padding:0 16px 48px}}.mast{{margin:0 -16px}}.mast .in{{padding:0 16px 16px}}.mast img{{height:120px}}}}
@media print{{html{{background:#fff}}}}
"""


def _check_items(checks: list[dict]) -> str:
    out = ""
    for c in checks:
        ok = c["status"] == "PASS"
        glyph = GLYPH.get(c["status"], "!")
        cls = "ok" if ok else ("mute" if c["status"] == "SKIPPED" else "bad")
        note = "" if c["mandatory"] else " (advisory)"
        out += (f'<li><span class="g {cls}">{glyph}</span><span>{_e(_cap(c["name"]))}{note}'
                f'<span class="fact">{_e(c["summary"])}</span></span></li>')
    return out


def _how_we_know(ev: dict) -> str:
    parts = ""
    for key, _ in STAGES:
        if key == "CLEANING":
            cl = ev.get("cleanup") or {}
            if not cl:
                continue
            ok = bool(cl.get("ok"))
            text = (f"{cl.get('removed', 0)} temporary resources removed and checked gone; restored data deleted"
                    if ok else f"Cleanup incomplete, still present: {cl.get('residual')}")
            mark = '<span class="g ok">✓</span>' if ok else '<span class="g bad">✗</span>'
            parts += (f'<div class="stage">{PLAIN_STAGE[key]}</div><ul class="list"><li>{mark}'
                      f'<span>{_e(text)}</span></li></ul>')
            continue
        items = [c for c in ev["checks"] if c["stage"] == key]
        if items:
            parts += f'<div class="stage">{PLAIN_STAGE[key]}</div><ul class="list">{_check_items(items)}</ul>'
    return parts


def _files_section(ev: dict) -> tuple[str, set[str]]:
    ref = next((c for c in ev["checks"] if c["kind"] == "reference" and c["status"] in ("FAIL", "ERROR")), None)
    if not ref:
        return "", set()
    e = ref.get("evidence") or {}
    if not e.get("references"):
        return "", set()
    sample = e.get("missing_sample") or []
    missing_paths = {m["path"] for m in sample}
    folders = "".join(f'<tr><td class="k"><code>{_e(f)}/</code></td><td><span class="bad">{n:,} missing</span></td></tr>'
                      for f, n in (e.get("missing_by_folder") or [])[:8])
    keys = ", ".join(_e(m["key"]) for m in sample[:12])
    more = e["missing"] - min(12, len(sample))
    records = ""
    if keys:
        records = (f'<p class="mute">Records affected: <span class="mono">{keys}</span>'
                   + (f" and {more:,} more (all listed in evidence.json)" if more > 0 else "") + "</p>")
    body = (f'<p>{e["missing"]:,} of {e["references"]:,} referenced files are missing. '
            f'{_e(_sentence(ref["name"]))}</p>'
            + _file_strip(e, "f")
            + (f"<table>{folders}</table>" if folders else "")
            + records)
    return (f'<details><summary>Which files are missing<span class="s2">{e["missing"]:,}</span></summary>'
            f'<div class="body">{body}</div></details>'), missing_paths


def _journey_section(ev: dict, missing_paths: set[str]) -> str:
    out = ""
    for c in ev["checks"]:
        steps = (c.get("evidence") or {}).get("steps") if c["kind"] == "journey" else None
        if not steps:
            continue
        rows = ""
        for st in steps:
            ok = st.get("ok")
            failed = "; ".join(x["check"] for x in st.get("checks", []) if not x.get("ok"))
            hint = ""
            path = str(st.get("path", "")).lstrip("/")
            if not ok and any(path.endswith(m) for m in missing_paths):
                hint = " The file it asks for is one of the missing files above."
            mark = '<span class="g ok">✓</span>' if ok else '<span class="g bad">✗</span>'
            fact = f'<span class="fact">expected {_e(failed)}.{_e(hint)}</span>' if failed else ""
            rows += (f'<li>{mark}<span><span class="mono">{_e(st.get("method", ""))} {_e(st.get("path", ""))}</span>'
                     f' answered {_e(st.get("status", "nothing"))}{fact}</span></li>')
        out += (f'<details><summary>{_e(_cap(c["name"]))}<span class="s2">{len(steps)} requests</span></summary>'
                f'<div class="body"><p class="mute">A test customer inside the sandbox made these requests '
                f'to the restored app:</p><ul class="list">{rows}</ul></div></details>')
    return out


def _technical(ev: dict, file_sha: str) -> str:
    o = ev.get("objectives") or {}
    rows = [("Run", f'<code>{_e(ev["run_id"])}</code> · {_e(ev.get("trigger"))} · '
                    f'{_e(str(ev.get("execution_status", "")).lower())}'),
            ("Started", _e(_when(ev.get("started_at"))))]
    for a in ev.get("artifacts", []):
        if a.get("expected_sha256"):
            match = ("matches the backup job's record" if a["expected_sha256"] == a["sha256"]
                     else "does not match the backup job's record")
        else:
            match = "no recorded digest"
        rows.append((_e(a["file"]), f'{a["bytes"]:,} bytes · <code>{_e(a["sha256"][:16])}…</code> · {match}'))
    for i in ev.get("images", []):
        rows.append(("Image", f'<code>{_e(i["image"])}</code> <span class="mute">{_e((i.get("id") or "")[:19])}</span>'))
    times = " · ".join(f"{_e(s['label'].lower())} {_e(human_duration(s['seconds']))}"
                       for s in stage_states(ev) if s["seconds"] is not None)
    if times:
        rows.append(("Timings", times))
    env = ev.get("environment") or {}
    rows += [("Isolation", f'{_e(env.get("isolation", ""))}. <span class="mute">{_e(env.get("strength", ""))}.</span>'),
             ("Recovery time", _e(o.get("rto_basis", "not measured"))),
             ("Data-loss window", _e(o.get("rpo_basis", "not measured"))),
             ("Contract", f'<code>{_e(ev["contract"]["sha256"])}</code>')]
    table = "".join(f'<tr><td class="k">{k}</td><td>{val}</td></tr>' for k, val in rows)
    verify = ""
    if file_sha:
        verify = (f'<div class="verify">evidence.json sha256 {_e(file_sha)}</div>'
                  f'<div class="verify">sha256sum -c evidence.sha256</div>')
    return (f'<details><summary>Technical details<span class="s2">inputs, images, timings, integrity</span>'
            f'</summary><div class="body"><table>{table}</table>{verify}</div></details>')


def _numbers(ev: dict, n_pass: int, n_total: int, n_fail: int) -> str:
    v = ev["verdict"]
    o = ev.get("objectives") or {}
    measured = o.get("measured_recovery_seconds")
    t1_label = "Back online in" if v == "PASS" else "Restore and checks took"
    t1_note = f"goal {o['rto_target_text']}" if o.get("rto_target_text") else "no goal set"
    if v != "PASS" and measured is not None:
        t1_note += " · not a full recovery"
    elif o.get("rto_met"):
        t1_note += " · met"
    rpo = o.get("rpo_seconds")
    if rpo is None:
        t2_value, t2_note = "unknown", "no data timestamp in the contract"
    else:
        t2_value = f"last {_say(rpo)}"
        if o.get("rpo_target_text"):
            t2_note = f"limit {o['rpo_target_text']}"
            if o.get("rpo_met") is True:
                t2_note += " · met"
            elif o.get("rpo_met") is False:
                t2_note += " · over the limit"
        else:
            t2_note = "no limit set"
    t3_note = f"{n_fail} failed" if n_fail else "all passed"
    t1_value = _say(measured) if measured is not None else "—"

    def tile(label, value, note):
        return (f'<div class="num"><div class="nl">{label}</div><div class="nv">{_e(value)}</div>'
                f'<div class="nn">{_e(note)}</div></div>')

    return ('<section class="nums">' + tile(t1_label, t1_value, t1_note)
            + tile("Data you'd lose", t2_value, t2_note)
            + tile("Checks passed", f"{n_pass} of {n_total}", t3_note) + "</section>")


def render_html(ev: dict, file_sha: str = "") -> str:
    v = ev["verdict"]
    claimed = ev.get("claimed_backup_status") if isinstance(ev.get("claimed_backup_status"), dict) else {}
    rp = (ev.get("recovery_point") or {}).get("label", "n/a")
    mandatory = [c for c in ev["checks"] if c["mandatory"]]
    n_pass = sum(1 for c in mandatory if c["status"] == "PASS")
    failed = failed_mandatory(ev)

    if claimed.get("status"):
        when = f' on {_e(_when(claimed["finished_at"]))}' if claimed.get("finished_at") else ""
        lead = (f'Your backup job reported <span class="said">“{_e(claimed["status"])}”</span>{when}. '
                "We rebuilt the app from that backup in a sealed sandbox with no internet, and tried it "
                "the way a customer would.")
    else:
        lead = (f"We rebuilt the app from backup {_e(rp)} in a sealed sandbox with no internet, and tried it "
                "the way a customer would.")

    nums = "" if v == "BLOCKED" else _numbers(ev, n_pass, len(mandatory), len(failed))
    step = next_step(ev)
    fix = f'<section class="fix"><h2>Fix this first</h2><p>{_e(step)}</p></section>' if step else ""

    if failed:
        items = "".join(f'<li><span class="g bad">✗</span><span>{_e(_cap(c["name"]))}'
                        f'<span class="fact">{_e(short_finding(c))}</span></span></li>' for c in failed)
        outcome = f'<ul class="list">{items}</ul>'
    elif v == "PASS":
        proven = [c for c in mandatory if c["kind"] in ("data", "reference", "journey")]
        items = "".join(f'<li><span class="g ok">✓</span><span>{_e(_cap(c["name"]))}</span></li>' for c in proven)
        outcome = f'<ul class="list">{items}</ul>' if items else ""
    else:
        outcome = ""

    files_html, missing_paths = _files_section(ev)
    how = ""
    if ev["checks"]:
        how = (f'<details><summary>How we know<span class="s2">every check behind the verdict</span></summary>'
               f'<div class="body">{_how_we_know(ev)}</div></details>')
    more = ('<section class="more">' + how + files_html + _journey_section(ev, missing_paths)
            + _technical(ev, file_sha) + "</section>")

    art = _art()
    art_img = f'<img src="data:image/jpeg;base64,{art}" alt="">' if art else ""
    vcolor = palette.verdict_color(v)
    vcolor_light = palette.verdict_color(v, palette.LIGHT)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(ev['app'])} · {_e(status_phrase(v))} · RestoreProof</title>
<style>{_fonts()}{_css()}
:root{{--v:{vcolor};{css_vars({('hb_' + k): val for k, val in palette.HALFTONE.items()})}}}
@media (prefers-color-scheme:light){{:root{{--v:{vcolor_light}}}}}</style></head>
<body><main class="page">
<header class="mast">{art_img}<div class="in"><span class="mark">RESTOREPROOF</span>
<span class="meta">{_e(_when(ev.get('started_at')))} · backup {_e(rp)}</span></div></header>
<div class="app">{_e(ev['app'])}</div>
<div class="status"><span class="dot"></span><span class="phrase">{_e(status_phrase(v))}</span><span class="vw">{_e(v)}</span></div>
<h1>{_e(headline(ev))}</h1>
<p class="lead">{lead}</p>
{nums}{fix}{outcome}{more}
<footer>Every verdict here comes from deterministic checks; no AI decides a result. RestoreProof {_e(__version__)}, a Hermes Agent plugin.</footer>
</main></body></html>"""
