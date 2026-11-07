<p align="center">
  <img src="assets/banner.jpg" width="100%" alt="RestoreProof, a community plugin for Hermes Agent. An engraved Hermes before a red sun, a drill transcript on the left and a blueprint of the drill on the right: the backup said success, the drill found PARTIAL.">
</p>

<p align="center">
  <b>Your backup job says “success”. RestoreProof proves your app can actually come back.</b><br>
  <sub>A Hermes Agent plugin · Docker · PostgreSQL · MIT</sub>
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> &nbsp;·&nbsp;
  <a href="#how-a-drill-works">How it works</a> &nbsp;·&nbsp;
  <a href="#what-you-get">Live view</a> &nbsp;·&nbsp;
  <a href="#the-recovery-contract">Recovery Contract</a> &nbsp;·&nbsp;
  <a href="#safety">Safety</a>
</p>

<br>

RestoreProof rebuilds your app from its backups in an isolated Docker network, then checks what your
customers would actually get: every database row that points at a file, a real user journey, recovery
time and data-loss window. It cleans up after itself and hands you evidence. No language model decides
a verdict.

## What it catches

<p align="center">
  <img src="assets/story.png" width="100%" alt="Three views of the same night. The backup job said success: a file was written. The health check said 200 OK: the process answers. The drill said PARTIAL: 55 invoice PDFs are missing and INV-2152 returns 404.">
</p>

This is the bundled demo, and the run is real. The database restored, the app booted and `/health`
answered 200. Customers still could not open 55 invoices, because one month of PDFs never made it into
the uploads archive. A health check would have hidden that until the day you needed the backup.

## Quick start

You need Docker and Hermes Agent.

**1. Install the plugin**

```bash
hermes plugins install Surajkumar5050/restoreproof --enable
```

Once it is listed in the Hermes catalog, `hermes plugins install restoreproof --enable` does the same.
In Hermes Desktop, turn on its Desktop part in **Capabilities → Plugins**.

**2. Set up the demo**, a billing portal whose “successful” backup is quietly broken

```bash
python examples/billing-portal/setup_demo.py
```

**3. Run a drill**

```bash
hermes restoreproof run examples/billing-portal/restoreproof.yaml
```

Or ask Hermes in plain words: *“prove billing-portal can come back from last night's backup.”*

<p align="center">
  <img src="assets/drill.svg" width="100%" alt="A real drill in the terminal: every stage passes until verify, where 55 invoice files are missing and INV-2152 returns 404. Verdict PARTIAL.">
</p>

## How a drill works

<p align="center">
  <img src="assets/pipeline.png" width="100%" alt="Six stages: preflight, isolate, restore, boot, verify, clean up. Then recovery time and data-loss window are measured and a report with a sha256 is written.">
</p>

Each stage either produces evidence or stops the claim. The result is one of five verdicts:

| Verdict | Meaning |
|---|---|
| **PASS** | Every mandatory check in the contract passed. |
| **PARTIAL** | The app boots, but a customer workflow you declared does not come back. |
| **FAIL** | An artifact, the restore or the boot failed. |
| **BLOCKED** | The drill could not run safely: a missing secret, image or Docker. Nothing was started. |
| **INCONCLUSIVE** | Not enough evidence for a verdict, such as a timeout or an infrastructure fault. |

A skipped check never counts as passed, and BLOCKED or INCONCLUSIVE is never shown as PASS.

## What you get

**A live view of the drill** in Hermes Desktop. It opens on its own when a drill starts and shows what
happens behind each check: the isolated network, the database, app and probe containers starting and
being removed, the restore counters, one square per file the database points to, and every request the
probe sends. Nothing in it is simulated: every frame comes from the drill's own event log, so the same
view replays any past run exactly.

<p align="center">
  <img src="assets/screens/live-drill.gif" width="88%" alt="The live drill view replaying a real drill: containers start in an isolated network, 1,149 files are restored and checksummed, the probe gets 200 for the invoice and 404 for its PDF, 55 of 1,204 referenced files show as a red run, the containers are removed, and the verdict is PARTIAL.">
</p>

**An evidence report** you can forward to whoever asks “are we covered?”. One file, fonts embedded, no
network requests; it reads the same offline and on paper. Every file the database points to is drawn as
a square, so a missing folder shows up as one red run.

<p align="center">
  <img src="assets/screens/report.jpg" width="88%" alt="The evidence report: verdict PARTIAL, the backup job's claim against the drill's finding, objectives, and a grid of every referenced file with the missing run in red.">
</p>

**A card in Hermes Desktop**, inline in the chat, in your active theme's colors.

<p align="center">
  <img src="assets/screens/desktop-card.png" width="88%" alt="The RestoreProof evidence card inline in a Hermes Desktop chat, verdict PARTIAL, with a to-scale stage timeline and the two failed checks.">
</p>

And everywhere else Hermes runs:

- **Seven tools** the agent drives: `discover`, `plan`, `run_drill`, `report`, `compare`, `history`, `cleanup`.
- **`/drill`** in any session or chat app, and **`hermes restoreproof`** for CI and cron. Exit codes:
  `0` PASS, `1` PARTIAL or FAIL, `2` BLOCKED or INCONCLUSIVE.
- **A live status message** that edits itself in place while the drill runs.
- **An approval bound to the contract's sha256.** Approve “always” once and that exact contract can run
  unattended, even under Hermes's default `cron_mode: deny`. Edit the contract and the next unattended
  run is refused until you approve again.
- **A “Nightly recovery drill” blueprint.** At 03:30 your phone gets the verdict:

