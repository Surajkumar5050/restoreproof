"""Recovery Contract: load, validate and normalise.

A contract says what "recovered" means for one application: which backup
artifacts rebuild it, how the service starts, and which checks must pass. It is
compiled into a fixed plan of known operation types; nothing in it is ever run
as a shell command.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import databases
from .util import canonical_json, load_yaml_text, parse_duration, parse_time, sha256_bytes

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")
ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
TEMPLATE_RE = re.compile(r"\{(db\.(?:host|port|user|password|name|path)|secret\.[A-Z_][A-Z0-9_]*)\}")
SUPPORTED_DB = tuple(databases.ENGINES)
SERVER_PLACEHOLDERS = {"db.host", "db.port", "db.user", "db.password", "db.name"}
MONGO_WRITES = re.compile(
    r"(?i)\b(insert\w*|update\w*|delete\w*|remove|drop\w*|replace\w*|create\w*|rename\w*|bulkWrite|"
    r"findOneAnd\w*|runCommand|adminCommand|eval|load)\s*\(|\$(out|merge)\b")
FILE_ARTIFACT_SUFFIXES = (".tar", ".tar.gz", ".tgz")
METHODS = {"GET", "HEAD", "POST"}
EXPECT_KEYS = {"min", "max", "equals"}
STEP_KEYS = {"name", "method", "path", "get", "json", "headers", "expect_status", "expect_json",
             "expect_header", "expect_body_contains", "expect_body_prefix", "capture"}

TOP_KEYS = {"restoreproof", "app", "recovery_point", "database", "files", "service", "checks",
            "objectives", "limits"}


class ContractError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class Contract:
    path: Path
    raw: dict
    digest: str
    data: dict
    warnings: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.data["app"]["name"]

    @property
    def base_dir(self) -> Path:
        return self.path.parent

    def recovery_dir(self, override: str | Path | None = None) -> Path:
        chosen = Path(override) if override else Path(self.data["recovery_point"].get("dir") or ".")
        return chosen if chosen.is_absolute() else (self.base_dir / chosen)

    def mandatory_checks(self) -> list[tuple[str, dict]]:
        out = []
        for kind in ("data", "references", "journeys"):
            for chk in self.data["checks"][kind]:
                if chk["mandatory"]:
                    out.append((kind, chk))
        return out


def load_contract(path: str | Path) -> Contract:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ContractError([f"contract file not found: {path}"])
    text = path.read_text(encoding="utf-8")
    try:
        raw = load_yaml_text(text)
    except Exception as exc:  # parser errors vary by library
        raise ContractError([f"contract is not valid YAML: {exc}"]) from exc
    if not isinstance(raw, dict):
        raise ContractError(["contract must be a YAML mapping"])
    data, errors, warnings = normalise(raw)
    if errors:
        raise ContractError(errors)
    digest = sha256_bytes(canonical_json(raw))
    return Contract(path=path, raw=raw, digest=digest, data=data, warnings=warnings)


def _str(d: dict, key: str, errors: list[str], where: str, required: bool = True, default: str | None = None):
    value = d.get(key, default)
    if value is None or value == "":
        if required:
            errors.append(f"{where}.{key} is required")
        return default
    if not isinstance(value, (str, int, float)):
        errors.append(f"{where}.{key} must be a string")
        return default
    return str(value)


def _mapping(raw: Any, where: str, errors: list[str]) -> dict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        errors.append(f"{where} must be a mapping")
        return {}
    return raw


def _list(raw: Any, where: str, errors: list[str]) -> list:
    if raw is None:
        return []
    if not isinstance(raw, list):
        errors.append(f"{where} must be a list")
        return []
    return raw


def _expect(raw: Any, where: str, errors: list[str]) -> dict:
    exp = _mapping(raw, where, errors)
    unknown = set(exp) - EXPECT_KEYS
    if unknown:
        errors.append(f"{where} has unknown keys {sorted(unknown)} (allowed: min, max, equals)")
    if not exp:
        errors.append(f"{where} needs at least one of min, max, equals")
    for k in ("min", "max"):
        if k in exp and not isinstance(exp[k], (int, float)):
            errors.append(f"{where}.{k} must be a number")
    return exp


def _sql(value: Any, where: str, errors: list[str]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{where} must be a SQL query")
        return None
    q = value.strip().rstrip(";").strip()
    if ";" in q:
        errors.append(f"{where} must be a single statement")
    if not re.match(r"(?is)^\s*(select|with)\b", q):
        errors.append(f"{where} must be a read-only SELECT (or WITH ... SELECT) query")
    return q


def _mongo(value: Any, where: str, errors: list[str]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{where} must be a mongosh expression, for example db.customers.countDocuments()")
        return None
    q = value.strip().rstrip(";").strip()
    if ";" in q:
        errors.append(f"{where} must be a single expression")
    if MONGO_WRITES.search(q):
        errors.append(f"{where} must only read (no insert, update, delete, drop, $out or $merge)")
    return q


def _query(c: dict, key: str, where: str, errors: list[str], engine: str) -> str | None:
    """A check's query: `sql` for SQL databases, `query` for MongoDB (either name is accepted)."""
    value = c.get(key, c.get("query" if key == "sql" else key.replace("_sql", "_query")))
    if engine == "mongodb":
        return _mongo(value, where.replace("_sql", "_query").removesuffix(".sql") + (".query" if where.endswith(".sql") else ""),
                      errors)
    return _sql(value, where, errors)


