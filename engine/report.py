"""Evidence outputs: evidence.json + evidence.sha256, report.md and report.html.

The HTML report is a case file: what the backup job claimed, what the drill found, and every
assertion behind the verdict. One sheet divided by rules, not cards. Color carries results only;
colors come from engine/palette.py (Hermes's Classic skin and its goldenrod light variant).
Courier Prime for prose, JetBrains Mono for data.
"""

from __future__ import annotations

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


def headline(ev: dict) -> str:
    """One plain sentence a non-engineer can act on. Built only from recorded facts."""
    v = ev["verdict"]
    failed = failed_mandatory(ev)
    if v == "PASS":
        return "Everything this contract requires came back: the data, the files and the customer journey."
    if v == "BLOCKED":
        return f"The drill did not run: {ev['verdict_reason']}."
    if v == "INCONCLUSIVE":
        return f"There is not enough evidence for a verdict: {ev['verdict_reason']}."
    ref = next((c for c in failed if c["kind"] == "reference"), None)
    journey = next((c for c in failed if c["kind"] == "journey"), None)
    if v == "PARTIAL":
        if ref:
            e = ref["evidence"]
            impact = f"{e['missing']:,} of {e['references']:,} files its database points to are gone"
        elif journey:
            impact = f"“{journey['name']}” fails"
        else:
            impact = f"“{failed[0]['name']}” fails" if failed else "a mandatory check fails"
        return f"The app boots, but the business does not fully come back: {impact}."
    first = failed[0] if failed else None
    if first and first["stage"] == "PREFLIGHT":
        return f"This backup set cannot rebuild the app: {first['summary']}."
    if first and first["stage"] == "STARTING":
        return "The data restores, but the application does not start from it."
    if first:
        return f"This backup cannot rebuild the app: {first['name']} failed."
    return f"This backup cannot rebuild the app: {ev['verdict_reason']}."


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
        "reason": ev["verdict_reason"], "started_at": ev.get("started_at"), "trigger": ev.get("trigger"),
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


def _finding(c: dict, missing_paths: set[str]) -> str:
    e = c.get("evidence") or {}
    title, body, note = _cap(c["name"]) + ".", "", ""
    assertion = ""
    if c["kind"] == "reference" and e.get("references"):
        folders = e.get("missing_by_folder") or []
        where = ", all from one folder" if len(folders) == 1 else (
            f", across {len(folders)} folders" if len(folders) < 10 else ", across many folders")
        title = f"{e['missing']:,} of {e['references']:,} referenced files are missing{where}."
        assertion = c["name"]
        frows = "".join(f'<tr><td>{_e(f)}/</td><td class="r bad">{n:,}</td></tr>' for f, n in folders[:6])
        if len(folders) < 10:
            frows += '<tr><td class="m">every other folder</td><td class="r m">0</td></tr>'
        sample = e.get("missing_sample") or []
        shown = sample[:6]
        rrows = "".join(f'<tr><td class="nw">{_e(m["key"])}</td><td class="m nw">{_e(m["path"])}</td></tr>'
                        for m in shown)
        rest = e["missing"] - len(shown)
        if rest > 0:
            where_rest = ("listed in evidence.json" if e["missing"] <= len(sample)
                          else f"evidence.json lists the first {len(sample)}")
            rrows += f'<tr><td colspan="2" class="m">and {rest:,} more, {where_rest}</td></tr>'
        body = (_file_strip(e, c.get("id") or "x")
                + f'<div class="pair"><div class="wrap"><table class="tb"><thead><tr><th>Folder</th>'
                  f'<th class="r">Missing</th></tr></thead><tbody>{frows}</tbody></table></div>'
                  f'<div class="wrap"><table class="tb"><thead><tr><th>Record</th><th>Expected file</th></tr></thead>'
                  f'<tbody>{rrows}</tbody></table></div></div>')
    elif c["kind"] == "journey" and e.get("steps"):
        rows = ""
        for i, s in enumerate(e["steps"], 1):
            checks = s.get("checks", [])
            good = sum(1 for k in checks if k["ok"])
            ok = bool(s.get("ok"))
            ctype = (s.get("content_type") or "").split(";")[0]
            status = s.get("status")
            rows += (f'<tr><td class="m">{i}</td><td>{_e(s.get("method"))} {_e(s.get("path"))}</td>'
                     f'<td class="r{"" if ok else " bad"}">{_e(status if status is not None else "no reply")}</td>'
                     f'<td class="m">{_e(ctype)}</td>'
                     f'<td class="r {"ok" if ok else "bad"}">{"✓" if ok else "✗"} {good} of {len(checks)}</td></tr>')
            misses = [k for k in checks if not k["ok"]]
            if misses:
                lines = "<br>".join(f'expected {_e(k["check"])}'
                                    + (f' <span class="m">{_e(k["detail"])}</span>' if k.get("detail") else "")
                                    for k in misses)
                rows += f'<tr class="why"><td></td><td colspan="4">{lines}</td></tr>'
            path = str(s.get("path") or "")
            if not ok and any(path.endswith("/" + p) for p in missing_paths):
                note = " The file it asks for is one of the missing files above."
        body = (f'<div class="inset wrap"><table class="tb journey"><thead><tr><th>#</th>'
                f'<th>Request</th><th class="r">Status</th><th>Type</th><th class="r">Checks</th></tr></thead>'
                f'<tbody>{rows}</tbody></table></div>')
    elif c["kind"] == "data" and "value" in e:
        body = (f'<div class="inset"><code>{_e(e.get("sql"))}</code><p>returned <b>{_e(e.get("value"))}</b>, '
                f'expected {_e(json.dumps(e.get("expect")))}</p></div>')
    elif e.get("logs"):
        body = f'<pre class="inset logs">{_e(e["logs"])}</pre>'
    sub = f'<span class="assert">Assertion: {_e(assertion)}</span>' if assertion else ""
    return (f'<article class="finding"><div class="fh"><span class="g bad">{GLYPH.get(c["status"], "!")}</span>'
            f'<div><h3>{_e(title)}</h3>{sub}</div></div><p class="fs">{_e(_sentence(c["summary"]))}{_e(note)}</p>'
            f'{body}</article>')


