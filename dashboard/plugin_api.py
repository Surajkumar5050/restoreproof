"""Read-only backend for the RestoreProof Desktop pane and inline card.

Mounted by Hermes at /api/plugins/restoreproof/. Every route only reads run folders under this
plugin's own data directory; nothing here can start a drill or touch Docker.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException

router = APIRouter()
RUN_ID = re.compile(r"^rp-\d{8}-\d{6}-[0-9a-f]{4}$")
_PKG = "_restoreproof_engine"


def _engine():
    """Load the plugin's engine package by path (this file is imported standalone, not as part of it)."""
    if _PKG not in sys.modules:
        root = Path(__file__).resolve().parents[1] / "engine"
        spec = importlib.util.spec_from_file_location(_PKG, root / "__init__.py",
                                                      submodule_search_locations=[str(root)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[_PKG] = module
        spec.loader.exec_module(module)
    report = importlib.import_module(f"{_PKG}.report")
    store = importlib.import_module(f"{_PKG}.store")
    return report, store


def _home() -> Path:
    try:
        from plugins.plugin_storage import plugin_data_dir

        return Path(plugin_data_dir("restoreproof"))
    except Exception:
        return Path.home() / ".restoreproof"


def _check_id(run_id: str) -> str:
    if run_id in ("latest", "previous") or RUN_ID.match(run_id):
        return run_id
    raise HTTPException(status_code=400, detail="invalid run id")


@router.get("/runs")
def runs(limit: int = 20, app: str | None = None):
    _, store = _engine()
    return {"runs": store.history(_home(), app=app, limit=max(1, min(limit, 100)))}


@router.get("/live")
def live():
    """The drill running right now, if any (a state file updated in the last 20 minutes)."""
    runs_dir = _home() / "runs"
    if not runs_dir.is_dir():
        return {"run": None}
    for d in sorted(runs_dir.iterdir(), key=lambda p: p.name, reverse=True)[:5]:
        state = d / "state.json"
        if (d / "evidence.json").is_file():
            continue  # finished: its evidence is the source of truth
        if state.is_file() and time.time() - state.stat().st_mtime < 1200:
            try:
                data = json.loads(state.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if data.get("status") == "RUNNING":
                return {"run": data}
    return {"run": None}


@router.get("/runs/{run_id}")
def run(run_id: str):
    report, store = _engine()
    ev = store.load_evidence(_home(), _check_id(run_id))
    if not ev:
        raise HTTPException(status_code=404, detail="no such run")
    return report.card(ev)


@router.get("/runs/{run_id}/report")
def report_html(run_id: str):
    _, store = _engine()
    ev = store.load_evidence(_home(), _check_id(run_id))
    if not ev:
        raise HTTPException(status_code=404, detail="no such run")
    path = Path(ev["reports"]["html"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="report file is missing")
    return {"run_id": ev["run_id"], "path": str(path), "html": path.read_text(encoding="utf-8")}
