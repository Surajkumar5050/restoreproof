"""Unit tests for the parts of RestoreProof that must never be wrong.

Run from the repository root:  python -m unittest discover -s tests -v
(These tests need no Docker. tests/test_drill_e2e.py runs real drills when Docker is available.)
"""

import io
import sys
import tarfile
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import archive  # noqa: E402
from engine.contract import ContractError, load_contract  # noqa: E402
from engine.runner import Drill  # noqa: E402

GOOD = textwrap.dedent("""
    restoreproof: 1
    app: {name: demo}
    database: {artifact: db.sql.gz, name: app, user: app}
    files:
      - {name: uploads, artifact: uploads.tar.gz, mount: /data/uploads}
    service: {image: demo:1, port: 8080, env: {DATABASE_URL: "postgresql://{db.user}:{db.password}@{db.host}/{db.name}"}}
    checks:
      references:
        - {name: refs, sql: "select id, path from docs", files: uploads}
      journeys:
        - name: open doc
          steps: [{get: /api/docs/1, expect_status: 200}]
    objectives: {rto: 10m, rpo: 24h}
""")


def write(tmp: Path, text: str) -> Path:
    p = tmp / "restoreproof.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def make_tar(path: Path, members):
    with tarfile.open(path, "w:gz") as tar:
        for info, data in members:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)


def file_member(name: str, data: bytes = b"x"):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    return info, data


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_valid_contract_loads_with_stable_digest(self):
        a = load_contract(write(self.tmp, GOOD))
        b = load_contract(write(self.tmp, GOOD + "\n# a comment changes nothing\n"))
        self.assertEqual(a.name, "demo")
        self.assertEqual(a.digest, b.digest)
        self.assertEqual(len(a.mandatory_checks()), 2)

    def test_digest_changes_when_contract_changes(self):
        a = load_contract(write(self.tmp, GOOD))
        b = load_contract(write(self.tmp, GOOD.replace("rto: 10m", "rto: 20m")))
        self.assertNotEqual(a.digest, b.digest)

    def test_rejects_write_queries(self):
        bad = GOOD.replace('select id, path from docs', 'delete from docs')
        with self.assertRaises(ContractError) as cm:
            load_contract(write(self.tmp, bad))
        self.assertTrue(any("read-only SELECT" in e for e in cm.exception.errors))

    def test_rejects_stacked_statements(self):
        bad = GOOD.replace('select id, path from docs', 'select 1; drop table docs')
        with self.assertRaises(ContractError):
            load_contract(write(self.tmp, bad))

    def test_rejects_unknown_placeholder_and_undeclared_secret(self):
        bad = GOOD.replace("{db.name}", "{db.name}?k={secret.API_KEY}&x={shell}")
        with self.assertRaises(ContractError) as cm:
            load_contract(write(self.tmp, bad))
        joined = " ".join(cm.exception.errors)
        self.assertIn("{shell}", joined)
        self.assertIn("API_KEY", joined)

    def test_rejects_mount_traversal_and_unknown_files_target(self):
        bad = GOOD.replace("/data/uploads", "/data/../etc").replace("files: uploads", "files: nope")
        with self.assertRaises(ContractError) as cm:
            load_contract(write(self.tmp, bad))
        joined = " ".join(cm.exception.errors)
        self.assertIn("mount", joined)
        self.assertIn("nope", joined)

    def test_rejects_duplicate_check_names(self):
        bad = GOOD.replace("name: open doc", "name: refs")
        with self.assertRaises(ContractError) as cm:
            load_contract(write(self.tmp, bad))
        self.assertTrue(any("unique" in e for e in cm.exception.errors))

    def test_warns_without_a_journey(self):
        text = GOOD.split("  journeys:")[0] + "objectives: {rto: 10m}\n"
        c = load_contract(write(self.tmp, text))
        self.assertTrue(any("journey" in w for w in c.warnings))


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def inspect(self, members):
        path = self.tmp / "a.tar.gz"
        make_tar(path, members)
        return archive.inspect(path, max_bytes=10 ** 9, max_members=1000)

    def test_clean_archive_passes_and_extracts_with_hashes(self):
        path = self.tmp / "ok.tar.gz"
        make_tar(path, [file_member("invoices/a.pdf", b"%PDF-1"), file_member("b.txt", b"hi")])
        self.assertTrue(archive.inspect(path, max_bytes=10 ** 6, max_members=10).ok)
        manifest = archive.extract(path, self.tmp / "out")
        self.assertEqual(set(manifest), {"invoices/a.pdf", "b.txt"})
        self.assertEqual(manifest["b.txt"]["bytes"], 2)

    def test_rejects_path_traversal(self):
        self.assertFalse(self.inspect([file_member("../../evil.sh")]).ok)

    def test_rejects_absolute_paths(self):
        self.assertFalse(self.inspect([file_member("/etc/passwd")]).ok)

    def test_rejects_symlinks(self):
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/shadow"
        self.assertFalse(self.inspect([(info, None)]).ok)

    def test_rejects_device_files(self):
        info = tarfile.TarInfo("dev")
        info.type = tarfile.CHRTYPE
        self.assertFalse(self.inspect([(info, None)]).ok)

    def test_rejects_decompression_bomb(self):
        rep = self.inspect([file_member("zeros.bin", b"\0" * (20 * 1024 * 1024))])
        self.assertFalse(rep.ok)
        self.assertTrue(any("ratio" in p for p in rep.problems))

    def test_rejects_truncated_archive(self):
        path = self.tmp / "t.tar.gz"
        make_tar(path, [file_member("a.bin", bytes(range(256)) * 4000)])
        path.write_bytes(path.read_bytes()[:-200])
        self.assertFalse(archive.inspect(path, max_bytes=10 ** 9, max_members=10).ok)


class VerdictTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.drill = Drill(load_contract(write(tmp, GOOD)), home=tmp, docker=object())

    def add(self, stage, status, kind="system", mandatory=True):
        self.drill.checks.append({"stage": stage, "status": status, "kind": kind, "mandatory": mandatory,
                                  "name": f"{stage}-{kind}", "summary": "x"})

    def test_pass_requires_every_mandatory_check(self):
        self.add("RESTORING", "PASS", "data")
        self.add("STARTING", "PASS", "service")
        self.add("VALIDATING", "PASS", "journey")
        self.assertEqual(self.drill.verdict(None, None)[0], "PASS")

    def test_green_health_with_broken_journey_is_partial_not_pass(self):
        self.add("RESTORING", "PASS", "data")
        self.add("STARTING", "PASS", "service")
        self.add("VALIDATING", "FAIL", "journey")
        self.assertEqual(self.drill.verdict(None, None)[0], "PARTIAL")

    def test_restore_failure_is_fail(self):
        self.add("RESTORING", "FAIL", "data")
        self.add("VALIDATING", "SKIPPED", "journey")
        self.assertEqual(self.drill.verdict(None, None)[0], "FAIL")

    def test_skipped_mandatory_check_is_never_pass(self):
        self.add("STARTING", "PASS", "service")
        self.add("VALIDATING", "SKIPPED", "journey")
        self.assertEqual(self.drill.verdict(None, None)[0], "INCONCLUSIVE")

    def test_advisory_failure_does_not_fail_the_drill(self):
        self.add("STARTING", "PASS", "service")
        self.add("VALIDATING", "PASS", "journey")
        self.add("VALIDATING", "FAIL", "data", mandatory=False)
        self.assertEqual(self.drill.verdict(None, None)[0], "PASS")

    def test_blocked_and_timeout_are_never_pass(self):
        self.add("STARTING", "PASS", "service")
        self.assertEqual(self.drill.verdict("missing secret", None)[0], "BLOCKED")
        self.assertEqual(self.drill.verdict(None, "timed out")[0], "INCONCLUSIVE")

    def test_secrets_are_redacted_from_summaries(self):
        self.drill.redact.add("hunter2-very-secret")
        self.drill.emit = lambda *a, **k: None
        chk = self.drill.record("VALIDATING", "x", "FAIL", "conn failed for hunter2-very-secret")
        self.assertNotIn("hunter2", chk["summary"])
        self.assertNotIn(self.drill.db_password, Drill._expect.__doc__ or "")


