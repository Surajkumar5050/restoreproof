"""Project discovery: read a Compose project and its backups, then draft a
Recovery Contract and a backup coverage map. Read-only; starts nothing.

The draft is a starting point for a human (or Hermes, with the human) to review.
It never invents business journeys: it marks where one is needed.
"""

from __future__ import annotations

import gzip
import re
from pathlib import Path

from .util import dump_yaml_text, load_yaml_text

COMPOSE_NAMES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
DB_SUFFIXES = (".sql.gz", ".sql", ".dump", ".pgdump")
FILE_SUFFIXES = (".tar.gz", ".tgz", ".tar")
REF_COLUMN = re.compile(r"(path|file|filename|attachment|object_key|storage_key|s3_key|blob|upload)", re.I)


def _find_compose(project: Path) -> Path | None:
    for name in COMPOSE_NAMES:
        if (project / name).is_file():
            return project / name
    return None


def _volumes_of(service: dict) -> list[dict]:
    out = []
    for v in service.get("volumes") or []:
        if isinstance(v, str):
            parts = v.split(":")
            if len(parts) >= 2:
                src, target = parts[0], parts[1]
                kind = "bind" if src.startswith((".", "/", "~")) or "\\" in src else "volume"
                out.append({"source": src, "target": target, "type": kind})
        elif isinstance(v, dict) and v.get("target"):
            out.append({"source": v.get("source", ""), "target": v["target"], "type": v.get("type", "volume")})
    return out


def _backup_sets(root: Path) -> list[Path]:
    sets = []
    if not root.is_dir():
        return sets
    for d in [root, *[p for p in root.iterdir() if p.is_dir()]]:
        names = [p.name.lower() for p in d.iterdir() if p.is_file()]
        if any(n.endswith(DB_SUFFIXES) for n in names):
            sets.append(d)
    return sorted(sets, key=lambda p: p.name)


def _dump_text(path: Path, limit: int = 4 * 1024 * 1024) -> str:
    try:
        if path.name.lower().endswith(".gz"):
            with gzip.open(path, "rb") as fh:
                return fh.read(limit).decode("utf-8", "replace")
        if path.name.lower().endswith(".sql"):
            return path.read_bytes()[:limit].decode("utf-8", "replace")
    except OSError:
        pass
    return ""


def _reference_columns(sql: str) -> list[tuple[str, str]]:
    found = []
    for m in re.finditer(r"CREATE TABLE\s+(?:public\.)?\"?(\w+)\"?\s*\((.*?)\);", sql, re.I | re.S):
        table, body = m.group(1), m.group(2)
        for col in re.finditer(r"(?:^|,)\s*\"?(\w+)\"?\s+(text|character varying|varchar)", body, re.I):
            if REF_COLUMN.search(col.group(1)):
                found.append((table, col.group(1)))
    return found


def _key_column(sql: str, table: str) -> str:
    m = re.search(rf"CREATE TABLE\s+(?:public\.)?\"?{table}\"?\s*\((.*?)\);", sql, re.I | re.S)
    if m:
        for cand in ("number", "slug", "name", "id"):
            if re.search(rf"(?:^|,|\()\s*\"?{cand}\"?\s", m.group(1), re.I):
                return cand
    return "id"


