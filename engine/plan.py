"""Preflight plan: everything a drill would read, create and check, computed
without starting a container. Answers "what will this drill access?"."""

from __future__ import annotations

import json
from pathlib import Path

from . import archive
from .contract import Contract
from .docker import Docker
from .runner import PROBE_IMAGE
from .util import human_duration, sha256_file


def preflight_plan(contract: Contract, recovery_dir: str | Path | None = None,
                   secrets: dict[str, str] | None = None, docker: Docker | None = None) -> dict:
    d = contract.data
    docker = docker or Docker()
    rdir = contract.recovery_dir(recovery_dir).resolve()
    blockers, problems = [], []

    ok, info = docker.available()
    if not ok:
        blockers.append(info)

    claimed = {}
    sf = d["recovery_point"].get("status_file")
    if sf and (rdir / sf).is_file():
        try:
            claimed = json.loads((rdir / sf).read_text(encoding="utf-8"))
        except ValueError:
            claimed = {}

    reads = []
    if not rdir.is_dir():
        blockers.append(f"recovery point directory not found: {rdir}")
    else:
        for label, art in [("database", d["database"]["artifact"])] + [(f["name"], f["artifact"]) for f in d["files"]]:
            p = rdir / art
            if not p.is_file():
                problems.append(f"{label} artifact missing: {p}")
                reads.append({"artifact": label, "path": str(p), "exists": False})
                continue
            entry = {"artifact": label, "path": str(p), "exists": True, "bytes": p.stat().st_size,
                     "sha256": sha256_file(p)}
            recorded = (claimed.get("artifacts") or {}).get(art, {}).get("sha256")
            if recorded:
                entry["matches_recorded_digest"] = recorded == entry["sha256"]
            if art.lower().endswith((".tar", ".tar.gz", ".tgz")):
                rep = archive.inspect(p, max_bytes=d["limits"]["max_archive_bytes"],
                                      max_members=d["limits"]["max_archive_members"])
                entry["archive_files"] = rep.files
                entry["archive_safe"] = rep.ok
                if not rep.ok:
                    problems.append(f"{label} archive would be rejected: {'; '.join(rep.problems[:3])}")
            reads.append(entry)

    images = []
    for image in (d["database"]["image"], d["service"]["image"], PROBE_IMAGE):
        present = ok and docker.image_info(image) is not None
        images.append({"image": image, "present_locally": present})
        if ok and not present:
            blockers.append(f"image not present locally: {image} (RestoreProof never pulls during a drill)")

    secret_status = [{"name": s, "available": bool((secrets or {}).get(s))} for s in d["service"]["secrets"]]
    for s in secret_status:
        if not s["available"]:
            blockers.append(f"secret reference not available: {s['name']}")

    return {
        "contract": str(contract.path), "contract_sha256": contract.digest, "app": contract.name,
        "warnings": contract.warnings,
        "ready": not blockers,
        "blockers": blockers,
        "problems_expected_to_fail_the_drill": problems,
        "backup_job_claimed": {k: claimed.get(k) for k in ("status", "finished_at")} if claimed else None,
        "will_read": reads,
        "will_create": [
            "1 docker network with --internal (no internet egress)",
            f"1 PostgreSQL container ({d['database']['image']}, memory {d['limits']['db_memory']})",
            f"1 app container ({d['service']['image']}, memory {d['limits']['app_memory']}, cap-drop ALL)",
            f"1 probe container ({PROBE_IMAGE}, memory 128m) that runs the journeys inside the network",
            "a scratch folder for extracted files, deleted after the drill",
        ],
        "will_not": ["pull images or reach the internet", "touch production or any existing container",
                     "mount host folders into restored workloads", "print or store secret values"],
        "images": images,
        "secrets_referenced": secret_status,
        "checks": {k: [c["name"] for c in v] for k, v in d["checks"].items()},
        "objectives": {"rto": d["objectives"]["rto_text"], "rpo": d["objectives"]["rpo_text"],
                       "rpo_watermark": d["objectives"]["rpo_watermark_sql"]},
        "time_limit": human_duration(d["limits"]["timeout"]),
    }
