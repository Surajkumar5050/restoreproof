"""RestoreProof: recovery drills for Hermes Agent.

Registers seven tools, an approval gate bound to each Recovery Contract's hash,
the /drill command, the `hermes restoreproof` CLI, a nightly-drill automation
blueprint and a workflow skill.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from . import schemas
from .engine import store
from .engine.contract import ContractError, load_contract
from .engine.report import summary_text
from .tools import Tools, home_dir, lookup_secrets

log = logging.getLogger("restoreproof")
TOOLSET = "restoreproof"


def register(ctx) -> None:
    tools = Tools(ctx)
    for schema, handler, emoji in (
        (schemas.DISCOVER, tools.discover, "🔎"),
        (schemas.PLAN, tools.plan, "📋"),
        (schemas.RUN, tools.run_drill, "🛟"),
        (schemas.REPORT, tools.report, "🧾"),
        (schemas.COMPARE, tools.compare, "↔️"),
        (schemas.HISTORY, tools.history, "🗂️"),
        (schemas.CLEANUP, tools.cleanup, "🧹"),
    ):
        ctx.register_tool(name=schema["name"], toolset=TOOLSET, schema=schema, handler=handler,
                          description=schema["description"].split(".")[0], emoji=emoji)

    def approval_gate(tool_name, args, task_id="", **kwargs):
        """Every drill needs a human yes. The yes is bound to the contract's sha256, so 'always allow'
        survives nightly runs but a changed contract must be approved again."""
        if tool_name != "restoreproof_run_drill":
            return None
        try:
            contract = load_contract(tools.resolve((args or {}).get("contract_path")))
        except (ContractError, TypeError, OSError):
            return None  # the tool reports the contract error itself; nothing runs
        rp = (args or {}).get("recovery_point") or contract.data["recovery_point"].get("dir") or "."
        n = len(contract.mandatory_checks())
        return {
            "action": "approve",
            "message": (f"RestoreProof will restore '{contract.name}' from {rp} into an isolated Docker network "
                        f"(4 temporary containers, no internet egress), run {n} mandatory checks, then delete "
                        f"everything it created. Contract sha256 {contract.digest[:12]}."),
            "rule_key": f"restoreproof:{contract.name}:{contract.digest[:16]}",
        }

    ctx.register_hook("pre_tool_call", approval_gate)

    ctx.register_command("drill", lambda raw: drill_command(tools, raw),
                         description="RestoreProof: /drill run <contract> [backup dir] | status | history | report [run]",
                         args_hint="run <contract> [backup dir]")

    from .engine import cli as rp_cli

    def cli_handler(args):
        sys.exit(rp_cli.handle(args, home=home_dir(), secret_lookup=lookup_secrets))

    ctx.register_cli_command("restoreproof", help="Run and inspect RestoreProof recovery drills",
                             setup_fn=rp_cli.setup_parser, handler_fn=cli_handler,
                             description="Recovery drills from a Recovery Contract. Exit 0 PASS, 1 PARTIAL/FAIL, "
                                         "2 BLOCKED/INCONCLUSIVE, 3 usage error.")

    skill = Path(__file__).parent / "skills" / "restoreproof" / "SKILL.md"
    if skill.is_file():
        try:
            ctx.register_skill("restoreproof", skill)
        except Exception:
            log.debug("skill registration unavailable", exc_info=True)

    try:
        ctx.register_automation_blueprint(
            "nightly-drill",
            title="Nightly recovery drill",
            description="Every night, restore last night's backup in isolation and tell me if the app "
                        "would really come back.",
            schedule_template="{minute} {hour} * * *",
            prompt_template=(
                "Run restoreproof_run_drill with contract_path {contract}. Reply with the tool result's "
                "phone_text exactly as given, nothing before or after it. If the tool returned an error "
                "instead, reply with one line: 'Nightly drill could not run: ' followed by the error."),
            category="devops",
            slots=[
                {"name": "time", "type": "time", "label": "What time?", "default": "03:30",
                 "help": "24h local time; pick a quiet hour"},
                {"name": "contract", "type": "text", "label": "Recovery Contract path",
                 "default": "restoreproof.yaml", "help": "absolute path to restoreproof.yaml"},
            ],
            deliver_default="origin",
            tags=["backup", "recovery", "devops"],
        )
    except Exception:
        log.debug("automation blueprints unavailable", exc_info=True)


def drill_command(tools: Tools, raw: str) -> str:
    parts = (raw or "").split()
    sub = parts[0].lower() if parts else "status"
    home = home_dir()
    if sub == "run":
        if len(parts) < 2:
            return "Usage: /drill run <restoreproof.yaml> [backup set dir]"
        import json

        args = {"contract_path": parts[1]}
        if len(parts) > 2:
            args["recovery_point"] = parts[2]
        # The user typed this command themselves: that is the approval for this one run.
        result = json.loads(tools.run_drill(args))
        if "error" in result:
            return "RestoreProof: " + result["error"] + ("\n- " + "\n- ".join(result["problems"])
                                                        if result.get("problems") else "")
        return result["summary_text"] + f"\nReport: {result['report_html']}"
    if sub == "history":
        rows = store.history(home, limit=10)
        if not rows:
            return "No drills yet. Try: /drill run restoreproof.yaml"
        return "\n".join(f"{r['run_id']}  {r['verdict']:<12} {r.get('app')}  {r.get('recovery_point') or ''}"
                         for r in rows)
    if sub in ("report", "show", "status"):
        ev = store.load_evidence(home, parts[1] if len(parts) > 1 else "latest")
        if not ev:
            return "No drills yet. Try: /drill run restoreproof.yaml"
        return summary_text(ev) + f"\nReport: {ev['reports']['html']}"
    return "Usage: /drill run <contract> [backup dir] | /drill status | /drill history | /drill report [run id]"