```text
Nightly drill · billing-portal · PARTIAL
✗ 55 of 1,204 referenced files missing (invoices/2026/10/)
✗ GET /files/invoices/2026/10/INV-2152.pdf returns 404
Recovery time 14.3s (target 10m, not met: recovery incomplete)
Data-loss window (RPO) 14h 09m (target 24h, met)
11 of 13 checks passed. Nothing left running.
Report rp-20261107-094452-af01
```

## The Recovery Contract

What “recovered” means for one app, in a file next to your `docker-compose.yml`. Every field maps to a
fixed, validated operation. Nothing in it runs as a shell command, and SQL checks must be single
read-only `SELECT` statements.

```yaml
restoreproof: 1
app: { name: billing-portal }
recovery_point: { dir: backups/nightly-2026-11-06, status_file: backup-status.json }
database: { engine: postgres, artifact: db.sql.gz, name: billing, user: billing }
files:
  - { name: uploads, artifact: uploads.tar.gz, mount: /data/uploads }
service:
  image: restoreproof-demo/billing-portal:1.0
  env: { DATABASE_URL: "postgresql://{db.user}:{db.password}@{db.host}:{db.port}/{db.name}" }
checks:
  references:
    - name: every invoice PDF referenced in the database exists
      sql: select number, pdf_path from invoices where pdf_path is not null
      files: uploads
  journeys:
    - name: a customer opens invoice INV-2152 and downloads the PDF
      steps:
        - { get: /api/invoices/INV-2152, expect_status: 200, capture: { pdf_url: $.pdf_url } }
        - { get: "{pdf_url}", expect_status: 200, expect_header: { content-type: application/pdf } }
objectives: { rto: 10m, rpo: 24h, rpo_watermark_sql: select max(created_at) from invoices }
```

You rarely write one from scratch. `restoreproof_discover` reads your compose file and backups and
drafts it, with a **backup coverage map**: in the demo it finds on its own that the `exports` volume is
in no backup. Hermes then asks one question, which customer action proves the app works, and turns the
answer into a journey.

## Safety

A tool that restores data and starts applications handles sensitive and possibly hostile material.

| Guarantee | How |
|---|---|
| **The model never decides** | It can draft a contract and explain evidence. Restores, assertions and verdicts are deterministic code. |
| **Fails closed** | A missing secret, image or Docker gives BLOCKED before anything starts. An engine fault is INCONCLUSIVE, never a result. |
| **No network surprises** | Drills never pull images. The restored app runs on an internal network with no egress and is never published to your host. |
| **Least privilege** | The app runs with `--cap-drop ALL`, `no-new-privileges`, memory and pid limits, and no host mounts. |
| **Secrets stay secret** | Read from Hermes's secret store, passed through a private env file deleted with the run, and redacted from every log and report. A test checks a planted secret appears in no evidence file. |
| **Honest isolation** | Container isolation for your own trusted backups and images, not a VM boundary for hostile ones. The report says so. |

<details>
<summary><b>What it touches</b> (the catalog's disclosures)</summary>
<br>

- **Runs** the `docker` CLI (argument lists, never a shell) to create, inspect and remove containers,
  networks and volumes labelled `restoreproof.run`.
- **Reads** only the backup folder and contract you point it at.
- **Writes** run evidence under Hermes's plugin data directory, and `restoreproof.draft.yaml` only when you
  ask discovery to.
- **Network:** none of its own. No telemetry. Docker images must already be present locally.

</details>

<details>
<summary><b>How it is tested</b></summary>
<br>

Unit tests cover contract validation, archive attacks, verdict logic and the report. Eight end-to-end
scenarios run **real drills** against Docker and assert the verdict and that nothing is left behind:

broken backup → PARTIAL with exactly 55 missing files · fixed backup → PASS with the same contract ·
missing dump → FAIL · tampered archive → FAIL on its digest · path-traversal archive rejected before
extraction · app that cannot reach its database → FAIL at boot · missing secret → BLOCKED with nothing
started · secret value never present in any evidence file.

```bash
python -m unittest discover -s tests -p "test_*.py"
```

</details>

<details>
<summary><b>Limits</b></summary>
<br>

PostgreSQL dumps (`.sql`, `.sql.gz`, pg_dump custom format), tar archives and one application image per
contract today. MySQL, S3-compatible object storage and multi-service graphs are next. RestoreProof
complements your backup tool; it does not take backups.

</details>

## The name

<p align="center">
  <img src="assets/art/krater.jpg" width="88%" alt="Red-figure painting on a Greek bell-krater: Persephone rises from the earth while Hermes stands beside her, Hekate holds two torches, and Demeter waits.">
</p>

Hermes is the god who walks between worlds. On this bowl, painted in Athens around 440 BCE, he stands
beside Persephone as she rises out of the underworld, while Hekate lights the way and Demeter waits.
RestoreProof checks whether your app can make the same trip, before the day it has to.

---

<sub>
Art: <a href="https://www.metmuseum.org/art/collection/search/254925">Marble statue of Hermes</a>, 1st–2nd century CE,
engraved by <code>scripts/build_readme_art.py</code>, and <a href="https://www.metmuseum.org/art/collection/search/252973">Terracotta bell-krater, the Return of Persephone</a>,
Persephone Painter, ca. 440 BCE. The Metropolitan Museum of Art, Open Access (CC0).
Fonts: Courier Prime and JetBrains Mono, SIL Open Font License 1.1 (<code>licenses/</code>).
RestoreProof is MIT licensed. It is an independent community plugin, not affiliated with Nous Research.
</sub>
