"""One-time setup for the Billing Portal demo.

    python examples/billing-portal/setup_demo.py

1. Builds the demo app image (restoreproof-demo/billing-portal:1.1).
2. Pulls postgres:16-alpine and python:3.12-alpine (the drill never pulls on its own).
3. Writes two backup sets under examples/billing-portal/backups/.

Then:  hermes restoreproof run examples/billing-portal/restoreproof.yaml
"""

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))

from engine.docker import docker_binary  # noqa: E402


def step(title: str, args: list[str]) -> None:
    print(f"\n-> {title}", flush=True)
    result = subprocess.run(args)
    if result.returncode != 0:
        sys.exit(f"   failed: {' '.join(args)}")


def main() -> None:
    docker = docker_binary()
    if not docker:
        sys.exit("Docker is required: install Docker Desktop (or Docker Engine) and try again.")
    step("Building the demo app image", [docker, "build", "-q", "-t", "restoreproof-demo/billing-portal:1.1",
                                         str(HERE / "app")])
    step("Pulling postgres:16-alpine", [docker, "pull", "-q", "postgres:16-alpine"])
    step("Pulling python:3.12-alpine (journey probe)", [docker, "pull", "-q", "python:3.12-alpine"])
    step("Writing backup sets", [sys.executable, str(HERE / "make_backups.py"), str(HERE / "backups")])
    print("\nReady. Try the broken backup first:\n"
          "  hermes restoreproof run examples/billing-portal/restoreproof.yaml\n"
          "then the fixed one:\n"
          "  hermes restoreproof run examples/billing-portal/restoreproof.yaml "
          "--recovery-point examples/billing-portal/backups/nightly-2026-11-07")


if __name__ == "__main__":
    main()