def _objectives(ev: dict, n_mand: int, n_pass: int, n_fail: int, n_skip: int) -> str:
    cl = ev.get("cleanup") or {}

    def row(name, measured, target, result, cls):
        return (f'<tr><td class="name">{_e(name)}</td><td class="r">{_e(measured)}</td>'
                f'<td class="r m">{_e(target)}</td><td class="{cls}">{_e(result)}</td></tr>')

    tone_cls = {"ok": "ok", "warn": "warn", "bad": "bad", "muted": "m"}
    rows = "".join(row(n, m, t, r, tone_cls[tone]) for n, m, t, r, tone in objective_rows(ev))

    if n_fail:
        res, cls = f"{n_fail} failed", "bad"
    elif n_skip:
        res, cls = f"{n_skip} not evaluated", "m"
    else:
        res, cls = "all passed", "ok"
    rows += row("Mandatory checks", f"{n_pass} of {n_mand}", f"{n_mand} of {n_mand}", res, cls)

    left = sum(len(x) for x in (cl.get("residual") or {}).values())
    if cl.get("ok"):
        res, cls = f"met · {cl.get('removed', 0)} temporary resources removed", "ok"
    else:
        res, cls = f"{left} still present · run hermes restoreproof cleanup", "bad"
    rows += row("Left behind after cleanup", str(left), "0", res, cls)
    return rows