def evidence(verdict: str = "PARTIAL", **over) -> dict:
    """A small, complete evidence record shaped like the runner's output."""
    def check(stage, name, status="PASS", kind="system", mandatory=True, ev=None, summary="ok"):
        return {"id": f"{stage.lower()}-{name}", "stage": stage, "kind": kind, "name": name, "status": status,
                "summary": summary, "mandatory": mandatory, "duration": 0.2, "evidence": ev or {}}
    checks = [check("PREFLIGHT", "artifact intact", kind="artifact"),
              check("RESTORING", "database restored", kind="data"),
              check("STARTING", "service boots", kind="service")]
    if verdict == "PARTIAL":
        checks.append(check("VALIDATING", "every file exists", "FAIL", "reference", summary="2 of 10 files missing",
                            ev={"references": 10, "missing": 2, "missing_runs": [[3, 2]],
                                "missing_by_folder": [["docs/2026", 2]], "first_path": "docs/2025/a.pdf",
                                "last_path": "docs/2026/z.pdf",
                                "missing_sample": [{"key": "D-4", "path": "docs/2026/d4.pdf"},
                                                   {"key": "D-5", "path": "docs/2026/d5.pdf"}]}))
        checks.append(check("VALIDATING", "open doc", "FAIL", "journey", summary="step 2 returned 404",
                            ev={"steps": [{"method": "GET", "path": "/api/docs/4", "status": 200, "ok": True,
                                           "content_type": "application/json", "checks": [{"check": "status 200",
                                                                                           "ok": True}]},
                                          {"method": "GET", "path": "/files/docs/2026/d4.pdf", "status": 404,
                                           "ok": False, "checks": [{"check": "status 200", "ok": False,
                                                                    "detail": "got 404"}]}]}))
    elif verdict == "FAIL":
        checks[2]["status"] = "FAIL"
    elif verdict == "INCONCLUSIVE":
        checks.append(check("VALIDATING", "open doc", "SKIPPED", "journey", summary="not evaluated"))
    ev = {
        "verdict": verdict, "verdict_reason": "the reason", "app": "demo", "description": "Demo app",
        "run_id": "rp-20261107-000000-abcd", "started_at": "2026-11-07T08:42:35Z", "trigger": "cli",
        "execution_status": "COMPLETED", "contract": {"sha256": "b" * 64},
        "recovery_point": {"label": "nightly"}, "total_seconds": 15.6,
        "claimed_backup_status": {"status": "success", "finished_at": "2026-11-06T23:00:00Z"},
        "checks": checks if verdict != "BLOCKED" else [],
        "stages": [{"stage": "PREFLIGHT", "started": 0.0, "ended": 0.8},
                   {"stage": "RESTORING", "started": 0.8, "ended": 9.0},
                   {"stage": "STARTING", "started": 9.0, "ended": 11.0},
                   {"stage": "VALIDATING", "started": 11.0, "ended": 12.0},
                   {"stage": "CLEANING", "started": 12.0, "ended": 15.6}] if verdict != "BLOCKED" else [],
        "objectives": {"measured_recovery_seconds": 12.0, "rto_target_text": "10m", "rto_met": verdict == "PASS",
                       "rpo_seconds": 3600, "rpo_target_text": "24h", "rpo_met": True},
        "cleanup": {"ok": True, "removed": 4, "residual": {}},
        "artifacts": [{"name": "database", "file": "db.sql.gz", "bytes": 10, "sha256": "a" * 64,
                       "expected_sha256": "a" * 64}],
        "images": [{"image": "demo:1", "id": "sha256:" + "c" * 64}],
        "environment": {"isolation": "internal network", "strength": "not a VM boundary"},
    }
    ev.update(over)
    return ev


