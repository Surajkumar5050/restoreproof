"""Hermes tool handlers. Thin: they resolve paths and secrets, call the engine,
and shape compact results for the model. They never raise; they return JSON."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from .engine import discover as discover_mod
from .engine import store
from .engine.contract import ContractError, load_contract
from .engine.docker import Docker
from .engine.plan import preflight_plan
from .engine.report import failed_mandatory, phone_text, short_finding, stage_states, summary_text
from .engine.runner import Drill
from .engine.util import data_home, human_duration

log = logging.getLogger("restoreproof")
_RUN_LOCK = threading.Lock()


def home_dir() -> Path:
    try:
        from plugins.plugin_storage import plugin_data_dir  # Hermes' per-plugin data folder

        path = Path(plugin_data_dir("restoreproof"))
        path.mkdir(parents=True, exist_ok=True)
        return path
    except Exception:
        return data_home()


def lookup_secrets(names: list[str]) -> dict[str, str]:
    """Resolve secret references through Hermes' secret scope only. Values never reach the model."""
    out: dict[str, str] = {}
    try:
        from agent.secret_scope import get_secret
    except Exception:
        return out
    for name in names:
        try:
            value = get_secret(name)
        except Exception:
            value = None
        if value:
            out[name] = value
    return out


def _ok(payload: dict) -> str:
    return json.dumps(payload, default=str)


def _err(message: str, **extra: Any) -> str:
    return json.dumps({"error": message, **extra}, default=str)


