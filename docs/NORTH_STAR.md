# RestoreProof: north star

Internal working document. Every decision is checked against this page.

## The aim
Win the Hermes Agent Plugin Hackathon (deadline **Nov 11, 5 PM EST = Nov 12, 3:30 AM IST**; catalog PR
open by **Nov 10**). Not "place". Win.

## The one idea
**A backup job saying "success" is a claim. RestoreProof is the proof.**
It rebuilds an app from its backups in isolation and checks what customers would actually experience:
every database row that points at a file, a real user journey, recovery time, data-loss window. Then it
cleans up and hands over evidence nobody has to take on faith.

The demo moment, in one breath: *the backup said SUCCESS, the database restored, the health check is
green, and 55 customers still can't open their invoices.*

## Who judges, and what they reward
Nous staff. They built Hermes with Greek-god skins, a kawaii gold default, typewriter fonts, a pet.
They care about craft and taste as much as function. Published criteria: **usefulness, reliability,
polish, creativity**, and they "favor plugins that do more than a prompt or a skill could".

| Criterion | How we earn it, concretely |
|---|---|
| Usefulness | A failure every team fears and almost none test for. Works on a real Compose + Postgres + uploads app in about 20 seconds. |
| Reliability | Deterministic engine, no model decides a verdict. Fails closed. Tested archive attacks. Cleanup proven by re-listing resources. Same inputs, same verdict. |
| Polish | Native in every Hermes surface: tools, `/drill`, `hermes restoreproof`, live status message, Desktop pane, inline evidence card, pet line, nightly blueprint to Telegram, an evidence report worth forwarding to a CTO. |
| Creativity | The Recovery Contract: recovery as an executable agreement. Approval bound to the contract's hash. Discovery that finds unbacked volumes by itself. |
| More than a skill | A real engine plus 7 tools, an approval hook, CLI, blueprint and Desktop UI. A skill cannot restore a database. |

## Why we beat the field
- ~300 entries, mostly integrations, memory providers and "receipts for agent claims". Only one backup
  entry exists: **backupdrill** (skill-only, Supabase, checks that rows came back). Our line:
  *backupdrill proves your rows came back. RestoreProof proves your business came back.*
- Most entries demo a happy path. Ours demos a **hidden failure caught**, then the fix verified by the
  same contract. A story with tension and resolution.

## Quality bar (non-negotiable)
1. Nothing half-done ships. A feature is either finished and tested or not in the repo.
2. Every claim in README, video and PR is true and reproducible from a clean clone.
3. No AI slop: no filler words, no emoji confetti, no "seamlessly/robust/leverage", no fake numbers.
   Short sentences. Concrete nouns. Every string is written on purpose.
4. Fail closed, always. Blocked or inconclusive is never shown as PASS. A skipped check lowers the claim.
5. Secrets never reach the model, logs, reports or process lists.
6. Catalog rules respected by construction: no committed archives or binaries, no core imports beyond
   documented APIs, every shell command and file read disclosed, approvals honored.

## Design language
Two homes, one identity.
- **Inside Hermes Desktop**: inherit the app's theme variables and UI kit. Native first; it reskins with
  every Hermes theme (Nous, Classic, Ares, Poseidon...).
- **Standalone surfaces** (evidence report, README art, catalog image, CLI): *Classic Hermes*. Deep navy
  ink, gold and bronze, cornsilk text. Courier Prime for display (the Nous typewriter voice), JetBrains
  Mono for data. Light mode follows Hermes's own goldenrod light ladder.
- Metaphor: **the evidence dossier**. A case file with a stamped verdict. Proof, not a dashboard.
- Myth, lightly: Hermes is the god who guided Persephone back from the underworld. RestoreProof checks
  whether your app can make the same trip. One line in the README, never a costume.
- Verdict colors come from Hermes's own `ui_ok / ui_warn / ui_error`.

## The 90-second demo
1. (0-8s) A green "backup: success" status. "Every night this says success."
2. (8-20s) In Hermes: "prove billing-portal can come back." Approval card shows exactly what will run.
3. (20-40s) Live status ticks through restore and boot, all green.
4. (40-55s) Verify: 55 invoice files missing; INV-2152 download returns 404. Stamp: PARTIAL.
5. (55-70s) Hermes explains: every missing file sits in `invoices/2026/10/`, so the backup job is
   probably excluding that folder. Evidence card and report.
6. (70-82s) Rerun the same contract on the fixed backup: PASS, recovery 18s against a 10m target.
7. (82-90s) Telegram at 03:30: "Nightly drill: PASS." End card: *Backups are claims. Drills are proof.*

## Launch checklist
- [ ] Desktop pane + inline evidence card + status chip, tested in Hermes Desktop
- [ ] README with demo GIF, quickstart from clean clone, disclosures, limits
- [ ] `setup_demo.py` reproduces the demo on a clean machine
- [ ] e2e test runs both drills; unit tests green; validator + doctor green
- [ ] Public repo under github.com/surajkumar5050, MIT, catalog entry pinned to a 40-char SHA
- [ ] Catalog PR with rule 13 disclosures; demo video on X tagging @NousResearch; form submitted
