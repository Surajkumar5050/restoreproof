"""A narrow wrapper around the docker CLI.

Every resource RestoreProof creates carries the label restoreproof.run=<run id>,
so cleanup can find it by label even after a crash. No shell is involved: each
call is an argument list, never a string built from user input.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

LABEL = "restoreproof.run"


class DockerError(RuntimeError):
    pass


@dataclass
class Result:
    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0


def docker_binary() -> str | None:
    found = shutil.which("docker")
    if found:
        return found
    for candidate in (
        r"C:\Program Files\Docker\Docker\resources\bin\docker.exe",
        "/usr/local/bin/docker",
        "/opt/homebrew/bin/docker",
    ):
        if Path(candidate).exists():
            return candidate
    return None


class Docker:
    def __init__(self, binary: str | None = None) -> None:
        self.binary = binary or docker_binary()

    def available(self) -> tuple[bool, str]:
        if not self.binary:
            return False, "docker CLI not found on PATH"
        r = self.run(["version", "--format", "{{.Server.Version}}"], timeout=20)
        if not r.ok:
            return False, "docker engine is not reachable: " + (r.err.strip() or r.out.strip())[:300]
        return True, r.out.strip()

    def run(self, args: Sequence[str], *, timeout: float = 120, stdin: bytes | None = None,
            stdin_file: Path | None = None) -> Result:
        if not self.binary:
            return Result(127, "", "docker CLI not found")
        try:
            if stdin_file is not None:
                with open(stdin_file, "rb") as fh:
                    proc = subprocess.run([self.binary, *args], stdin=fh, capture_output=True, timeout=timeout)
            else:
                proc = subprocess.run([self.binary, *args], input=stdin, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return Result(124, "", f"docker {args[0] if args else ''} timed out after {timeout:.0f}s")
        except OSError as exc:
            return Result(126, "", f"could not run docker: {exc}")
        return Result(proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace"))

    # -- images ---------------------------------------------------------------------------------------

    def image_info(self, image: str) -> dict | None:
        r = self.run(["image", "inspect", image, "--format", "{{json .}}"], timeout=30)
        if not r.ok:
            return None
        try:
            data = json.loads(r.out)
        except json.JSONDecodeError:
            return None
        return {
            "image": image,
            "id": data.get("Id"),
            "repo_digests": data.get("RepoDigests") or [],
            "created": data.get("Created"),
            "size": data.get("Size"),
        }

    # -- lifecycle ------------------------------------------------------------------------------------

    def resources(self, run_id: str | None = None) -> dict[str, list[str]]:
        flt = f"label={LABEL}" + (f"={run_id}" if run_id else "")
        found: dict[str, list[str]] = {}
        for kind, args in (
            ("containers", ["ps", "-a", "--filter", flt, "--format", "{{.Names}}"]),
            ("networks", ["network", "ls", "--filter", flt, "--format", "{{.Name}}"]),
            ("volumes", ["volume", "ls", "--filter", flt, "--format", "{{.Name}}"]),
        ):
            r = self.run(args, timeout=30)
            found[kind] = [line for line in r.out.split() if line] if r.ok else []
        return found

    def stream(self, args: Sequence[str], *, stdin: bytes, timeout: float,
               on_line: Callable[[str], None]) -> Result:
        """Like run(), but hands each stdout line to on_line as it arrives (for live progress).

        The full output is still returned, so callers parse the same result either way."""
        if not self.binary:
            return Result(127, "", "docker CLI not found")
        try:
            proc = subprocess.Popen([self.binary, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
        except OSError as exc:
            return Result(126, "", f"could not run docker: {exc}")
        out: list[bytes] = []
        err: list[bytes] = []

        def read_out() -> None:
            for raw in iter(proc.stdout.readline, b""):
                out.append(raw)
                try:
                    on_line(raw.decode("utf-8", "replace"))
                except Exception:
                    pass

        def read_err() -> None:
            err.append(proc.stderr.read())

        readers = [threading.Thread(target=read_out, daemon=True), threading.Thread(target=read_err, daemon=True)]
        for t in readers:
            t.start()
        try:
            proc.stdin.write(stdin)
            proc.stdin.close()
        except OSError:
            pass
        def finish() -> None:
            for t in readers:
                t.join(timeout=5)
            for pipe in (proc.stdout, proc.stderr):
                try:
                    pipe.close()
                except OSError:
                    pass

        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            finish()
            return Result(124, b"".join(out).decode("utf-8", "replace"),
                          f"docker {args[0] if args else ''} timed out after {timeout:.0f}s")
        finish()
        return Result(proc.returncode, b"".join(out).decode("utf-8", "replace"),
                      b"".join(err).decode("utf-8", "replace"))

    def cleanup(self, run_id: str | None = None,
                on_removed: Callable[[str, list[str]], None] | None = None) -> dict:
        """Remove every labelled resource, then look again to prove nothing is left.

        on_removed(kind, names) is called after each removal command succeeds (for live progress);
        the returned record, built from the second listing, stays the proof."""
        before = self.resources(run_id)
        errors: list[str] = []

        def removed(kind: str, names: list[str]) -> None:
            if on_removed is not None:
                try:
                    on_removed(kind, names)
                except Exception:
                    pass

        if before["containers"]:
            r = self.run(["rm", "-f", "-v", *before["containers"]], timeout=120)
            if not r.ok:
                errors.append(r.err.strip()[:300])
            else:
                removed("containers", before["containers"])
        for net in before["networks"]:
            r = self.run(["network", "rm", net], timeout=60)
            if not r.ok:
                errors.append(r.err.strip()[:300])
            else:
                removed("networks", [net])
        if before["volumes"]:
            r = self.run(["volume", "rm", "-f", *before["volumes"]], timeout=60)
            if not r.ok:
                errors.append(r.err.strip()[:300])
            else:
                removed("volumes", before["volumes"])
        after = self.resources(run_id)
        removed = sum(len(v) for v in before.values()) - sum(len(v) for v in after.values())
        return {
            "removed": removed,
            "created": before,
            "residual": {k: v for k, v in after.items() if v},
            "errors": errors,
            "ok": not any(after.values()),
        }
