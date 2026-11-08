"""The recovery drill: a fixed sequence of deterministic stages.

    PREFLIGHT -> PROVISIONING -> RESTORING -> STARTING -> VALIDATING -> CLEANING -> REPORTING

Execution status (did the drill run?) and the recovery verdict (did the app come
back?) are separate fields. Cleanup runs on every exit path, including errors,
timeouts and Ctrl+C.
"""

from __future__ import annotations

import gzip
import json
import secrets as pysecrets
import shutil
import time
import traceback
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from . import __version__, archive
from .contract import Contract
from .docker import LABEL, Docker
from .util import (Redactor, Stopwatch, human_duration, iso, parse_time, sha256_file, tail,
                   utcnow)

PROBE_IMAGE = "python:3.12-alpine"
PROBE_SOURCE = (Path(__file__).parent / "probe.py").read_text(encoding="utf-8")
SEP = "\x1f"

PASS, FAIL, SKIP, ERROR = "PASS", "FAIL", "SKIPPED", "ERROR"


class Blocked(Exception):
    """The drill cannot safely proceed (missing permission, secret, image or tool)."""


class DrillTimeout(Exception):
    pass


def contained(base: Path, path: Path) -> Path | None:
    """path resolved, or None if it resolves outside base (via '..', an absolute path or a link)."""
    base_r, path_r = base.resolve(), path.resolve()
    try:
        path_r.relative_to(base_r)
    except ValueError:
        return None
    return path_r


def new_run_id() -> str:
    now = utcnow()
    return f"rp-{now:%Y%m%d-%H%M%S}-{pysecrets.token_hex(2)}"