class Tools:
    def __init__(self, ctx: Any = None) -> None:
        self.ctx = ctx

    # -- helpers --------------------------------------------------------------------------------------

    def cwd(self) -> Path:
        try:
            if self.ctx is not None:
                return Path(self.ctx.session_cwd())
        except Exception:
            pass
        return Path.cwd()

    def resolve(self, value: str | None) -> Path | None:
        if not value:
            return None
        p = Path(value).expanduser()
        return p if p.is_absolute() else (self.cwd() / p).resolve()

    def surface(self) -> str:
        try:
            return self.ctx.session_surface() if self.ctx is not None else "cli"
        except Exception:
            return "cli"

    # -- tools ----------------------------------------------------------------------------------------

    def discover(self, args: dict, **kwargs) -> str:
        try:
            project = self.resolve(args.get("project_dir")) or self.cwd()
            if not project.is_dir():
                return _err(f"project folder not found: {project}")
            result = discover_mod.discover(project, backups=args.get("backups_dir"))
            draft_path = None
            if args.get("write_draft"):
                draft_path = project / "restoreproof.draft.yaml"
                draft_path.write_text(result["contract_yaml"], encoding="utf-8")
            return _ok({
                "coverage": result["coverage_lines"],
                "uncovered_volumes": result["uncovered_volumes"],
                "reference_columns": result["reference_columns"],
                "backup_sets": result["backup_sets"],
                "draft_contract_yaml": result["contract_yaml"],
                "draft_written_to": str(draft_path) if draft_path else None,
                "next": "Show the coverage. Ask the user which customer journey proves the app works "
                        "(e.g. open a record and download its file), add it under checks.journeys, then "
                        "save as restoreproof.yaml and call restoreproof_plan.",
            })
        except Exception as exc:
            log.exception("discover failed")
            return _err(f"discovery failed: {exc}")

    def plan(self, args: dict, **kwargs) -> str:
        try:
            contract = load_contract(self.resolve(args.get("contract_path")))
        except ContractError as exc:
            return _err("contract is invalid", problems=exc.errors)
        try:
            plan = preflight_plan(contract, recovery_dir=self.resolve(args.get("recovery_point")),
                                  secrets=lookup_secrets(contract.data["service"]["secrets"]))
            return _ok(plan)
        except Exception as exc:
            log.exception("plan failed")
            return _err(f"preflight failed: {exc}")

    def run_drill(self, args: dict, **kwargs) -> str:
        try:
            contract = load_contract(self.resolve(args.get("contract_path")))
        except ContractError as exc:
            return _err("contract is invalid", problems=exc.errors)
        if not _RUN_LOCK.acquire(blocking=False):
            return _err("another RestoreProof drill is already running in this Hermes process; wait for it")
        try:
            live = LiveStatus(self.ctx, contract.name)
            drill = Drill(contract, home=home_dir(), recovery_dir=self.resolve(args.get("recovery_point")),
                          secrets=lookup_secrets(contract.data["service"]["secrets"]),
                          approved_by="Hermes approval gate (contract sha256 %s)" % contract.digest[:16],
                          trigger="cron" if _in_cron(self.ctx) else "hermes", on_event=live.update)
            ev = drill.run()
            live.finish(ev)
            return _ok(compact(ev, desktop=self.surface() == "desktop"))
        except Exception as exc:
            log.exception("drill failed")
            return _err(f"drill could not run: {exc}")
        finally:
            _RUN_LOCK.release()

    def report(self, args: dict, **kwargs) -> str:
        ev = store.load_evidence(home_dir(), args.get("run_id") or "latest")
        if not ev:
            return _err("no such drill run", hint="call restoreproof_history to list runs")
        detail = args.get("detail") or "summary"
        if detail == "full":
            return _ok(ev)
        if detail == "failures":
            failed = [c for c in ev["checks"] if c["status"] in ("FAIL", "ERROR", "SKIPPED")]
            return _ok({"run_id": ev["run_id"], "verdict": ev["verdict"], "reason": ev["verdict_reason"],
                        "failed_checks": failed, "objectives": ev.get("objectives"),
                        "report_html": ev["reports"]["html"],
                        "guidance": "Explain each failure from its evidence. Label inferences as hypotheses "
                                    "(e.g. a shared folder among missing files suggests an exclude rule or a "
                                    "failed sync for that folder). Suggest the next check or fix."})
        return _ok(compact(ev, desktop=self.surface() == "desktop"))

    def compare(self, args: dict, **kwargs) -> str:
        home = home_dir()
        a = store.load_evidence(home, args.get("before") or "previous", app=args.get("app"))
        b = store.load_evidence(home, args.get("after") or "latest", app=args.get("app"))
        if not a or not b:
            return _err("need two finished runs to compare")
        return _ok(store.compare(a, b))

    def history(self, args: dict, **kwargs) -> str:
        rows = store.history(home_dir(), app=args.get("app"), limit=int(args.get("limit") or 10))
        for r in rows:
            r["recovery_time"] = human_duration(r.get("recovery_seconds"))
        return _ok({"runs": rows})

    def cleanup(self, args: dict, **kwargs) -> str:
        try:
            return _ok(Docker().cleanup(args.get("run_id")))
        except Exception as exc:
            return _err(f"cleanup failed: {exc}")


def _in_cron(ctx: Any) -> bool:
    try:
        return ctx is not None and ctx.current_cron_execution() is not None
    except Exception:
        return False


def compact(ev: dict, desktop: bool = False) -> dict:
    """What the model needs to explain a drill, without the full evidence payload."""
    o = ev.get("objectives") or {}
    out = {
        "run_id": ev["run_id"], "app": ev["app"], "verdict": ev["verdict"], "reason": ev["verdict_reason"],
        "execution_status": ev["execution_status"],
        "recovery_point": (ev.get("recovery_point") or {}).get("label"),
        "backup_job_claimed": (ev.get("claimed_backup_status") or {}).get("status")
        if isinstance(ev.get("claimed_backup_status"), dict) else None,
        "checks": [{"stage": c["stage"], "name": c["name"], "status": c["status"], "summary": c["summary"],
                    "mandatory": c["mandatory"]} for c in ev["checks"]],
        "measured_recovery_time": human_duration(o.get("measured_recovery_seconds")),
        "rto_target": o.get("rto_target_text"), "rto_met": o.get("rto_met"),
        "rpo": human_duration(o.get("rpo_seconds")), "rpo_target": o.get("rpo_target_text"),
        "rpo_met": o.get("rpo_met"), "rpo_basis": o.get("rpo_basis"),
        "cleanup_ok": (ev.get("cleanup") or {}).get("ok"),
        "report_html": (ev.get("reports") or {}).get("html"),
        "evidence_sha256": ev.get("evidence_sha256"),
        "summary_text": summary_text(ev),
        "phone_text": phone_text(ev, title="Nightly drill" if ev.get("trigger") == "cron" else "RestoreProof"),
        "instructions": "State the verdict exactly as given. Do not call the app recovered unless verdict is PASS.",
    }
    if desktop:
        out["display"] = (f'Put this line on its own paragraph in your reply to show the evidence card: '
                          f'::restoreproof{{run="{ev["run_id"]}"}}')
    return out


