"""`/drill demo`: from nothing to a real drill in one command, for a first try.

Only runs when someone types `/drill demo` (optionally `fixed` and a database: postgres, mysql,
mariadb, mongodb or sqlite). It prepares what the Billing Portal example needs, each step skipped
when already done:

  1. builds the demo app image (restoreproof-demo/billing-portal:1.1) from examples/billing-portal/app
  2. pulls the database image and python:3.12-alpine if they are not on this machine
  3. writes the two demo backup sets under the plugin's data folder (never inside the plugin)

then the caller runs an ordinary drill against the broken night or the fixed one.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .engine import databases
from .engine.docker import Docker

HERE = Path(__file__).resolve().parent
EXAMPLE = HERE / "examples" / "billing-portal"
DEMO_IMAGE = "restoreproof-demo/billing-portal:1.1"
PROBE_IMAGE = "python:3.12-alpine"
ENGINES = tuple(databases.ENGINES)
NIGHTS = {"broken": "nightly-2026-11-06", "fixed": "nightly-2026-11-07"}


class DemoError(RuntimeError):
    pass


def contract(engine: str = "postgres") -> Path:
    return EXAMPLE / ("restoreproof.yaml" if engine == "postgres" else f"restoreproof.{engine}.yaml")


CONTRACT = contract()


def prepare(home: Path, docker: Docker | None = None, engine: str = "postgres") -> Path:
    """Make sure the demo can run on `engine`; return the folder that holds its backup sets."""
    docker = docker or Docker()
    ok, detail = docker.available()
    if not ok:
        raise DemoError(f"Docker is needed and is not running ({detail}). Start Docker Desktop, then try again.")
    if docker.image_info(DEMO_IMAGE) is None:
        r = docker.run(["build", "-q", "-t", DEMO_IMAGE, str(EXAMPLE / "app")], timeout=600)
        if not r.ok:
            raise DemoError(f"Could not build the demo app image: {(r.err or r.out).strip()[:300]}")
    for image in dict.fromkeys((databases.get(engine).default_image, PROBE_IMAGE)):
        if docker.image_info(image) is None:
            r = docker.run(["pull", "-q", image], timeout=900)
            if not r.ok:
                raise DemoError(f"Could not pull {image}: {(r.err or r.out).strip()[:300]}")
    backups = home / "demo" / engine
    if not all((backups / night / "backup-status.json").is_file() for night in NIGHTS.values()):
        backups.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run([sys.executable, str(EXAMPLE / "make_backups.py"), str(backups), "--engine", engine],
                              capture_output=True, timeout=600)
        if proc.returncode != 0:
            raise DemoError("Could not write the demo backups: "
                            + proc.stderr.decode("utf-8", "replace").strip()[-300:])
    return backups


def recovery_point(backups: Path, which: str) -> Path:
    return backups / NIGHTS["fixed" if which == "fixed" else "broken"]
