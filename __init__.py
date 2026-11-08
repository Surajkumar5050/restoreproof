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
from .engine.report import phone_text, summary_text
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
        """Every drill needs a human yes. The yes covers what actually runs: this contract's sha256 AND
        the backup folder it will read. 'Always allow' survives nightly runs of that pair; a changed
        contract or a different backup folder asks again."""
        if tool_name != "restoreproof_run_drill":
            return None
        try:
            contract = load_contract(tools.resolve((args or {}).get("contract_path")))
        except (ContractError, TypeError, OSError):
            return None  # the tool reports the contract error itself; nothing runs
        rule_key, message = approval_request(contract, tools.resolve((args or {}).get("recovery_point")))
        return {"action": "approve", "message": message, "rule_key": rule_key}

    ctx.register_hook("pre_tool_call", approval_gate)

    ctx.register_command("drill", lambda raw: drill_command(tools, raw),
                         description="RestoreProof: /drill demo | run <contract> [backup dir] | status | history | "
                                     "report [run] | help",
                         args_hint="demo | run <contract> [backup dir] | help")

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


def approval_request(contract, recovery_point) -> tuple[str, str]:
    """The approval rule key and message for one drill.

    The backup folder is resolved exactly as the drill resolves it, so the approval names, and is
    bound to, the folder that will actually be read."""
    import hashlib
    import os

    from .engine.runner import PROBE_IMAGE

    folder = contract.recovery_dir(recovery_point).resolve()
    folder_id = hashlib.sha256(os.path.normcase(str(folder)).encode("utf-8")).hexdigest()[:12]
    d = contract.data
    images = [d["database"]["image"], d["service"]["image"], PROBE_IMAGE]
    secrets = d["service"]["secrets"]
    message = (
        f"RestoreProof will restore '{contract.name}' from {folder} into an isolated Docker network with no "
        f"internet access, using images {', '.join(images)}. "
        + (f"Secrets passed to the app container: {', '.join(secrets)} (names only; values never reach the model). "
           if secrets else "No secrets are passed to any container. ")
        + f"It runs {len(contract.mandatory_checks())} mandatory checks, then deletes everything it created. "
        f"Contract sha256 {contract.digest[:12]}, backup folder {folder_id}. 'Always' covers exactly this "
        f"contract and this backup folder; any other folder or an edited contract asks again."
    )
    return f"restoreproof:{contract.name}:{contract.digest[:16]}:{folder_id}", message


LOCAL_SURFACES = ("cli", "tui", "desktop", "acp")


GETTING_STARTED = """RestoreProof proves your app can come back from its backup. It restores the backup into a
sealed sandbox with no internet, starts the app, and tries it the way a customer would.

Try it in one step:
  /drill demo          a small billing app whose backup said "success" but lost 55 invoices
  /drill demo fixed    the same app from a good backup
  Add mysql, mariadb, mongodb or sqlite to run the same demo on that database.

For your own app, tell Hermes in plain words, for example:
  "Set up a RestoreProof drill for the app in ~/code/shop. Its backups are in /backups/shop."
Hermes finds the database dump and file archives, asks what a customer does, writes the
contract, shows you the plan, and runs the drill when you approve.

You need: Docker running, a folder with the backup (a database dump and file archives),
and your app's Docker image. Databases: PostgreSQL, MySQL, MariaDB, MongoDB and SQLite."""


def _drill_result(tools: Tools, args: dict, on_phone: bool, note=None) -> str:
    """Run one drill for a slash command. `note(result)` may add a closing line."""
    import json

    # The user typed this command themselves: that is the approval for this one run.
    result = json.loads(tools.run_drill(args))
    if "error" in result:
        return "RestoreProof: " + result["error"] + ("\n- " + "\n- ".join(result["problems"])
                                                    if result.get("problems") else "")
    text = result["phone_text"] if on_phone else result["summary_text"] + f"\nReport: {result['report_html']}"
    extra = note(result) if note else ""
    return text + (f"\n\n{extra}" if extra else "")


def drill_command(tools: Tools, raw: str) -> str:
    parts = (raw or "").split()
    # Chat apps get the phone-sized message; a local report path means nothing on a phone.
    on_phone = tools.surface() not in LOCAL_SURFACES
    sub = parts[0].lower() if parts else "status"
    home = home_dir()
    if sub in ("help", "start", "setup"):
        return GETTING_STARTED
    if sub == "demo":
        from . import demo

        words = [w.lower() for w in parts[1:]]
        which = "fixed" if "fixed" in words else "broken"
        words = ["mongodb" if w == "mongo" else w for w in words]
        engine = next((w for w in words if w in demo.ENGINES), "postgres")
        unknown = [w for w in words if w not in ("fixed", "broken", *demo.ENGINES)]
        if unknown:
            return (f"RestoreProof demo: unknown option {unknown[0]!r}. "
                    f"Try /drill demo [fixed] [{' | '.join(demo.ENGINES)}]")
        try:
            backups = demo.prepare(home, engine=engine)
        except demo.DemoError as exc:
            return f"RestoreProof demo: {exc}"

        def note(result):
            if which == "broken":
                again = "/drill demo fixed" + ("" if engine == "postgres" else f" {engine}")
                return f"Next: {again} runs the same drill on the good backup."
            if result.get("verdict") == "PASS":
                return "That is a passing drill. To set one up for your own app, see /drill help."
            return ""

        return _drill_result(tools, {"contract_path": str(demo.contract(engine)),
                                     "recovery_point": str(demo.recovery_point(backups, which))}, on_phone, note)
    if sub == "run":
        if len(parts) < 2:
            return "Usage: /drill run <restoreproof.yaml> [backup set dir]"
        args = {"contract_path": parts[1]}
        if len(parts) > 2:
            args["recovery_point"] = parts[2]
        return _drill_result(tools, args, on_phone)
    if sub == "history":
        rows = store.history(home, limit=10)
        if not rows:
            return GETTING_STARTED
        return "\n".join(f"{r['run_id']}  {r['verdict']:<12} {r.get('app')}  {r.get('recovery_point') or ''}"
                         for r in rows)
    if sub in ("report", "show", "status"):
        ev = store.load_evidence(home, parts[1] if len(parts) > 1 else "latest")
        if not ev:
            return GETTING_STARTED
        if on_phone:
            return phone_text(ev)
        return summary_text(ev) + f"\nReport: {ev['reports']['html']}"
    return ("Usage: /drill demo [fixed] [database] | /drill run <contract> [backup dir] | /drill status | /drill history | "
            "/drill report [run id] | /drill help")