def discover(project: Path, backups: str | None = None) -> dict:
    project = project.resolve()
    compose_path = _find_compose(project)
    services: dict = {}
    named_volumes: list[str] = []
    if compose_path:
        doc = load_yaml_text(compose_path.read_text(encoding="utf-8")) or {}
        services = doc.get("services") or {}
        named_volumes = list((doc.get("volumes") or {}).keys())

    db_svc = app_svc = None
    for name, svc in services.items():
        image = str(svc.get("image", ""))
        if "postgres" in image and not db_svc:
            db_svc = (name, svc)
    for name, svc in services.items():
        if db_svc and name == db_svc[0]:
            continue
        if svc.get("ports") or svc.get("build"):
            app_svc = (name, svc)
            break

    backup_root = Path(backups) if backups else project / "backups"
    if not backup_root.is_absolute():
        backup_root = project / backup_root
    sets = _backup_sets(backup_root)
    latest = sets[-1] if sets else None
    db_art = None
    file_arts: list[Path] = []
    status_file = None
    if latest:
        for p in sorted(latest.iterdir()):
            low = p.name.lower()
            if low.endswith(DB_SUFFIXES) and db_art is None:
                db_art = p
            elif low.endswith(FILE_SUFFIXES):
                file_arts.append(p)
            elif low.endswith(".json") and "status" in low:
                status_file = p.name

    # Map each compose volume to the artifact that should hold it.
    usage: dict[str, list[str]] = {}
    for sname, svc in services.items():
        for v in _volumes_of(svc):
            if v["type"] == "volume":
                usage.setdefault(v["source"], []).append(f"{sname}:{v['target']}")
    coverage = []
    files_entries = []
    for vol in named_volumes or list(usage):
        mounts = usage.get(vol, [])
        covered_by = None
        if db_svc and any(m.startswith(db_svc[0] + ":") for m in mounts) and db_art:
            covered_by = db_art.name
        else:
            for art in file_arts:
                stem = art.name.lower().split(".")[0]
                if stem == vol.lower() or stem in vol.lower() or vol.lower() in stem:
                    covered_by = art.name
                    target = next((m.split(":", 1)[1] for m in mounts if not m.startswith((db_svc or ("",))[0] + ":")),
                                  f"/data/{vol}")
                    files_entries.append({"name": vol, "artifact": art.name, "mount": target, "covers_volume": vol})
                    break
        coverage.append({"volume": vol, "mounted_at": mounts, "covered_by": covered_by})

    sql = _dump_text(db_art) if db_art else ""
    refs = _reference_columns(sql)
    db_env = (db_svc[1].get("environment") or {}) if db_svc else {}
    if isinstance(db_env, list):
        db_env = dict(item.split("=", 1) for item in db_env if "=" in item)
    db_name = str(db_env.get("POSTGRES_DB") or db_env.get("POSTGRES_USER") or "app")
    db_user = str(db_env.get("POSTGRES_USER") or "app")

    env_out = {}
    secrets = []
    if app_svc:
        env = app_svc[1].get("environment") or {}
        if isinstance(env, list):
            env = dict(item.split("=", 1) if "=" in item else (item, "") for item in env)
        for k, v in env.items():
            v = str(v)
            if "postgres" in v and "@" in v:
                env_out[k] = "postgresql://{db.user}:{db.password}@{db.host}:{db.port}/{db.name}"
            elif "${" in v:
                secrets.append(k)
                env_out[k] = "{secret." + k + "}"
            else:
                env_out[k] = v

    port = 8080
    if app_svc:
        for p in app_svc[1].get("ports") or []:
            m = re.search(r"(\d+)(?:/\w+)?$", str(p))
            if m:
                port = int(m.group(1))
                break

    contract = {
        "restoreproof": 1,
        "app": {"name": re.sub(r"[^a-z0-9_.-]", "-", project.name.lower())[:63] or "app",
                "description": f"Recovery profile drafted from {compose_path.name if compose_path else 'project scan'}"},
        "recovery_point": {"label": latest.name if latest else "TODO",
                           "dir": str(latest.relative_to(project)).replace("\\", "/") if latest and latest.is_relative_to(project) else str(latest or "TODO"),
                           **({"status_file": status_file} if status_file else {})},
        "database": {"engine": "postgres", "image": str(db_svc[1].get("image")) if db_svc else "postgres:16-alpine",
                     "artifact": db_art.name if db_art else "TODO.sql.gz", "name": db_name, "user": db_user,
                     **({"covers_volume": next((c["volume"] for c in coverage if db_art and c["covered_by"] == db_art.name), None)}
                        if db_art else {})},
        "files": files_entries,
        "service": {"image": str(app_svc[1].get("image", "TODO")) if app_svc else "TODO", "port": port,
                    "health": "/health", "compose_file": compose_path.name if compose_path else None,
                    "compose_service": app_svc[0] if app_svc else None, "env": env_out,
                    **({"secrets": secrets} if secrets else {})},
        "checks": {
            "data": [{"name": f"{t} came back", "sql": f"select count(*) from {t}", "expect": {"min": 1}}
                     for t in sorted({t for t, _ in refs})] or
                    [{"name": "tables came back", "sql": "select count(*) from information_schema.tables "
                      "where table_schema = 'public'", "expect": {"min": 1}}],
            "references": [],
            "journeys": [],
        },
        "objectives": {"rto": "15m", "rpo": "24h"},
    }
    for table, col in refs:
        target = files_entries[0]["name"] if files_entries else None
        if target:
            contract["checks"]["references"].append({
                "name": f"every {table}.{col} points at a restored file",
                "sql": f"select {_key_column(sql, table)}, {col} from {table} where {col} is not null",
                "files": target})
    ts_cols = re.findall(r"\"?(\w*(?:created|updated)_at)\"?\s+timestamp", sql, re.I)
    if ts_cols and refs:
        contract["objectives"]["rpo_watermark_sql"] = f"select max({ts_cols[0]}) from {refs[0][0]}"

    text = dump_yaml_text(contract)
    text = ("# Draft Recovery Contract generated by RestoreProof discovery.\n"
            "# Review every field. Add at least one business journey under checks.journeys:\n"
            "# a green health check does not prove customers can use the app.\n") + text

    lines = []
    for c in coverage:
        if c["covered_by"]:
            lines.append(f"COVERED   volume '{c['volume']}' ({', '.join(c['mounted_at']) or 'unused'}) <- {c['covered_by']}")
        else:
            lines.append(f"NOT COVERED  volume '{c['volume']}' ({', '.join(c['mounted_at']) or 'unused'}): "
                         "no backup artifact holds this data")
    if not compose_path:
        lines.append("No compose file found; coverage could not be computed.")
    if not latest:
        lines.append(f"No backup sets found under {backup_root}.")
    return {
        "project": str(project), "compose_file": str(compose_path) if compose_path else None,
        "backup_sets": [str(s) for s in sets], "latest_set": str(latest) if latest else None,
        "coverage": coverage, "coverage_lines": lines,
        "uncovered_volumes": [c["volume"] for c in coverage if not c["covered_by"]],
        "reference_columns": [f"{t}.{c}" for t, c in refs],
        "contract": contract, "contract_yaml": text,
    }