class LiveStatus:
    """Posts one message when a drill starts and edits it in place as stages pass.

    Line one is short on purpose: some surfaces (the Desktop toast) show only that and fold the rest
    into one line. Line two is the six stages, so even folded it reads as progress, not a list."""

    STAGES = [("PREFLIGHT", "preflight"), ("PROVISIONING", "isolate"), ("RESTORING", "restore"),
              ("STARTING", "boot"), ("VALIDATING", "verify"), ("CLEANING", "clean up")]
    DOING = {"PREFLIGHT": "checking the backup", "PROVISIONING": "isolating", "RESTORING": "restoring",
             "STARTING": "booting", "VALIDATING": "verifying", "CLEANING": "cleaning up"}

    def __init__(self, ctx: Any, app: str) -> None:
        self.ctx = ctx
        self.app = app
        self.state = "starting"
        self.current: str | None = None
        self.marks: dict[str, str] = {}      # stage -> ✓ / ✗ once it has finished
        self.failed: set[str] = set()
        self.lines: list[str] = []           # findings, final message only
        self.handle = None
        self._post(self._text())

    def _strip(self) -> str:
        parts = []
        for key, label in self.STAGES:
            mark = self.marks.get(key) or ("●" if key == self.current else "·")
            parts.append(f"{mark} {label}")
        return "  ".join(parts)

    def _text(self) -> str:
        return "\n".join([f"RestoreProof · {self.app} · {self.state}", self._strip(), *self.lines])

    def _post(self, text: str) -> None:
        if self.ctx is None:
            return
        try:
            if self.handle is None:
                self.handle = self.ctx.post_message(text)
            elif getattr(self.handle, "ok", True):
                self.handle.edit(text)
        except Exception:
            self.handle = None

    def _close_current(self) -> None:
        if self.current and self.current not in self.marks:
            self.marks[self.current] = "✗" if self.current in self.failed else "✓"

    def update(self, event: dict) -> None:
        if event["kind"] == "stage":
            self._close_current()
            self.current = event["stage"]
            # Every stage before the current one is over, even if its own event was never seen.
            order = [key for key, _ in self.STAGES]
            if self.current in order:
                for key in order[:order.index(self.current)]:
                    self.marks.setdefault(key, "✗" if key in self.failed else "✓")
            self.state = self.DOING.get(self.current, self.current.lower())
        elif event["kind"] == "check":
            c = event["check"]
            if c.get("mandatory", True) and c["status"] in ("FAIL", "ERROR"):
                self.failed.add(c["stage"])
            return  # checks change a stage's mark, which shows when the stage closes
        else:
            return
        self._post(self._text())

    def finish(self, ev: dict) -> None:
        glyph = {"pass": "✓", "fail": "✗", "skip": "–"}
        self.marks = {key: glyph[s["state"]] for (key, _), s in zip(self.STAGES, stage_states(ev))
                      if s["state"] in glyph}
        self.current = None
        self.state = f"{ev['verdict']} in {human_duration(ev['total_seconds'])}"
        self.lines = [f"✗ {short_finding(c)}" for c in failed_mandatory(ev)[:2]] + [f"Report {ev['run_id']}"]
        self._post(self._text())
