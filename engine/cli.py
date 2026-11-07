"""Command-line front end, shared by `hermes restoreproof ...` and standalone use
(`python -m engine.cli ...` from the plugin folder). Exit codes suit CI:
0 PASS, 1 PARTIAL/FAIL, 2 BLOCKED/INCONCLUSIVE, 3 usage or contract error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import discover as discover_mod
from . import store
from .contract import ContractError, load_contract
from .docker import Docker
from .report import summary_text
from .term import CORNSILK, DIM, ERR, GOLDENROD, OK, VERDICT_COLOR, Term
from .runner import Drill
from .util import data_home, human_duration

EXIT = {"PASS": 0, "PARTIAL": 1, "FAIL": 1, "BLOCKED": 2, "INCONCLUSIVE": 2}


def setup_parser(p: argparse.ArgumentParser) -> None:
    sub = p.add_subparsers(dest="rp_command")
    r = sub.add_parser("run", help="run a recovery drill from a Recovery Contract")
    r.add_argument("contract")
    r.add_argument("--recovery-point", help="backup set directory to use instead of the contract's default")
    r.add_argument("--json", action="store_true", help="print the evidence JSON")
    v = sub.add_parser("validate", help="validate a contract and preflight its inputs without starting anything")
    v.add_argument("contract")
    v.add_argument("--json", action="store_true")
    v.add_argument("--recovery-point")
    d = sub.add_parser("discover", help="scan a project and draft a Recovery Contract + backup coverage")
    d.add_argument("project", nargs="?", default=".")
    d.add_argument("--backups", help="directory holding backup sets")
    h = sub.add_parser("history", help="list recent drills")
    h.add_argument("--app")
    sh = sub.add_parser("show", help="print a drill's summary")
    sh.add_argument("run_id", nargs="?", default="latest")
    c = sub.add_parser("cleanup", help="remove leftover drill containers, networks and volumes")
    c.add_argument("run_id", nargs="?")


def secrets_from_env(names: list[str]) -> dict[str, str]:
    return {n: os.environ[n] for n in names if os.environ.get(n)}


def handle(args: argparse.Namespace, home: Path | None = None, secret_lookup=None) -> int:
    home = home or data_home()
    term = Term()
    cmd = getattr(args, "rp_command", None)
    if cmd in ("run", "validate"):
        if args.recovery_point:
            # A path typed on the command line is relative to where the user is, not to the contract.
            args.recovery_point = str(Path(args.recovery_point).expanduser().resolve())
        try:
            contract = load_contract(args.contract)
        except ContractError as exc:
            term.out(term.c("Recovery Contract is invalid:", ERR, bold=True))
            for e in exc.errors:
                term.out("  " + term.c(term.g["FAIL"], ERR) + " " + e)
            return 3
        lookup = secret_lookup or secrets_from_env
        secrets = lookup(contract.data["service"]["secrets"])
        if cmd == "validate":
            from .plan import preflight_plan

            plan = preflight_plan(contract, recovery_dir=args.recovery_point, secrets=secrets)
            if args.json:
                term.out(json.dumps(plan, indent=2))
            else:
                render_plan(term, plan)
            return 0 if plan["ready"] else 2
        drill = Drill(contract, home=home, recovery_dir=args.recovery_point, secrets=secrets,
                      approved_by="cli operator", trigger="cli", on_event=None if args.json else term.progress)
        if not args.json:
            label = contract.data["recovery_point"].get("label") if not args.recovery_point else None
            term.header(contract.name, label or drill.recovery_dir.name, drill.run_id)
        ev = drill.run()
        if args.json:
            term.out(json.dumps(ev, indent=2, default=str))
        else:
            term.verdict(ev, ev["reports"]["html"])
        return EXIT.get(ev["verdict"], 2)
    if cmd == "discover":
        result = discover_mod.discover(Path(args.project), backups=args.backups)
        term.out(result["contract_yaml"])
        term.out(term.c("Backup coverage", CORNSILK, bold=True))
        for line in result["coverage_lines"]:
            if line.startswith("COVERED"):
                term.out("  " + term.c(term.g["PASS"], OK) + " " + line.split(None, 1)[1])
            elif line.startswith("NOT COVERED"):
                term.out("  " + term.c(term.g["FAIL"], ERR) + " " + term.c(line, ERR, bold=True))
            else:
                term.out("  " + term.c(line, DIM))
        return 0
    if cmd == "history":
        rows = store.history(home, app=args.app)
        if not rows:
            term.out("No drills yet. Run: hermes restoreproof run restoreproof.yaml")
        for row in rows:
            parts = [
                term.c(row["run_id"], DIM),
                term.c(row["verdict"].ljust(12), VERDICT_COLOR.get(row["verdict"], DIM), bold=True),
                str(row.get("app") or "").ljust(20),
                term.c((row.get("recovery_point") or "").ljust(22), DIM),
                term.c(human_duration(row.get("recovery_seconds")).rjust(8), DIM),
            ]
            term.out("  ".join(parts))
        return 0
    if cmd == "show":
        ev = store.load_evidence(home, args.run_id)
        if not ev:
            term.out("No such run.")
            return 3
        term.out(summary_text(ev))
        term.out(term.c("report  ", DIM) + ev["reports"]["html"])
        return 0
    if cmd == "cleanup":
        result = Docker().cleanup(args.run_id)
        if result["ok"]:
            term.out(term.c(term.g["PASS"], OK) + f" removed {result['removed']} RestoreProof resource(s); none left")
        else:
            term.out(term.c(term.g["FAIL"], ERR) + f" could not remove: {result['residual']} {result['errors']}")
        return 0 if result["ok"] else 1
    term.out("usage: restoreproof {run,validate,discover,history,show,cleanup} ...")
    return 3


def render_plan(term: Term, plan: dict) -> None:
    g = term.g
    term.out()
    term.out(term.wordmark() + "  " + term.c(plan["app"], CORNSILK, bold=True)
             + term.c(f" {g['dot']} preflight plan, nothing started", DIM))
    term.out()
    claimed = plan.get("backup_job_claimed")
    if claimed:
        term.out(f"  {term.c('backup job said', DIM)} {g['lq']}{claimed.get('status')}{g['rq']} "
                 f"{term.c(str(claimed.get('finished_at') or ''), DIM)}")
    term.out(term.c("  will read", GOLDENROD))
    for r in plan["will_read"]:
        mark = term.c(g["PASS"], OK) if r.get("exists") else term.c(g["FAIL"], ERR)
        extra = ""
        if r.get("exists"):
            extra = f"{r['bytes']:,} bytes, sha256 {r['sha256'][:12]}"
            if "matches_recorded_digest" in r:
                extra += ", matches backup job" if r["matches_recorded_digest"] else ", DIGEST MISMATCH"
            if "archive_files" in r:
                extra += f", {r['archive_files']:,} files" + ("" if r.get("archive_safe") else ", UNSAFE")
        term.out(f"    {mark} {r['artifact']:<10} {term.c(extra, DIM)}")
    term.out(term.c("  will create", GOLDENROD))
    for item in plan["will_create"]:
        term.out(f"    {g['dot']} {item}")
    term.out(term.c("  will not", GOLDENROD))
    for item in plan["will_not"]:
        term.out(f"    {g['dot']} {item}")
    term.out(term.c("  checks", GOLDENROD))
    for kind, names in plan["checks"].items():
        for n in names:
            term.out(f"    {g['dot']} {term.c(kind, DIM)} {n}")
    for p in plan["problems_expected_to_fail_the_drill"]:
        term.out("  " + term.c(g["FAIL"] + " " + p, ERR))
    term.out()
    if plan["ready"]:
        term.out("  " + term.c(g["PASS"] + " ready to run", OK, bold=True))
    else:
        for b in plan["blockers"]:
            term.out("  " + term.c("blocked: " + b, ERR, bold=True))
    term.out()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="restoreproof")
    setup_parser(p)
    return handle(p.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