def normalise(raw: dict) -> tuple[dict, list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    unknown = set(raw) - TOP_KEYS
    if unknown:
        warnings.append(f"ignored unknown top-level keys: {sorted(unknown)}")
    if raw.get("restoreproof") not in (1, "1"):
        errors.append("restoreproof: 1 is required (contract format version)")

    app = _mapping(raw.get("app"), "app", errors)
    name = _str(app, "name", errors, "app")
    if name and not NAME_RE.match(name):
        errors.append("app.name must be lowercase letters, digits, '.', '_' or '-' (max 63)")

    rp = _mapping(raw.get("recovery_point"), "recovery_point", errors)

    db = _mapping(raw.get("database"), "database", errors)
    engine = _str(db, "engine", errors, "database", default="postgres")
    if engine == "mongo":
        engine = "mongodb"
    if engine and engine not in SUPPORTED_DB:
        errors.append(f"database.engine '{engine}' is not supported (supported: {', '.join(SUPPORTED_DB)})")
        engine = "postgres"
    adapter = databases.get(engine or "postgres")
    db_artifact = _str(db, "artifact", errors, "database")
    if db_artifact and not db_artifact.lower().endswith(adapter.artifact_suffixes):
        errors.append(f"database.artifact for {adapter.label} must end with one of "
                      f"{', '.join(adapter.artifact_suffixes)}")
    if adapter.server:
        for key in ("name", "user"):
            val = db.get(key, "app")
            if not isinstance(val, str) or not re.match(r"^[a-z_][a-z0-9_]{0,62}$", val):
                errors.append(f"database.{key} must be a simple lowercase identifier")
    db_mount = db.get("mount")
    if not adapter.server:
        if not isinstance(db_mount, str) or not db_mount.startswith("/") or ".." in db_mount.split("/")                 or db_mount.endswith("/"):
            errors.append("database.mount is required for SQLite: the absolute file path the app opens, "
                          "for example /data/app.db")

    files = []
    seen = set()
    for i, f in enumerate(_list(raw.get("files"), "files", errors)):
        where = f"files[{i}]"
        f = _mapping(f, where, errors)
        fname = _str(f, "name", errors, where)
        if fname and not NAME_RE.match(fname):
            # The name becomes a folder under the drill's scratch area, so it must never carry a path.
            errors.append(f"{where}.name must be lowercase letters, digits, '.', '_' or '-' (max 63), "
                          "with no slashes or '..'")
        art = _str(f, "artifact", errors, where)
        mount = _str(f, "mount", errors, where)
        if fname and fname in seen:
            errors.append(f"{where}.name '{fname}' is duplicated")
        seen.add(fname)
        if art and not art.lower().endswith(FILE_ARTIFACT_SUFFIXES):
            errors.append(f"{where}.artifact must end with one of {FILE_ARTIFACT_SUFFIXES}")
        if mount and (not mount.startswith("/") or ".." in mount.split("/")):
            errors.append(f"{where}.mount must be an absolute container path without '..'")
        files.append({"name": fname, "artifact": art, "mount": mount, "sha256": f.get("sha256"),
                      "covers_volume": f.get("covers_volume")})

    svc = _mapping(raw.get("service"), "service", errors)
    image = _str(svc, "image", errors, "service")
    port = svc.get("port", 8080)
    if not isinstance(port, int) or not 1 <= port <= 65535:
        errors.append("service.port must be an integer port")
    health = _str(svc, "health", errors, "service", required=False, default="/health")
    if health and not health.startswith("/"):
        errors.append("service.health must be a path starting with '/'")
    env = _mapping(svc.get("env"), "service.env", errors)
    env_out = {}
    secret_names = [str(s) for s in _list(svc.get("secrets"), "service.secrets", errors)]
    for s in secret_names:
        if not ENV_RE.match(s):
            errors.append(f"service.secrets entry '{s}' must be an UPPER_CASE name")
    for k, v in env.items():
        if not ENV_RE.match(str(k)):
            errors.append(f"service.env key '{k}' must be an UPPER_CASE name")
            continue
        v = "" if v is None else str(v)
        for ref in re.findall(r"\{([^{}]+)\}", v):
            if not TEMPLATE_RE.fullmatch("{" + ref + "}"):
                errors.append(f"service.env.{k} uses unknown placeholder {{{ref}}}")
            elif not adapter.server and ref in SERVER_PLACEHOLDERS:
                errors.append(f"service.env.{k} uses {{{ref}}}, but SQLite has no server; use {{db.path}}")
            elif adapter.server and ref == "db.path":
                errors.append(f"service.env.{k} uses {{db.path}}, which is only for SQLite")
            elif ref.startswith("secret.") and ref[7:] not in secret_names:
                errors.append(f"service.env.{k} uses {{{ref}}} but '{ref[7:]}' is not listed in service.secrets")
        env_out[str(k)] = v

    checks = _mapping(raw.get("checks"), "checks", errors)
    data_checks, ref_checks, journeys = [], [], []
    file_names = {f["name"] for f in files}
    for i, c in enumerate(_list(checks.get("data"), "checks.data", errors)):
        where = f"checks.data[{i}]"
        c = _mapping(c, where, errors)
        data_checks.append({
            "name": _str(c, "name", errors, where),
            "sql": _query(c, "sql", where + ".sql", errors, engine),
            "expect": _expect(c.get("expect"), where + ".expect", errors),
            "mandatory": bool(c.get("mandatory", True)),
        })
    for i, c in enumerate(_list(checks.get("references"), "checks.references", errors)):
        where = f"checks.references[{i}]"
        c = _mapping(c, where, errors)
        target = _str(c, "files", errors, where)
        if target and target not in file_names:
            errors.append(f"{where}.files '{target}' does not match any files[].name")
        ref_checks.append({
            "name": _str(c, "name", errors, where),
            "sql": _query(c, "sql", where + ".sql", errors, engine),
            "files": target,
            "strip_prefix": str(c.get("strip_prefix") or ""),
            "mandatory": bool(c.get("mandatory", True)),
        })
    for i, j in enumerate(_list(checks.get("journeys"), "checks.journeys", errors)):
        where = f"checks.journeys[{i}]"
        j = _mapping(j, where, errors)
        steps = []
        for s_i, s in enumerate(_list(j.get("steps"), where + ".steps", errors)):
            s_where = f"{where}.steps[{s_i}]"
            s = _mapping(s, s_where, errors)
            unknown_step = set(s) - STEP_KEYS
            if unknown_step:
                errors.append(f"{s_where} has unknown keys {sorted(unknown_step)}")
            method = str(s.get("method", "GET")).upper()
            path = s.get("path", s.get("get"))
            if method not in METHODS:
                errors.append(f"{s_where}.method must be one of {sorted(METHODS)}")
            if not isinstance(path, str) or not path.startswith(("/", "{")):
                errors.append(f"{s_where}.path must start with '/' or a captured {{variable}}")
            status = s.get("expect_status", 200)
            if not isinstance(status, int):
                errors.append(f"{s_where}.expect_status must be an integer")
            capture = _mapping(s.get("capture"), s_where + ".capture", errors)
            for var, expr in capture.items():
                if not re.match(r"^[a-z_][a-z0-9_]*$", str(var)) or not str(expr).startswith("$."):
                    errors.append(f"{s_where}.capture needs name: $.json.path pairs")
            steps.append({
                "name": s.get("name") or f"{method} {path}",
                "method": method, "path": path, "json": s.get("json"),
                "headers": _mapping(s.get("headers"), s_where + ".headers", errors),
                "expect_status": status,
                "expect_json": s.get("expect_json"),
                "expect_header": _mapping(s.get("expect_header"), s_where + ".expect_header", errors),
                "expect_body_contains": s.get("expect_body_contains"),
                "expect_body_prefix": s.get("expect_body_prefix"),
                "capture": {str(k): str(v) for k, v in capture.items()},
            })
        if not steps:
            errors.append(f"{where} needs at least one step")
        journeys.append({"name": _str(j, "name", errors, where), "steps": steps,
                         "mandatory": bool(j.get("mandatory", True))})

    names = [c["name"] for c in data_checks + ref_checks + journeys if c["name"]]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        errors.append(f"check names must be unique (used more than once: {', '.join(dupes)})")
    if not (data_checks or ref_checks or journeys):
        errors.append("checks must declare at least one data, references or journeys check")
    if not journeys:
        warnings.append("no business journey declared: a green health check alone does not prove recovery")

    obj = _mapping(raw.get("objectives"), "objectives", errors)
    rto = parse_duration(obj.get("rto"))
    rpo = parse_duration(obj.get("rpo"))
    if obj.get("rto") is not None and rto is None:
        errors.append("objectives.rto must be a duration like 15m")
    if obj.get("rpo") is not None and rpo is None:
        errors.append("objectives.rpo must be a duration like 24h")
    watermark = None
    if obj.get("rpo_watermark_sql") is not None or obj.get("rpo_watermark_query") is not None:
        watermark = _query(obj, "rpo_watermark_sql", "objectives.rpo_watermark_sql", errors, engine)
    incident = obj.get("incident_time")
    if incident is not None and parse_time(incident) is None:
        errors.append("objectives.incident_time must be an ISO 8601 timestamp")

    lim = _mapping(raw.get("limits"), "limits", errors)
    timeout = parse_duration(lim.get("timeout", "15m")) or 900
    readiness = parse_duration(lim.get("readiness_timeout", "90s")) or 90
    for key in ("db_memory", "app_memory"):
        if key in lim and not re.match(r"^\d+[mg]$", str(lim[key])):
            errors.append(f"limits.{key} must look like 512m or 1g")

    data = {
        "app": {"name": name, "description": app.get("description", ""), "owner": app.get("owner", "")},
        "recovery_point": {"label": rp.get("label"), "dir": rp.get("dir"),
                           "status_file": rp.get("status_file")},
        "database": {"engine": engine, "image": db.get("image") or adapter.default_image,
                     "artifact": db_artifact, "sha256": db.get("sha256"),
                     "name": db.get("name", "app"), "user": db.get("user", "app"),
                     "mount": db_mount if not adapter.server else None,
                     "covers_volume": db.get("covers_volume")},
        "files": files,
        "service": {"image": image, "port": port if isinstance(port, int) else 8080, "health": health,
                    "env": env_out, "secrets": secret_names, "compose_file": svc.get("compose_file"),
                    "compose_service": svc.get("compose_service")},
        "checks": {"data": data_checks, "references": ref_checks, "journeys": journeys},
        "objectives": {"rto": rto, "rpo": rpo, "rto_text": obj.get("rto"), "rpo_text": obj.get("rpo"),
                       "rpo_watermark_sql": watermark, "incident_time": incident},
        "limits": {"timeout": timeout, "readiness_timeout": readiness,
                   "db_memory": str(lim.get("db_memory", "512m")), "app_memory": str(lim.get("app_memory", "256m")),
                   "max_archive_bytes": int(lim.get("max_archive_bytes", 2 * 1024 ** 3)),
                   "max_archive_members": int(lim.get("max_archive_members", 200_000))},
    }
    return data, errors, warnings