def _timeline(ev: dict) -> str:
    states = stage_states(ev)
    total = ev.get("total_seconds") or sum(s["seconds"] or 0 for s in states)
    if not total or not any(s["seconds"] for s in states):
        return '<p class="m">No stage ran long enough to time.</p>'
    starts = {}
    for s in ev.get("stages", []):
        starts.setdefault(STAGE_LABEL.get(s["stage"], s["stage"]), s.get("started"))
    segs = ""
    for s in states:
        if not s["seconds"]:
            continue
        cls = {"fail": "bad", "skip": "skip"}.get(s["state"], "after" if s["label"] == "Clean up" else "done")
        segs += f'<span class="{cls}" style="flex:{s["seconds"]:.3f}" title="{_e(s["label"])} {_e(human_duration(s["seconds"]))}"></span>'
    o = ev.get("objectives") or {}
    rec = o.get("measured_recovery_seconds")
    bracket = ""
    if rec and rec <= total:
        bracket = (f'<div class="bracket"><span style="flex:{rec:.3f}">recovery time {_e(human_duration(rec))}, '
                   f'until the last mandatory check</span><span class="r" style="flex:{max(total - rec, 0.001):.3f}">'
                   f'{_e(human_duration(total))}</span></div>')
    aria = "; ".join(f"{s['label']} {human_duration(s['seconds'])}" for s in states if s["seconds"])
    rows = ""
    for key, label in STAGES:
        s = next(x for x in states if x["label"] == label)
        if key == "CLEANING":
            cl = ev.get("cleanup") or {}
            left = sum(len(x) for x in (cl.get("residual") or {}).values())
            res = (f'<span class="ok">✓</span> {cl.get("removed", 0)} removed, 0 left' if cl.get("ok")
                   else f'<span class="bad">✗</span> {left} left behind')
        else:
            items = [c for c in ev["checks"] if c["stage"] == key and c["mandatory"]]
            good = sum(1 for c in items if c["status"] == "PASS")
            glyph = {"pass": '<span class="ok">✓</span>', "fail": '<span class="bad">✗</span>',
                     "skip": '<span class="m">–</span>'}.get(s["state"], '<span class="m">·</span>')
            res = f"{glyph} {good} of {len(items)} passed" if items else '<span class="m">—</span>'
        at = starts.get(label)
        rows += (f'<tr><td class="name">{_e(label)}</td><td class="r m">{_e(f"{at:.1f}s" if at is not None else "—")}</td>'
                 f'<td class="r">{_e(human_duration(s["seconds"]) if s["seconds"] is not None else "—")}</td>'
                 f'<td>{res}</td></tr>')
    return (f'<div class="bar" role="img" aria-label="Stage durations to scale: {_e(aria)}">{segs}</div>{bracket}'
            f'<div class="wrap"><table class="tb" style="min-width:520px"><thead><tr><th>Stage</th>'
            f'<th class="r">Starts at</th><th class="r">Took</th><th>Result</th></tr></thead><tbody>{rows}</tbody>'
            f'</table></div>')


@functools.lru_cache(maxsize=1)
def _fonts() -> str:
    """Embedded Courier Prime and JetBrains Mono subsets (see scripts/build_report_fonts.py).

    The report makes no network requests; without this file it falls back to system fonts."""
    try:
        return (Path(__file__).with_name("report_fonts.css")).read_text(encoding="utf-8")
    except OSError:
        return ""


