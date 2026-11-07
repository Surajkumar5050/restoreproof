"""Run history and run-to-run comparison."""

from __future__ import annotations

import json
from pathlib import Path


def runs_dir(home: Path) -> Path:
    d = home / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_evidence(home: Path, run_id: str | None = None, app: str | None = None) -> dict | None:
    """Load one run's evidence. run_id may be 'latest', 'previous' or None (= latest)."""
    if run_id and run_id not in ("latest", "previous", "last"):
        run = runs_dir(home) / run_id
        path = run / "evidence.json"
        if not path.is_file():
            return None
        ev = json.loads(path.read_text(encoding="utf-8"))
        # Paths are derived from the run folder, so evidence stays readable if the folder moves.
        ev["reports"] = {"evidence": str(path), "markdown": str(run / "report.md"), "html": str(run / "report.html")}
        return ev
    items = history(home, app=app, limit=2)
    index = 1 if run_id == "previous" else 0
    if len(items) <= index:
        return None
    return load_evidence(home, items[index]["run_id"])


def history(home: Path, app: str | None = None, limit: int = 20) -> list[dict]:
    out = []
    for d in sorted(runs_dir(home).iterdir(), key=lambda p: p.name, reverse=True):
        ev_path = d / "evidence.json"
        if not ev_path.is_file():
            state = d / "state.json"
            if state.is_file():
                try:
                    st = json.loads(state.read_text(encoding="utf-8"))
                    if st.get("status") == "RUNNING" and (not app or st.get("app") == app):
                        out.append({"run_id": d.name, "app": st.get("app"), "verdict": "RUNNING",
                                    "stage": st.get("stage"), "started_at": st.get("started_at")})
                except ValueError:
                    pass
            continue
        try:
            ev = json.loads(ev_path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if app and ev.get("app") != app:
            continue
        o = ev.get("objectives") or {}
        out.append({
            "run_id": ev["run_id"], "app": ev["app"], "verdict": ev["verdict"],
            "reason": ev["verdict_reason"], "started_at": ev["started_at"],
            "recovery_point": (ev.get("recovery_point") or {}).get("label"),
            "recovery_seconds": o.get("measured_recovery_seconds"), "rpo_seconds": o.get("rpo_seconds"),
            # Derived from the run folder, so runs saved by older versions still point at their report.
            "trigger": ev.get("trigger"), "report": str(d / "report.html") if (d / "report.html").is_file() else None,
        })
        if len(out) >= limit:
            break
    return out


def compare(a: dict, b: dict) -> dict:
    """What changed from run a (older) to run b (newer)."""
    def by_name(ev):
        return {c["name"]: c for c in ev["checks"]}

    ca, cb = by_name(a), by_name(b)
    flips = []
    for name in sorted(set(ca) | set(cb)):
        sa, sb = ca.get(name, {}).get("status"), cb.get(name, {}).get("status")
        if sa != sb:
            flips.append({"check": name, "before": sa, "after": sb,
                          "after_summary": cb.get(name, {}).get("summary")})
    arts_a = {x["file"]: x["sha256"] for x in a.get("artifacts", [])}
    arts_b = {x["file"]: x["sha256"] for x in b.get("artifacts", [])}
    changed = [{"file": f, "before": arts_a.get(f, "")[:16], "after": arts_b.get(f, "")[:16]}
               for f in sorted(set(arts_a) | set(arts_b)) if arts_a.get(f) != arts_b.get(f)]
    oa, ob = a.get("objectives") or {}, b.get("objectives") or {}
    return {
        "before": {"run_id": a["run_id"], "verdict": a["verdict"],
                   "recovery_point": (a.get("recovery_point") or {}).get("label"),
                   "recovery_seconds": oa.get("measured_recovery_seconds")},
        "after": {"run_id": b["run_id"], "verdict": b["verdict"],
                  "recovery_point": (b.get("recovery_point") or {}).get("label"),
                  "recovery_seconds": ob.get("measured_recovery_seconds")},
        "same_contract": a["contract"]["sha256"] == b["contract"]["sha256"],
        "check_changes": flips,
        "artifact_changes": changed,
    }
