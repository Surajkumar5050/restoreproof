---
name: restoreproof
description: Prove an application can actually be recovered from its backups. Set up a Recovery Contract, run an isolated recovery drill, explain the evidence, and rerun after a fix.
---

# RestoreProof workflow

A backup job saying "success" is a claim. A drill is the proof. Use the RestoreProof tools; never
replace them with ad-hoc shell commands, and never decide a verdict yourself.

## 1. Set up (first time for an app)
1. `restoreproof_discover` on the project folder. Show the user the **coverage lines** first: any
   `NOT COVERED` volume is data no backup holds. Say so plainly.
2. Show the draft contract. Ask the user **one** question: which customer action proves the app works
   (for example "open an invoice and download its PDF")? Turn the answer into `checks.journeys` steps
   using only `get`/`path`, `expect_status`, `expect_json`, `expect_header`, `expect_body_prefix`,
   `capture`. Use a real record id the user gives you, or one from the data.
3. Save as `restoreproof.yaml` next to the compose file (with the user's okay), then `restoreproof_plan`.
   Show what it will read and create, and any blockers. Fix blockers before running.

## 2. Run
- `restoreproof_run_drill`. Hermes asks the user to approve; the approval is bound to the contract's
  sha256. While it runs, a live status message updates by itself.
- Report the verdict **exactly**: PASS, PARTIAL, FAIL, BLOCKED or INCONCLUSIVE. PARTIAL means the service
  boots but a declared customer workflow does not. Never say "recovered" unless the verdict is PASS.
- If the tool result has a `display` line, put it on its own paragraph so Hermes Desktop shows the card.

## 3. Explain a failure
- `restoreproof_report` with `detail: failures`. Lead with the business impact ("55 customers cannot
  download invoices"), then the facts (which check, which files, which HTTP status).
- Patterns are hypotheses, and say so: if every missing file shares one folder, the backup job probably
  excludes or failed on that folder. Suggest where to look (exclude rules, sync logs, retention).
- Never print secret values. Never suggest touching production as part of a drill.

## 4. Verify the fix
- Rerun the **same** contract with `recovery_point` set to the new backup set, then `restoreproof_compare`.
  Show which checks flipped, which artifact digests changed, and the new recovery time.

## 5. Keep it proven
- Offer the "Nightly recovery drill" automation blueprint (`/blueprint`) so a broken backup is caught
  the next morning, not during an outage. Unattended runs need a standing approval: tell the user to run
  the drill once interactively and answer "always". That approval covers only this contract's exact
  hash; editing the contract requires approving again. CI can run `hermes restoreproof run restoreproof.yaml`
  (exit 0 PASS, 1 PARTIAL/FAIL, 2 BLOCKED/INCONCLUSIVE).

## Honest limits to state when relevant
- Isolation is Docker containers on an internal network with no egress, dropped capabilities and
  resource limits. That suits your own trusted backups and images; it is not a VM boundary for hostile ones.
- Supported today: PostgreSQL dumps (.sql, .sql.gz, pg_dump custom), tar/tar.gz file archives, one app image.