def _css() -> str:
    dark, light = css_vars(palette.DARK), css_vars(palette.LIGHT)
    return f"""
:root{{{dark};--display:"RP Courier Prime","Courier Prime","RP JetBrains Mono","Courier New",Courier,monospace;
  --mono:"RP JetBrains Mono","JetBrains Mono","Cascadia Mono",Consolas,"DejaVu Sans Mono",ui-monospace,monospace;
  color-scheme:dark}}
@media (prefers-color-scheme:light){{:root{{{light};color-scheme:light}}}}
*{{box-sizing:border-box}}
html{{background:var(--ground)}}
body{{margin:0;color:var(--ink);font:17px/1.6 var(--display);-webkit-font-smoothing:antialiased}}
.page{{max-width:1040px;margin:0 auto;padding:40px 24px 72px;display:flex;flex-direction:column;gap:52px}}
code,.mono{{font:13px/1.6 var(--mono)}}
code{{overflow-wrap:anywhere}}
.m{{color:var(--muted)}}.ink{{color:var(--ink)}}.ok{{color:var(--ok)}}.warn{{color:var(--warn)}}.bad{{color:var(--err)}}
.r{{text-align:right}}
.top{{display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:8px 24px;
  border-bottom:1px solid var(--bronze);padding-bottom:14px}}
.mark{{font:700 15px/1 var(--display);letter-spacing:.22em;color:var(--gold)}}
.mark span{{letter-spacing:0;font-weight:400;color:var(--muted);margin-left:8px}}
.top .meta{{font:13px var(--mono);color:var(--muted)}}
.case{{display:flex;flex-direction:row;flex-wrap:wrap;justify-content:space-between;align-items:flex-start;gap:28px}}
.who{{flex:999 1 520px;min-width:0;display:flex;flex-direction:column;gap:12px}}
h1{{margin:0;font:700 56px/1.05 var(--display);overflow-wrap:anywhere}}
.who p{{margin:0}}.desc{{color:var(--ink-2)}}
.kv{{font:13px/1.8 var(--mono);color:var(--muted)}}.kv b{{font-weight:400;color:var(--ink);margin-right:18px;white-space:nowrap}}
.verdict{{flex:0 0 auto;border:2px solid var(--v);padding:14px 24px 12px;display:flex;flex-direction:column;gap:4px}}
.vw{{font:700 44px/1 var(--display);letter-spacing:.08em;color:var(--v)}}
.vn{{font:12px var(--mono);color:var(--ink-2)}}
.headline{{margin:-20px 0 0;font:700 30px/1.3 var(--display);max-width:32ch}}
.claim{{display:flex;flex-direction:row;gap:0;flex-wrap:wrap;border-top:1px solid var(--rule);border-bottom:1px solid var(--rule)}}
.claim>div{{flex:1 1 300px;padding:22px 28px 22px 0;display:flex;flex-direction:column;gap:6px}}
.claim>div+div{{padding:22px 0 22px 28px;border-left:1px solid var(--rule)}}
.lbl{{font:500 12px/1.4 var(--mono);color:var(--muted)}}
.said{{font:400 36px/1.1 var(--display)}}.found{{font:700 36px/1.1 var(--display);color:var(--v)}}
.when{{font:13px var(--mono);color:var(--muted)}}
section{{display:flex;flex-direction:column;gap:16px}}
h2{{margin:0;font:700 22px/1.3 var(--display);break-after:avoid}}
h2 .n{{color:var(--label);font:600 15px var(--mono);margin-right:14px}}
.wrap{{overflow-x:auto}}
.tb{{border-collapse:collapse;width:100%}}
.tb th{{text-align:left;font:500 12px/1.4 var(--mono);color:var(--muted);padding:0 14px 8px 0;border-bottom:1px solid var(--rule-2)}}
.tb td{{padding:9px 14px 9px 0;border-bottom:1px solid var(--rule);vertical-align:top;font:13px/1.55 var(--mono);
  font-variant-numeric:tabular-nums;overflow-wrap:anywhere}}
.tb th:last-child,.tb td:last-child{{padding-right:0}}
.tb td.r,.tb th.r,.tb td.nw{{white-space:nowrap;overflow-wrap:normal}}
.tb td.name{{font:16px/1.5 var(--display)}}
.tb .adv{{color:var(--muted);margin-left:8px}}
.tb tr.why td{{color:var(--ink-2);border-bottom:0}}
.journey{{min-width:680px}}.journey td{{overflow-wrap:normal}}
.finding{{display:flex;flex-direction:column;gap:18px}}
.finding+.finding{{border-top:1px solid var(--rule);padding-top:26px}}
.fh{{display:flex;gap:14px;align-items:baseline}}
.fh .g{{font:700 18px var(--mono)}}
.fh>div{{display:flex;flex-direction:column;gap:4px}}
h3{{margin:0;font:700 22px/1.3 var(--display)}}
.assert{{font:13px var(--mono);color:var(--muted)}}
.fs,.finding>.strip,.finding>.pair,.finding>.inset{{margin:0 0 0 32px}}
.fs{{color:var(--ink-2)}}
.inset{{background:var(--inset);padding:16px 18px}}
.inset p{{margin:6px 0 0;font:13px var(--mono);color:var(--ink-2)}}
.logs{{max-height:260px;overflow:auto;white-space:pre-wrap;font:12px/1.6 var(--mono);color:var(--ink-2)}}
.strip{{display:flex;flex-direction:column;gap:8px}}
.files{{display:block;width:100%;height:auto;-webkit-print-color-adjust:exact;print-color-adjust:exact}}
.ends{{display:flex;justify-content:space-between;gap:16px;font:12px var(--mono);color:var(--muted);overflow-wrap:anywhere}}
.strip figcaption{{font:12px/1.6 var(--mono);color:var(--muted)}}
.pair{{display:flex;flex-wrap:wrap;gap:32px}}.pair>div{{flex:1 1 300px;min-width:0}}
.none{{margin:0;color:var(--ink-2)}}
.bar{{display:flex;height:14px;gap:2px;-webkit-print-color-adjust:exact;print-color-adjust:exact}}
.bar span{{min-width:2px}}.bar .done{{background:var(--muted)}}.bar .after{{background:var(--rule-2)}}
.bar .bad{{background:var(--err)}}.bar .skip{{background:transparent;border:1px dashed var(--rule-2)}}
.bracket{{display:flex;font:12px var(--mono);color:var(--muted);margin-top:-8px}}
.bracket span:first-child{{border-top:1px solid var(--muted);padding-top:6px}}.bracket span{{padding-top:7px}}
dl{{margin:8px 0 0;display:grid;grid-template-columns:minmax(0,160px) minmax(0,1fr);gap:12px 24px;font-size:16px}}
dt{{padding-top:3px}}dd{{margin:0;color:var(--ink-2);display:flex;flex-direction:column;gap:6px}}
dd code.cmd{{background:var(--inset);padding:8px 12px;align-self:flex-start}}
footer{{display:flex;flex-wrap:wrap;justify-content:space-between;gap:10px 24px;border-top:1px solid var(--bronze);
  padding-top:16px;font:12px/1.7 var(--mono);color:var(--muted)}}
@media (max-width:640px){{
  .page{{gap:40px;padding-top:28px}}h1{{font-size:38px}}.vw{{font-size:34px}}
  .headline{{font-size:23px;margin-top:-12px}}.said,.found{{font-size:28px}}
  .claim>div+div{{border-left:0;border-top:1px solid var(--rule);padding-left:0}}
  .fs,.finding>.strip,.finding>.pair,.finding>.inset{{margin-left:0}}
  dl{{grid-template-columns:1fr;gap:4px}}dd{{margin-bottom:12px}}
}}
@media print{{
  :root{{{light}}}
  html,body{{background:none}}.page{{padding:0;gap:36px;max-width:none}}
  .finding,.tb tr,dl,.case,.claim{{break-inside:avoid}}
  .wrap{{overflow:visible}}a{{color:inherit}}
}}
"""