class ReportTests(unittest.TestCase):
    """The evidence report and the short texts: every state renders, nothing slips past escaping,
    and the design rules that keep it a document hold."""

    def setUp(self):
        from engine import report
        self.report = report

    def test_every_verdict_renders_its_word_and_headline(self):
        for v in ("PASS", "PARTIAL", "FAIL", "BLOCKED", "INCONCLUSIVE"):
            ev = evidence(v)
            page = self.report.render_html(ev, "f" * 64)
            self.assertIn(f'<span class="vw">{v}</span>', page, v)
            self.assertIn(self.report.html.escape(self.report.headline(ev)), page, v)
            self.assertTrue(page.rstrip().endswith("</html>"), v)

    def test_design_rules_hold(self):
        page = self.report.render_html(evidence("PARTIAL"), "f" * 64)
        for banned in ("border-left:3px", "rotate(", "color-mix", "gradient", "box-shadow"):
            self.assertNotIn(banned, page, banned)

    def test_masthead_is_embedded_and_the_page_stays_small(self):
        page = self.report.render_html(evidence("PARTIAL"), "f" * 64)
        self.assertIn('src="data:image/jpeg;base64,', page)  # one self-contained file, no network fetch
        self.assertLess(len(page.encode("utf-8")), 200_000)

    def test_the_backup_jobs_claim_is_quoted_never_colored(self):
        page = self.report.render_html(evidence("PARTIAL"), "")
        self.assertIn('<span class="said">“success”</span>', page)
        self.assertNotIn('class="said ok"', page)

    def test_untrusted_text_is_escaped(self):
        ev = evidence("PARTIAL", app="<script>alert(1)</script>", description='"><img src=x onerror=1>')
        ev["checks"][3]["summary"] = "<b>bold</b>"
        page = self.report.render_html(ev, "")
        self.assertNotIn("<script>alert", page)
        self.assertNotIn("<img src=x", page)
        self.assertNotIn("<b>bold</b>", page)

    def test_file_grid_draws_every_reference_and_the_missing_run(self):
        page = self.report.render_html(evidence("PARTIAL"), "")
        self.assertIn('aria-label="10 referenced files in path order: 8 present, 2 missing in one unbroken run."',
                      page)
        self.assertIn('<rect x="18" y="0" width="12" height="6"', page)  # files 3 and 4, 6 units each
        self.assertIn("The file it asks for is one of the missing files above.", page)

    def test_old_evidence_without_runs_still_renders(self):
        ev = evidence("PARTIAL")
        del ev["checks"][3]["evidence"]["missing_runs"]
        page = self.report.render_html(ev, "")
        self.assertNotIn("<svg", page)
        self.assertIn("2 of 10 referenced files are missing", page)

    def test_report_embeds_its_fonts_and_makes_no_network_requests(self):
        page = self.report.render_html(evidence("PASS"), "")
        self.assertEqual(page.count("@font-face"), 4)
        self.assertNotIn("<link", page)
        self.assertNotIn("googleapis", page)
        self.assertNotIn("<script", page)
        self.assertLess(len(page.encode("utf-8")), 200_000)

    def test_colors_live_only_in_the_palette(self):
        import re
        for name in ("report.py", "term.py", "cli.py"):
            text = (ROOT / "engine" / name).read_text(encoding="utf-8")
            self.assertEqual(re.findall(r"#[0-9a-fA-F]{6}\b", text), [], name)

    def test_phone_text_leads_with_the_verdict_and_stays_short(self):
        text = self.report.phone_text(evidence("PARTIAL"), title="Nightly drill")
        lines = text.splitlines()
        self.assertEqual(lines[0], "Nightly drill · demo · PARTIAL")
        self.assertIn("✗ 2 of 10 referenced files missing (docs/2026/)", lines)
        self.assertIn("✗ GET /files/docs/2026/d4.pdf returns 404", lines)
        self.assertLessEqual(len(lines), 8)
        self.assertIn("PASS", self.report.phone_text(evidence("PASS")).splitlines()[0])


if __name__ == "__main__":
    unittest.main()
