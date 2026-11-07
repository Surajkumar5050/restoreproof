"""Tool schemas the model reads."""

CONTRACT = {"type": "string", "description": "Path to a Recovery Contract YAML (restoreproof.yaml). "
                                           "Relative paths resolve from the session's working directory."}
RECOVERY_POINT = {"type": "string", "description": "Optional backup-set directory to test instead of the "
                                                   "contract's default recovery_point.dir (same contract, "
                                                   "different backup)."}
RUN_ID = {"type": "string", "description": "A run id like rp-20261107-081311-702f, or 'latest' / 'previous'."}

DISCOVER = {
    "name": "restoreproof_discover",
    "description": (
        "Read a Docker Compose project and its backup folder (read-only, starts nothing). Returns a backup "
        "coverage map (which volumes no backup holds), database columns that reference files, and a DRAFT "
        "Recovery Contract. Use it first when the user asks to set up or prove recovery for an app. Show the "
        "user the coverage lines and the draft; ask them which customer journey proves the app works before "
        "saving a contract. Never invent journeys silently."),
    "parameters": {"type": "object", "properties": {
        "project_dir": {"type": "string", "description": "Project folder holding the compose file."},
        "backups_dir": {"type": "string", "description": "Folder holding backup sets (default: <project>/backups)."},
        "write_draft": {"type": "boolean", "description": "Also write <project>/restoreproof.draft.yaml "
                                                         "(never overwrites restoreproof.yaml)."},
    }, "required": ["project_dir"]},
}

PLAN = {
    "name": "restoreproof_plan",
    "description": (
        "Validate a Recovery Contract and preflight its inputs WITHOUT starting anything: exactly which files "
        "it will read (with sha256), what it will create, images, secret references (names only), checks and "
        "objectives, plus blockers. Use for 'what will this drill access?' and before every first run."),
    "parameters": {"type": "object", "properties": {"contract_path": CONTRACT, "recovery_point": RECOVERY_POINT},
                   "required": ["contract_path"]},
}

RUN = {
    "name": "restoreproof_run_drill",
    "description": (
        "Run a recovery drill: restore the backup into an isolated Docker network, boot the app, run every "
        "contract check (data, file references, user journeys), measure recovery time and RPO, clean up and "
        "write an evidence report. Requires human approval bound to the contract's hash. Takes ~20-120s. "
        "The verdict (PASS, PARTIAL, FAIL, BLOCKED, INCONCLUSIVE) comes from the engine: report it exactly, "
        "never upgrade it. Explain failures from the evidence, separating facts from hypotheses."),
    "parameters": {"type": "object", "properties": {"contract_path": CONTRACT, "recovery_point": RECOVERY_POINT},
                   "required": ["contract_path"]},
}

REPORT = {
    "name": "restoreproof_report",
    "description": (
        "Get a finished drill's evidence: verdict, every check with its evidence (missing files grouped by "
        "folder, journey steps with HTTP status), timings, RTO/RPO basis, cleanup and report paths. Use for "
        "'why did recovery fail?'."),
    "parameters": {"type": "object", "properties": {
        "run_id": RUN_ID,
        "detail": {"type": "string", "enum": ["summary", "failures", "full"],
                   "description": "summary (default), failures (evidence for failed checks), or full evidence JSON."},
    }},
}

COMPARE = {
    "name": "restoreproof_compare",
    "description": "Compare two drills (default: previous vs latest): verdict change, checks that flipped, "
                   "artifact digests that changed, recovery time delta. Use for 'did the fix work?'.",
    "parameters": {"type": "object", "properties": {
        "before": {"type": "string", "description": "Older run id (default 'previous')."},
        "after": {"type": "string", "description": "Newer run id (default 'latest')."},
        "app": {"type": "string", "description": "Limit latest/previous to one app."},
    }},
}

HISTORY = {
    "name": "restoreproof_history",
    "description": "List recent drills with verdict, recovery point, recovery time and RPO.",
    "parameters": {"type": "object", "properties": {
        "app": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 50}}},
}

CLEANUP = {
    "name": "restoreproof_cleanup",
    "description": "Remove leftover RestoreProof containers, networks and volumes (only resources labelled "
                   "restoreproof.run). Optionally for one run id. Reports anything that could not be removed.",
    "parameters": {"type": "object", "properties": {"run_id": {"type": "string"}}},
}