def render_html(ev: dict, file_sha: str = "") -> str:
    v = ev["verdict"]
    claimed = ev.get("claimed_backup_status") if isinstance(ev.get("claimed_backup_status"), dict) else {}
    rp = (ev.get("recovery_point") or {}).get("label", "n/a")
    mandatory = [c for c in ev["checks"] if c["mandatory"]]
    n_pass = sum(1 for c in mandatory if c["status"] == "PASS")
    failed = failed_mandatory(ev)
    n_skip = sum(1 for c in mandatory if c["status"] == "SKIPPED")
    total = human_duration(ev.get("total_seconds"))

    if v == "PASS":
        note = f"{n_pass} of {len(mandatory)} mandatory checks passed"
    elif v == "BLOCKED":
        note = "the drill did not run"
    elif failed:
        note = f"{len(failed)} of {len(mandatory)} mandatory checks failed"
    else:
        note = f"{n_skip} of {len(mandatory)} mandatory checks not evaluated"

    kv = (f'Recovery point <b>{_e(rp)}</b>Contract <b>{_e(ev["contract"]["sha256"][:12])}</b>'
          f'Trigger <b>{_e(ev.get("trigger"))}</b>')
    if ev.get("execution_status") and ev["execution_status"] != "COMPLETED":
        kv += f'Execution <b>{_e(ev["execution_status"].lower())}</b>'

    claim = ""
    if claimed.get("status"):
        tested = (f"rebuilt from that backup and tested in {total}" if v in ("PASS", "PARTIAL", "FAIL")
                  else f"stopped after {total}")
        finished = _when(claimed.get("finished_at"))
        claim = (f'<section class="claim" aria-label="Claim against finding"><div><span class="lbl">The backup job said'
                 f'</span><span class="said">“{_e(claimed["status"])}”</span>'
                 f'<span class="when">{_e("finished " + finished if finished else "finish time not recorded")}</span>'
                 f'</div><div><span class="lbl">The drill found</span><span class="found">{_e(v)}</span>'
                 f'<span class="when">{_e(tested)}</span></div></section>')

    missing_paths = set()
    for c in failed:
        if c["kind"] == "reference":
            missing_paths.update(m["path"] for m in (c.get("evidence") or {}).get("missing_sample", []))
    findings = "".join(_finding(c, missing_paths) for c in failed)
    if not findings:
        findings = ('<p class="none"><span class="ok">✓</span> No findings. Every mandatory assertion in the '
                    'Recovery Contract passed against this backup.</p>' if v == "PASS" else
                    f'<p class="none">No mandatory check failed, but the verdict is {_e(v)}: '
                    f'{_e(ev["verdict_reason"])}.</p>')

    ledger = ""
    for c in ev["checks"]:
        bad = c["status"] in ("FAIL", "ERROR")
        gcls = {"PASS": "ok", "FAIL": "bad", "ERROR": "bad"}.get(c["status"], "m")
        adv = "" if c["mandatory"] else '<span class="adv">advisory</span>'
        ledger += (f'<tr><td class="{gcls}">{GLYPH.get(c["status"], "?")}</td>'
                   f'<td class="m nw">{_e(STAGE_LABEL.get(c["stage"], c["stage"]))}</td>'
                   f'<td><span{" class=bad" if bad else ""}>{_e(c["name"])}</span>{adv}<br>'
                   f'<span class="m">{_e(c["summary"])}</span></td>'
                   f'<td class="r m">{_e(human_duration(c["duration"])) if c.get("duration") is not None else ""}</td></tr>')

    arts = ""
    for a in ev.get("artifacts", []):
        if a.get("expected_sha256"):
            match = ('<span class="ok">matches</span>' if a["expected_sha256"] == a["sha256"]
                     else '<span class="bad">does not match</span>')
        else:
            match = '<span class="m">not recorded</span>'
        arts += (f'<tr><td>{_e(a["file"])}</td><td class="r">{a["bytes"]:,}</td>'
                 f'<td class="m" title="{_e(a["sha256"])}">{_e(a["sha256"][:16])}…</td><td>{match}</td></tr>')
    imgs = "".join(f'<tr><td>{_e(i["image"])}</td><td class="m">{_e((i.get("id") or "unknown")[:19])}…</td></tr>'
                   for i in ev.get("images", []))
    o = ev.get("objectives") or {}
    env = ev.get("environment") or {}
    prov = ""
    if arts:
        prov += (f'<div class="wrap"><table class="tb" style="min-width:600px"><thead><tr><th>Backup artifact</th>'
                 f'<th class="r">Bytes</th><th>sha256</th><th>Against the backup job\'s record</th></tr></thead>'
                 f'<tbody>{arts}</tbody></table></div>')
    if imgs:
        prov += (f'<div class="wrap"><table class="tb" style="min-width:480px"><thead><tr><th>Image</th>'
                 f'<th>Pinned digest</th></tr></thead><tbody>{imgs}</tbody></table></div>')
    prov += (f'<dl><dt class="lbl">Isolation</dt><dd><span>{_e(_cap(env.get("isolation", "not recorded")))}.</span>'
             f'<span class="m">{_e(_cap(env.get("strength", "")))}{"." if env.get("strength") else ""}</span></dd>'
             f'<dt class="lbl">Recovery time</dt><dd>{_e(_cap(o.get("rto_basis") or "not measured"))}.</dd>'
             f'<dt class="lbl">Data-loss window</dt><dd>{_e(_cap(o.get("rpo_basis") or "not measured"))}.</dd>'
             f'<dt class="lbl">Integrity</dt><dd><code>evidence.json sha256 {_e(file_sha)}</code>'
             f'<code class="cmd">sha256sum -c evidence.sha256</code></dd></dl>')

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark light">
<title>{_e(ev['app'])} · {_e(v)} · RestoreProof</title>
<style>{_fonts()}{_css()}</style></head>
<body style="--v:var(--{palette.VERDICT_KEY.get(v, 'neutral')})"><main class="page">
<header class="top"><span class="mark">RESTOREPROOF<span>recovery evidence</span></span>
<span class="meta">{_e(ev['run_id'])} · {_e(_when(ev.get('started_at')))}</span></header>