class Drill:
    def __init__(self, contract: Contract, *, home: Path, recovery_dir: str | Path | None = None,
                 secrets: dict[str, str] | None = None, approved_by: str | None = None,
                 trigger: str = "manual", on_event: Callable[[dict], None] | None = None,
                 docker: Docker | None = None, run_id: str | None = None) -> None:
        self.c = contract
        self.d = contract.data
        self.home = home
        self.recovery_dir = contract.recovery_dir(recovery_dir).resolve()
        self.secret_values = dict(secrets or {})
        self.approved_by = approved_by
        self.trigger = trigger
        self.on_event = on_event
        self.docker = docker or Docker()
        self.run_id = run_id or new_run_id()
        self.short = self.run_id.replace("rp-", "").replace("-", "")[-10:]
        self.run_dir = home / "runs" / self.run_id
        self.scratch = self.run_dir / "scratch"
        self.net = f"rp-{self.short}-net"
        self.db_name = f"rp-{self.short}-db"
        self.app_name = f"rp-{self.short}-app"
        self.probe_name = f"rp-{self.short}-probe"
        self.db_password = pysecrets.token_urlsafe(18)
        self.redact = Redactor([self.db_password, *self.secret_values.values()])
        self.clock = Stopwatch()
        self.started = utcnow()
        self.checks: list[dict] = []
        self.stages: list[dict] = []
        self.inputs: dict[str, Any] = {"artifacts": [], "images": [], "claimed_status": None}
        self.manifests: dict[str, dict] = {}
        self.failed_artifacts: set[str] = set()  # "database" or a files[].name that failed preflight
        self.state = "PREFLIGHT"
        self.last_mandatory_at: float | None = None
        self.notes: list[str] = []

    # -- events and state -----------------------------------------------------------------------------

    def emit(self, kind: str, *, _state: bool = True, **data: Any) -> None:
        """Append one event to events.jsonl. Progress events pass _state=False so frequent ticks do not
        rewrite state.json each time."""
        event = {"ts": iso(utcnow()), "t": round(self.clock.elapsed(), 3), "run_id": self.run_id,
                 "kind": kind, **data}
        try:
            with open(self.run_dir / "events.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, default=str) + "\n")
        except OSError:
            pass
        if _state:
            self._write_state()
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:
                pass

    def _write_state(self, final: dict | None = None) -> None:
        if final is not None:
            self.final_state = final
        state = getattr(self, "final_state", None) or {
            "run_id": self.run_id, "app": self.c.name, "status": "RUNNING", "stage": self.state,
            "started_at": iso(self.started), "elapsed": round(self.clock.elapsed(), 2),
            "checks": [{k: c[k] for k in ("id", "stage", "name", "status", "summary", "mandatory")}
                       for c in self.checks],
        }
        tmp = self.run_dir / "state.json.tmp"
        try:
            tmp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
            tmp.replace(self.run_dir / "state.json")
        except OSError:
            pass

    def stage(self, name: str) -> None:
        if self.stages and "ended" not in self.stages[-1]:
            self.stages[-1]["ended"] = round(self.clock.elapsed(), 3)
        self.state = name
        self.stages.append({"stage": name, "started": round(self.clock.elapsed(), 3)})
        self.emit("stage", stage=name)
        if self.clock.elapsed() > self.d["limits"]["timeout"]:
            raise DrillTimeout(f"drill exceeded its {human_duration(self.d['limits']['timeout'])} time limit")

    def record(self, stage: str, name: str, status: str, summary: str, *, kind: str = "system",
               mandatory: bool = True, evidence: dict | None = None, started: float | None = None) -> dict:
        now = self.clock.elapsed()
        chk = {
            "id": f"{stage.lower()}-{len(self.checks) + 1}", "stage": stage, "kind": kind, "name": name,
            "status": status, "summary": self.redact(summary), "mandatory": mandatory,
            "at": round(now, 3), "duration": round(now - started, 3) if started is not None else None,
            "evidence": json.loads(self.redact(json.dumps(evidence or {}, default=str))),
        }
        self.checks.append(chk)
        if mandatory and status in (PASS, FAIL, ERROR):
            self.last_mandatory_at = now
        self.emit("check", check={k: chk[k] for k in ("id", "stage", "name", "status", "summary", "mandatory",
                                                       "kind", "duration")})
        return chk

    # -- live view events -----------------------------------------------------------------------------
    # Additive, for the Desktop live drill view: what the drill is doing behind each check. They never
    # feed the verdict, and every value is a fact the engine just observed.

    def resource(self, action: str, rtype: str, role: str, name: str, image: str | None = None) -> None:
        """action: created | started | removed. rtype: network | container | volume | scratch."""
        self.emit("resource", action=action, type=rtype, role=role, name=name,
                  **({"image": image} if image else {}))

    def file_progress(self, name: str, unit: str = "files"):
        """A throttled counter: about 40 events per archive, plus the last file. unit is "files" while
        extracting and "checksummed" while hashing the restored files."""
        last = {"n": 0}

        def tick(done: int, total: int) -> None:
            step = max(1, total // 40)
            if done == total or done - last["n"] >= step:
                last["n"] = done
                self.emit("progress", _state=False, what=name, unit=unit, done=done, total=total)
        return tick

    def journey_line(self, check: str, total: int):
        """Turn the probe's streamed lines into journey events while the request is in flight."""
        def on_line(line: str) -> None:
            line = line.strip()
            if not line.startswith("{"):
                return
            try:
                msg = json.loads(line)
            except ValueError:
                return
            if "request" in msg:
                r = msg["request"]
                self.emit("journey", _state=False, phase="request", check=check, index=r.get("index"),
                          total=total, method=r.get("method"), path=self.redact(str(r.get("path"))))
            elif "response" in msg:
                r = msg["response"]
                self.emit("journey", _state=False, phase="response", check=check, index=r.get("index"),
                          total=total, status=r.get("status"), elapsed=r.get("elapsed"), ok=bool(r.get("ok")),
                          bytes=r.get("bytes"), content_type=(r.get("content_type") or "").split(";")[0],
                          failed=[self.redact(str(f)) for f in r.get("failed") or []],
                          **({"error": self.redact(str(r["error"]))} if r.get("error") else {}))
        return on_line

    def cleanup_removed(self, kind: str, names: list[str]) -> None:
        rtype = {"containers": "container", "networks": "network", "volumes": "volume"}.get(kind, kind)
        roles = {self.db_name: "database", self.app_name: "app", self.probe_name: "probe", self.net: "network"}
        for name in names:
            self.resource("removed", rtype, roles.get(name, rtype), name)

    # -- helpers --------------------------------------------------------------------------------------

    def dk(self, args: list[str], timeout: float = 120, **kw):
        return self.docker.run(args, timeout=timeout, **kw)

    def labels(self) -> list[str]:
        return ["--label", f"{LABEL}={self.run_id}"]

    def psql(self, sql: str, timeout: float = 120):
        # Over the container's local socket (trust auth inside the throwaway container), so no
        # password ever appears in a command line.
        db = self.d["database"]
        return self.dk(["exec", self.db_name, "psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-At", "-F", SEP,
                        "-U", db["user"], "-d", db["name"], "-c", sql], timeout=timeout)

    def env_file(self, name: str, values: dict[str, str]) -> Path:
        """Write container environment to a private file (passed with --env-file, deleted with the
        scratch folder) so secret values never appear in process listings."""
        for key, value in values.items():
            if "\n" in value or "\r" in value:
                raise Blocked(f"environment value for {key} contains a newline, which docker env files cannot carry")
        path = self.scratch / f".env-{name}"
        path.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return path

    def probe(self, job: dict, timeout: float, on_line: Callable[[str], None] | None = None):
        args = ["exec", "-i", self.probe_name, "python", "-c", PROBE_SOURCE]
        if on_line is not None:
            # Same probe, same single call; it also prints each request and response as it happens.
            r = self.docker.stream(args, stdin=(json.dumps({**job, "stream": True}) + "\n").encode(),
                                   timeout=timeout, on_line=on_line)
        else:
            r = self.dk(args, stdin=(json.dumps(job) + "\n").encode(), timeout=timeout)
        if not r.ok:
            return None, self.redact(r.err.strip() or r.out.strip())
        lines = [line for line in r.out.splitlines() if line.strip()]
        try:
            return json.loads(lines[-1] if lines else r.out), None
        except json.JSONDecodeError:
            return None, "probe returned unreadable output: " + self.redact(r.out[:300])

    def resolve_env(self) -> dict[str, str]:
        db = self.d["database"]
        values = {"db.host": self.db_name, "db.port": "5432", "db.user": db["user"],
                  "db.password": self.db_password, "db.name": db["name"]}
        values.update({f"secret.{k}": v for k, v in self.secret_values.items()})
        out = {}
        for key, template in self.d["service"]["env"].items():
            text = template
            for ref, val in values.items():
                text = text.replace("{" + ref + "}", val)
            out[key] = text
        return out

    # -- stages ---------------------------------------------------------------------------------------

    def preflight(self) -> None:
        self.stage("PREFLIGHT")
        t = self.clock.elapsed()
        ok, info = self.docker.available()
        if not ok:
            raise Blocked(info)
        self.inputs["docker"] = info

        missing = [s for s in self.d["service"]["secrets"] if not self.secret_values.get(s)]
        if missing:
            raise Blocked("required secret references are not available: " + ", ".join(missing)
                          + " (values are never guessed or printed)")

        if not self.recovery_dir.is_dir():
            raise Blocked(f"recovery point directory not found: {self.recovery_dir}")
        claimed = None
        status_name = self.d["recovery_point"].get("status_file")
        if (status_name and contained(self.recovery_dir, self.recovery_dir / status_name) is not None
                and (self.recovery_dir / status_name).is_file()):
            try:
                claimed = json.loads((self.recovery_dir / status_name).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                claimed = {"unreadable": True}
        self.inputs["claimed_status"] = claimed
        # The contract's label names its default backup set; any other folder is named by itself.
        is_default = self.recovery_dir == self.c.recovery_dir(None).resolve()
        label = (self.d["recovery_point"].get("label") if is_default else None) or self.recovery_dir.name
        self.inputs["recovery_point"] = {"label": label, "dir": str(self.recovery_dir)}
        claimed_arts = (claimed or {}).get("artifacts", {}) if isinstance(claimed, dict) else {}

        # Database artifact
        db = self.d["database"]
        db_path = self.recovery_dir / db["artifact"]
        self._artifact_check("database", db_path, db.get("sha256") or claimed_arts.get(db["artifact"], {}).get("sha256"),
                             t)
        self._db_artifact_integrity(db_path)

        # File archives
        for f in self.d["files"]:
            path = self.recovery_dir / f["artifact"]
            self._artifact_check(f["name"], path, f.get("sha256") or claimed_arts.get(f["artifact"], {}).get("sha256"), t)
            if path.is_file() and contained(self.recovery_dir, path) is not None:
                started = self.clock.elapsed()
                rep = archive.inspect(path, max_bytes=self.d["limits"]["max_archive_bytes"],
                                      max_members=self.d["limits"]["max_archive_members"])
                if not rep.ok:
                    self.failed_artifacts.add(f["name"])
                self.record("PREFLIGHT", f"{f['name']} archive is safe to extract",
                            PASS if rep.ok else FAIL,
                            f"{rep.files} files, {rep.total_bytes:,} bytes, no unsafe members" if rep.ok
                            else "rejected before extraction: " + "; ".join(rep.problems[:3]),
                            kind="artifact", started=started,
                            evidence={"members": rep.members, "files": rep.files,
                                      "bytes": rep.total_bytes, "problems": rep.problems})
                need = rep.total_bytes * 1.2 + 50 * 1024 * 1024
                free = shutil.disk_usage(self.home).free
                if free < need:
                    raise Blocked(f"not enough free disk for {f['name']}: need {need / 1e9:.1f} GB, have {free / 1e9:.1f} GB")

        # Images (never pulled implicitly: a drill does not reach the network on its own)
        for image in (db["image"], self.d["service"]["image"], PROBE_IMAGE):
            info = self.docker.image_info(image)
            if info is None:
                raise Blocked(f"image '{image}' is not present locally. Pull or build it first "
                              f"(docker pull {image}); RestoreProof never pulls images during a drill.")
            self.inputs["images"].append(info)
        self.record("PREFLIGHT", "container images are present and pinned by digest", PASS,
                    ", ".join(f"{i['image']} ({(i['id'] or '')[7:19]})" for i in self.inputs["images"]),
                    kind="artifact", evidence={"images": self.inputs["images"]})

    def _artifact_check(self, name: str, path: Path, expected: str | None, started: float) -> None:
        if contained(self.recovery_dir, path) is None:
            # A drill reads only the backup folder it was pointed at.
            self.failed_artifacts.add(name)
            self.record("PREFLIGHT", f"{name} artifact exists", FAIL,
                        f"refused: {path.name} is outside the backup folder", kind="artifact",
                        evidence={"path": str(path)})
            self.emit("artifact", name=name, file=path.name, present=False)
            return
        if not path.is_file():
            self.failed_artifacts.add(name)
            self.record("PREFLIGHT", f"{name} artifact exists", FAIL, f"missing from the backup set: {path.name}",
                        kind="artifact", evidence={"path": str(path)})
            self.emit("artifact", name=name, file=path.name, present=False)
            return
        digest = sha256_file(path)
        size = path.stat().st_size
        art = {"name": name, "file": path.name, "path": str(path), "bytes": size, "sha256": digest,
               "expected_sha256": expected}
        self.inputs["artifacts"].append(art)
        if expected and expected.lower() != digest:
            self.failed_artifacts.add(name)
            self.record("PREFLIGHT", f"{name} artifact matches its recorded digest", FAIL,
                        f"sha256 mismatch: expected {expected[:12]}..., got {digest[:12]}...",
                        kind="artifact", evidence=art)
        else:
            note = "matches the digest the backup job recorded" if expected else "readable (no recorded digest to compare)"
            self.record("PREFLIGHT", f"{name} artifact is present and intact", PASS,
                        f"{path.name}, {size:,} bytes, sha256 {digest[:12]}..., {note}", kind="artifact",
                        evidence=art)
        self.emit("artifact", name=name, file=path.name, present=True, bytes=size, sha256=digest[:12],
                  digest=("unrecorded" if not expected else "match" if expected.lower() == digest else "mismatch"))

    def _db_artifact_integrity(self, path: Path) -> None:
        if "database" in self.failed_artifacts or not path.is_file():
            return
        started = self.clock.elapsed()
        name = path.name.lower()
        try:
            if name.endswith(".gz"):
                total = 0
                with gzip.open(path, "rb") as fh:
                    while chunk := fh.read(1 << 20):
                        total += len(chunk)
                detail = f"gzip stream is complete ({total:,} bytes of SQL)"
            elif name.endswith((".dump", ".pgdump")):
                with open(path, "rb") as fh:
                    if fh.read(5) != b"PGDMP":
                        raise ValueError("not a pg_dump custom-format archive (missing PGDMP header)")
                detail = "pg_dump custom-format header present"
            else:
                detail = "plain SQL file"
            self.record("PREFLIGHT", "database dump format is readable", PASS, detail, kind="artifact",
                        started=started)
        except (OSError, EOFError, ValueError, gzip.BadGzipFile) as exc:
            self.failed_artifacts.add("database")
            self.record("PREFLIGHT", "database dump format is readable", FAIL, f"corrupt dump: {exc}",
                        kind="artifact", started=started)

    def provision(self) -> None:
        self.stage("PROVISIONING")
        started = self.clock.elapsed()
        self.scratch.mkdir(parents=True, exist_ok=True)
        r = self.dk(["network", "create", "--internal", *self.labels(), self.net], timeout=60)
        if not r.ok:
            raise RuntimeError("could not create the isolated network: " + r.err.strip())
        self.resource("created", "network", "network", self.net)
        self.record("PROVISIONING", "isolated network created (internal, no internet egress)", PASS,
                    f"docker network {self.net} --internal", started=started,
                    evidence={"network": self.net, "internal": True})

    def restore_database(self) -> bool:
        self.stage("RESTORING")
        db = self.d["database"]
        started = self.clock.elapsed()
        if "database" in self.failed_artifacts:
            self.record("RESTORING", "database restored from backup", FAIL,
                        "not attempted: the database artifact failed preflight", kind="data", started=started)
            return False
        envf = self.env_file("db", {"POSTGRES_PASSWORD": self.db_password, "POSTGRES_USER": db["user"],
                                    "POSTGRES_DB": db["name"]})
        r = self.dk(["run", "-d", "--name", self.db_name, "--network", self.net, *self.labels(),
                     "--memory", self.d["limits"]["db_memory"], "--pids-limit", "256",
                     "--security-opt", "no-new-privileges", "--env-file", str(envf), db["image"]], timeout=120)
        if not r.ok:
            self.record("RESTORING", "temporary database started", FAIL, self.redact(r.err.strip()[:400]),
                        kind="data", started=started)
            return False
        self.resource("started", "container", "database", self.db_name, db["image"])
        ready = False
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            # TCP readiness only succeeds after the image's init phase (its temporary server is socket-only).
            if self.dk(["exec", self.db_name, "pg_isready", "-q", "-h", "127.0.0.1", "-U", db["user"]],
                       timeout=10).ok:
                ready = True
                break
            time.sleep(0.5)
        if not ready:
            logs = self.dk(["logs", "--tail", "30", self.db_name], timeout=20)
            self.record("RESTORING", "temporary database started", FAIL, "database did not become ready in 90s",
                        kind="data", started=started, evidence={"logs": self.redact(tail(logs.out + logs.err))})
            return False

        art = self.recovery_dir / db["artifact"]
        name = art.name.lower()
        if name.endswith(".gz"):
            cmd = ["exec", "-i", self.db_name, "sh", "-c",
                   f"gunzip -c | psql -X -q -v ON_ERROR_STOP=1 -U {db['user']} -d {db['name']}"]
            r = self.dk(cmd, timeout=self.d["limits"]["timeout"], stdin_file=art)
        elif name.endswith(".sql"):
            cmd = ["exec", "-i", self.db_name, "psql", "-X", "-q", "-v", "ON_ERROR_STOP=1",
                   "-U", db["user"], "-d", db["name"]]
            r = self.dk(cmd, timeout=self.d["limits"]["timeout"], stdin_file=art)
        else:
            cp = self.dk(["cp", str(art), f"{self.db_name}:/tmp/restore.dump"], timeout=300)
            if not cp.ok:
                self.record("RESTORING", "database restored from backup", FAIL, cp.err.strip()[:400],
                            kind="data", started=started)
                return False
            r = self.dk(["exec", self.db_name, "pg_restore", "--no-owner", "--no-privileges", "--exit-on-error",
                         "-U", db["user"], "-d", db["name"], "/tmp/restore.dump"], timeout=self.d["limits"]["timeout"])
        if not r.ok:
            self.record("RESTORING", "database restored from backup", FAIL,
                        "restore failed: " + self.redact(tail(r.err or r.out, 6, 600)), kind="data",
                        started=started, evidence={"stderr": self.redact(tail(r.err, 40))})
            return False
        tables = self.psql(
            "select table_name from information_schema.tables where table_schema = 'public' "
            "and table_type = 'BASE TABLE' order by 1")
        counts = {}
        for t in [x for x in tables.out.split("\n") if x][:60]:
            c = self.psql(f'select count(*) from public."{t}"')
            counts[t] = int(c.out.strip()) if c.ok and c.out.strip().isdigit() else None
        total = sum(v for v in counts.values() if v)
        self.emit("restored", what="database", tables=len(counts), rows=total)
        self.record("RESTORING", "database restored from backup", PASS,
                    f"{len(counts)} tables, {total:,} rows restored into a fresh PostgreSQL",
                    kind="data", started=started, evidence={"tables": counts, "image": db["image"]})
        return True

    def scratch_dest(self, name: str) -> Path | None:
        """Where a file group is extracted, or None if that would leave the scratch folder."""
        return contained(self.scratch, self.scratch / name)

    def restore_files(self) -> bool:
        ok = True
        for f in self.d["files"]:
            started = self.clock.elapsed()
            path = self.recovery_dir / f["artifact"]
            if f["name"] in self.failed_artifacts or not path.is_file():
                self.record("RESTORING", f"{f['name']} restored", FAIL,
                            "not attempted: its artifact failed preflight", kind="files", started=started)
                ok = False
                continue
            dest = self.scratch_dest(f["name"])
            if dest is None or dest == self.scratch.resolve():
                self.record("RESTORING", f"{f['name']} restored", FAIL,
                            "refused: its extraction folder would be outside the drill's scratch area",
                            kind="files", started=started)
                ok = False
                continue
            try:
                manifest = archive.extract(path, dest, progress=self.file_progress(f["name"]),
                                           hashed=self.file_progress(f["name"], "checksummed"))
            except Exception as exc:
                self.record("RESTORING", f"{f['name']} restored", FAIL, f"extraction failed: {exc}",
                            kind="files", started=started)
                ok = False
                continue
            self.manifests[f["name"]] = manifest
            size = sum(m["bytes"] for m in manifest.values())
            self.record("RESTORING", f"{f['name']} restored", PASS,
                        f"{len(manifest):,} files ({size:,} bytes) extracted to isolated scratch storage",
                        kind="files", started=started, evidence={"files": len(manifest), "bytes": size,
                                                                 "mount": f["mount"]})
        return ok

    def start_service(self) -> bool:
        self.stage("STARTING")
        svc = self.d["service"]
        started = self.clock.elapsed()
        args = ["create", "--name", self.app_name, "--network", self.net, *self.labels(),
                "--memory", self.d["limits"]["app_memory"], "--pids-limit", "256",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--tmpfs", "/tmp"]
        args += ["--env-file", str(self.env_file("app", self.resolve_env()))]
        r = self.dk(args + [svc["image"]], timeout=60)
        if not r.ok:
            self.record("STARTING", "application container created", FAIL, self.redact(r.err.strip()[:400]),
                        kind="service", started=started)
            return False
        self.resource("created", "container", "app", self.app_name, svc["image"])
        for f in self.d["files"]:
            if f["name"] in self.manifests:
                cp = self.dk(["cp", str(self.scratch / f["name"]) + "/.", f"{self.app_name}:{f['mount']}"],
                             timeout=600)
                if not cp.ok:
                    self.record("STARTING", f"{f['name']} mounted into the app", FAIL, cp.err.strip()[:400],
                                kind="files", started=started)
                    return False
        r = self.dk(["start", self.app_name], timeout=60)
        if not r.ok:
            self.record("STARTING", "application started", FAIL, self.redact(r.err.strip()[:400]),
                        kind="service", started=started)
            return False
        self.resource("started", "container", "app", self.app_name, svc["image"])
        r = self.dk(["run", "-d", "--name", self.probe_name, "--network", self.net, *self.labels(),
                     "--memory", "128m", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                     PROBE_IMAGE, "sleep", str(int(self.d["limits"]["timeout"]) + 60)], timeout=60)
        if not r.ok:
            raise RuntimeError("could not start the journey probe: " + r.err.strip()[:300])
        self.resource("started", "container", "probe", self.probe_name, PROBE_IMAGE)
        base = f"http://{self.app_name}:{svc['port']}"
        result, err = self.probe({"mode": "health", "base": base, "path": svc["health"],
                                  "timeout": self.d["limits"]["readiness_timeout"]},
                                 timeout=self.d["limits"]["readiness_timeout"] + 30)
        running = self.dk(["inspect", "-f", "{{.State.Running}} {{.State.ExitCode}}", self.app_name], timeout=20)
        if result and result.get("ok"):
            self.record("STARTING", f"service boots and {svc['health']} responds", PASS,
                        f"HTTP {result['status']} after {result['attempts']} attempt(s)", kind="service",
                        started=started, evidence=result)
            return True
        logs = self.dk(["logs", "--tail", "40", self.app_name], timeout=20)
        self.record("STARTING", f"service boots and {svc['health']} responds", FAIL,
                    (err or (result or {}).get("error") or "no healthy response")
                    + f" (container running/exit: {running.out.strip()})", kind="service", started=started,
                    evidence={"probe": result, "logs": self.redact(tail(logs.out + logs.err, 40))})
        return False

    def validate(self, db_ok: bool, app_ok: bool) -> None:
        self.stage("VALIDATING")
        checks = self.d["checks"]
        for chk in checks["data"]:
            started = self.clock.elapsed()
            if not db_ok:
                self.record("VALIDATING", chk["name"], SKIP, "not evaluated: database did not restore",
                            kind="data", mandatory=chk["mandatory"])
                continue
            r = self.psql(chk["sql"])
            if not r.ok:
                self.record("VALIDATING", chk["name"], FAIL, "query failed: " + tail(r.err, 3, 300),
                            kind="data", mandatory=chk["mandatory"], started=started, evidence={"sql": chk["sql"]})
                continue
            raw = r.out.strip().split("\n")[0].split(SEP)[0] if r.out.strip() else ""
            passed, why = self._expect(raw, chk["expect"])
            self.record("VALIDATING", chk["name"], PASS if passed else FAIL, why, kind="data",
                        mandatory=chk["mandatory"], started=started,
                        evidence={"sql": chk["sql"], "value": raw, "expect": chk["expect"]})

        for chk in checks["references"]:
            started = self.clock.elapsed()
            manifest = self.manifests.get(chk["files"])
            if not db_ok or manifest is None:
                self.record("VALIDATING", chk["name"], SKIP,
                            "not evaluated: " + ("database" if not db_ok else chk["files"]) + " did not restore",
                            kind="reference", mandatory=chk["mandatory"])
                continue
            r = self.psql(chk["sql"], timeout=300)
            if not r.ok:
                self.record("VALIDATING", chk["name"], FAIL, "query failed: " + tail(r.err, 3, 300),
                            kind="reference", mandatory=chk["mandatory"], started=started)
                continue
            rows = [line.split(SEP) for line in r.out.split("\n") if line]
            missing = []
            ordered = []  # (path, is_missing) for every reference, to place the gaps in path order
            for row in rows:
                ref = row[-1]
                key = row[0] if len(row) > 1 else ref
                rel = ref[len(chk["strip_prefix"]):] if chk["strip_prefix"] and ref.startswith(chk["strip_prefix"]) else ref
                rel = rel.lstrip("/")
                ordered.append((rel, rel not in manifest))
                if rel not in manifest:
                    missing.append({"key": key, "path": rel})
            folders = Counter(m["path"].rsplit("/", 1)[0] if "/" in m["path"] else "." for m in missing)
            orphans = len(manifest) - (len(rows) - len(missing))
            ordered.sort()
            runs: list[list[int]] = []
            for i, (_, gone) in enumerate(ordered):
                if not gone:
                    continue
                if runs and runs[-1][0] + runs[-1][1] == i:
                    runs[-1][1] += 1
                else:
                    runs.append([i, 1])
            evidence = {"sql": chk["sql"], "references": len(rows), "missing": len(missing),
                        "files_in_backup": len(manifest), "unreferenced_files": max(orphans, 0),
                        "missing_sample": missing[:40], "missing_by_folder": folders.most_common(10),
                        "missing_runs": runs[:200], "missing_runs_truncated": len(runs) > 200,
                        "first_path": ordered[0][0] if ordered else None,
                        "last_path": ordered[-1][0] if ordered else None}
            if missing:
                top = folders.most_common(3)
                where = (f"all of them under {top[0][0]}/" if len(folders) == 1
                         else "most under " + ", ".join(f"{d}/ ({n})" for d, n in top))
                summary = (f"{len(missing):,} of {len(rows):,} files the database points to are missing from the "
                           f"backup; {where}")
            else:
                summary = f"all {len(rows):,} files the database points to came back"
            self.emit("references", check=chk["name"], total=len(rows), missing=len(missing),
                      missing_runs=runs[:200], truncated=len(runs) > 200,
                      first_path=self.redact(ordered[0][0]) if ordered else None,
                      last_path=self.redact(ordered[-1][0]) if ordered else None)
            self.record("VALIDATING", chk["name"], FAIL if missing else PASS, summary, kind="reference",
                        mandatory=chk["mandatory"], started=started, evidence=evidence)

        base = f"http://{self.app_name}:{self.d['service']['port']}"
        for j in checks["journeys"]:
            started = self.clock.elapsed()
            if not app_ok:
                self.record("VALIDATING", j["name"], SKIP, "not evaluated: the service did not start",
                            kind="journey", mandatory=j["mandatory"])
                continue
            result, err = self.probe({"mode": "journey", "base": base, "steps": j["steps"], "step_timeout": 15},
                                     timeout=60 + 20 * len(j["steps"]),
                                     on_line=self.journey_line(j["name"], len(j["steps"])))
            if result is None:
                self.record("VALIDATING", j["name"], ERROR, f"probe failed: {err}", kind="journey",
                            mandatory=j["mandatory"], started=started)
                continue
            total = len(j["steps"])
            if result["ok"]:
                last = result["steps"][-1]
                kind = (last.get("content_type") or "").split(";")[0]
                summary = (f"all {total} steps passed; last response HTTP {last.get('status')}, "
                           f"{last.get('bytes', 0):,} bytes {kind}").strip()
            else:
                bad = result["steps"][-1]
                at = f"step {len(result['steps'])} of {total}, {bad.get('method', '')} {bad.get('path', '')}"
                if "error" in bad:
                    summary = f"stopped at {at}: {bad['error']}"
                else:
                    failed = [c["check"] for c in bad.get("checks", []) if not c["ok"]]
                    summary = (f"stopped at {at}: got HTTP {bad.get('status')}, expected "
                               + "; ".join(failed[:3]))
            self.record("VALIDATING", j["name"], PASS if result["ok"] else FAIL, summary, kind="journey",
                        mandatory=j["mandatory"], started=started, evidence=result)

    @staticmethod
    def _expect(raw: str, exp: dict) -> tuple[bool, str]:
        try:
            value: Any = float(raw)
            shown = f"{int(value):,}" if value.is_integer() else raw
        except ValueError:
            value, shown = raw, repr(raw)
        problems = []
        if "equals" in exp and str(exp["equals"]) != raw and value != exp["equals"]:
            problems.append(f"expected {exp['equals']!r}")
        if "min" in exp and (not isinstance(value, float) or value < exp["min"]):
            problems.append(f"expected at least {exp['min']}")
        if "max" in exp and (not isinstance(value, float) or value > exp["max"]):
            problems.append(f"expected at most {exp['max']}")
        if problems:
            return False, f"got {shown}, " + ", ".join(problems)
        return True, f"got {shown}"

    def objectives(self, db_ok: bool) -> dict:
        obj = self.d["objectives"]
        out: dict[str, Any] = {"rto_target": obj["rto"], "rpo_target": obj["rpo"],
                               "rto_target_text": obj["rto_text"], "rpo_target_text": obj["rpo_text"]}
        measured = self.last_mandatory_at
        out["measured_recovery_seconds"] = measured
        out["rto_basis"] = "from drill start (after approval) until the last mandatory check was evaluated"
        out["rpo_seconds"] = None
        out["rpo_basis"] = "unknown: no rpo_watermark_sql declared"
        if obj["rpo_watermark_sql"] and db_ok:
            r = self.psql(obj["rpo_watermark_sql"])
            wm = parse_time(r.out.strip().split(SEP)[0]) if r.ok and r.out.strip() else None
            incident = parse_time(obj["incident_time"]) if obj["incident_time"] else self.started
            src = "declared incident_time" if obj["incident_time"] else "drill start time"
            if wm:
                out["rpo_seconds"] = max((incident - wm).total_seconds(), 0)
                out["watermark"] = iso(wm)
                out["incident_time"] = iso(incident)
                out["rpo_basis"] = (f"{src} {iso(incident)} minus newest restored data "
                                    f"{iso(wm)} (from: {obj['rpo_watermark_sql']})")
            else:
                out["rpo_basis"] = "unknown: watermark query returned no timestamp"
        return out

    # -- verdict --------------------------------------------------------------------------------------

    def verdict(self, blocked: str | None, inconclusive: str | None) -> tuple[str, str]:
        if blocked:
            return "BLOCKED", blocked
        mandatory = [c for c in self.checks if c["mandatory"]]
        failed = [c for c in mandatory if c["status"] == FAIL]
        errored = [c for c in mandatory if c["status"] == ERROR]
        skipped = [c for c in mandatory if c["status"] == SKIP]
        foundation = [c for c in failed if c["stage"] in ("PREFLIGHT", "PROVISIONING", "RESTORING", "STARTING")]
        if inconclusive and not failed:
            return "INCONCLUSIVE", inconclusive
        if foundation:
            return "FAIL", f"{foundation[0]['name']}: {foundation[0]['summary']}"
        if failed:
            booted = any(c["stage"] == "STARTING" and c["status"] == PASS and c["kind"] == "service"
                         for c in self.checks)
            label = "PARTIAL" if booted else "FAIL"
            return label, f"{len(failed)} mandatory check(s) failed: " + "; ".join(c["name"] for c in failed[:3])
        if errored or skipped:
            return "INCONCLUSIVE", f"{len(errored) + len(skipped)} mandatory check(s) could not be evaluated"
        if not mandatory:
            return "INCONCLUSIVE", "no mandatory checks were evaluated"
        return "PASS", "every mandatory check in the Recovery Contract passed"

    # -- run ------------------------------------------------------------------------------------------

    def run(self) -> dict:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "contract.yaml").write_text(self.c.path.read_text(encoding="utf-8"), encoding="utf-8")
        self.emit("start", app=self.c.name, recovery_dir=str(self.recovery_dir), trigger=self.trigger)
        blocked = inconclusive = None
        execution = "COMPLETED"
        db_ok = app_ok = False
        try:
            self.preflight()
            if any(c["status"] == FAIL and c["mandatory"] for c in self.checks):
                self.notes.append("Preflight found failed artifacts; restoring what can be restored for evidence.")
            self.provision()
            db_ok = self.restore_database()
            self.restore_files()
            app_ok = self.start_service() if db_ok else False
            if not db_ok:
                self.record("STARTING", "service boots", SKIP, "not started: database did not restore",
                            kind="service")
            self.validate(db_ok, app_ok)
        except Blocked as exc:
            blocked = str(exc)
            execution = "BLOCKED"
            self.emit("blocked", reason=blocked)
        except DrillTimeout as exc:
            inconclusive = str(exc)
            execution = "TIMED_OUT"
        except KeyboardInterrupt:
            inconclusive = "drill was cancelled by the operator"
            execution = "CANCELLED"
        except Exception as exc:  # an engine fault is never reported as a recovery result
            inconclusive = f"drill infrastructure error: {type(exc).__name__}: {self.redact(str(exc))}"
            execution = "FAILED"
            self.notes.append(self.redact(traceback.format_exc(limit=4)))
        objectives = {}
        try:
            objectives = self.objectives(db_ok) if not blocked else {}
        except Exception as exc:
            objectives = {"error": str(exc)}
        verdict, reason = self.verdict(blocked, inconclusive)

        self.stage_safe("CLEANING")
        cleanup = (self.docker.cleanup(self.run_id, on_removed=self.cleanup_removed) if self.docker.binary
                   else {"ok": True, "removed": 0, "residual": {}, "errors": []})
        had_scratch = self.scratch.exists()
        shutil.rmtree(self.scratch, ignore_errors=True)
        cleanup["scratch_removed"] = not self.scratch.exists()
        if had_scratch and cleanup["scratch_removed"]:
            self.resource("removed", "scratch", "files", "restored files")
        self.emit("cleanup", ok=bool(cleanup.get("ok")), removed=cleanup.get("removed", 0),
                  residual=cleanup.get("residual") or {})
        if self.stages and "ended" not in self.stages[-1]:
            self.stages[-1]["ended"] = round(self.clock.elapsed(), 3)

        rto_met = None
        if objectives.get("rto_target") and objectives.get("measured_recovery_seconds") is not None:
            rto_met = verdict == "PASS" and objectives["measured_recovery_seconds"] <= objectives["rto_target"]
        rpo_met = None
        if objectives.get("rpo_target") and objectives.get("rpo_seconds") is not None:
            rpo_met = objectives["rpo_seconds"] <= objectives["rpo_target"]
        objectives["rto_met"] = rto_met
        objectives["rpo_met"] = rpo_met

        evidence = {
            "restoreproof_version": __version__,
            "run_id": self.run_id,
            "app": self.c.name,
            "description": self.d["app"]["description"],
            "execution_status": execution,
            "verdict": verdict,
            "verdict_reason": self.redact(reason),
            "trigger": self.trigger,
            "approved_by": self.approved_by,
            "started_at": iso(self.started),
            "finished_at": iso(utcnow()),
            "total_seconds": round(self.clock.elapsed(), 3),
            "contract": {"path": str(self.c.path), "sha256": self.c.digest, "warnings": self.c.warnings},
            "recovery_point": self.inputs.get("recovery_point"),
            "claimed_backup_status": self.inputs.get("claimed_status"),
            "artifacts": self.inputs["artifacts"],
            "images": self.inputs["images"],
            "environment": {
                "isolation": "docker containers on an --internal network (no internet egress), "
                             "cap-drop ALL for the app, no-new-privileges, memory and pid limits, no host mounts",
                "strength": "trusted-input mode: containers share the host kernel and are not a VM boundary",
                "network": self.net, "docker": self.inputs.get("docker"),
                "limits": {k: self.d["limits"][k] for k in ("db_memory", "app_memory", "timeout")},
            },
            "stages": self.stages,
            "checks": self.checks,
            "objectives": objectives,
            "cleanup": cleanup,
            "notes": self.notes,
            "coverage": {
                "mandatory_declared": len([1 for _, c in self.c.mandatory_checks()]),
                "mandatory_evaluated": len([c for c in self.checks if c["mandatory"] and c["kind"] in
                                            ("data", "reference", "journey") and c["status"] in (PASS, FAIL)]),
            },
        }
        from .report import write_reports  # local import keeps runner importable on its own

        paths = write_reports(evidence, self.run_dir)
        evidence["reports"] = paths
        self._write_state({"run_id": self.run_id, "app": self.c.name, "status": execution, "stage": "DONE",
                           "verdict": verdict, "verdict_reason": evidence["verdict_reason"],
                           "started_at": evidence["started_at"], "elapsed": evidence["total_seconds"],
                           "checks": [{k: c[k] for k in ("id", "stage", "name", "status", "summary", "mandatory")}
                                      for c in self.checks]})
        self.emit("done", verdict=verdict, reason=evidence["verdict_reason"])
        return evidence

    def stage_safe(self, name: str) -> None:
        try:
            self.stage(name)
        except DrillTimeout:
            pass