<section class="case" aria-label="Case"><div class="who"><h1>{_e(ev['app'])}</h1>
{f'<p class="desc">{_e(ev["description"])}</p>' if ev.get('description') else ''}
<p class="kv">{kv}</p></div>
<div class="verdict"><span class="vw">{_e(v)}</span><span class="vn">{_e(note)}</span></div></section>

<p class="headline">{_e(headline(ev))}</p>
{claim}

<section><h2><span class="n">01</span>Objectives</h2><div class="wrap"><table class="tb" style="min-width:520px">
<thead><tr><th>Objective</th><th class="r">Measured</th><th class="r">Target</th><th>Result</th></tr></thead>
<tbody>{_objectives(ev, len(mandatory), n_pass, len(failed), n_skip)}</tbody></table></div></section>

<section><h2><span class="n">02</span>Findings</h2>{findings}</section>

<section><h2><span class="n">03</span>The drill, to scale</h2>{_timeline(ev)}</section>

<section><h2><span class="n">04</span>Every assertion, in order</h2><div class="wrap"><table class="tb" style="min-width:600px">
<thead><tr><th style="width:18px"></th><th style="width:96px">Stage</th><th>Assertion and what was observed</th>
<th class="r" style="width:64px">Took</th></tr></thead><tbody>{ledger}</tbody></table></div></section>

<section><h2><span class="n">05</span>Provenance</h2>{prov}</section>

<footer><span>Verdicts come from deterministic assertions. <span class="ink">No language model decides a result.</span></span>
<span>RestoreProof {_e(__version__)} · a Hermes Agent plugin</span></footer>
</main></body></html>"""
