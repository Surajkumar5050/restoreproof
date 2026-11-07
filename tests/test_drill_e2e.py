"""End-to-end drills against real Docker. Each scenario asserts the verdict AND that nothing is left behind.

Needs Docker plus the demo images (run `python examples/billing-portal/setup_demo.py` once).
Skipped automatically when they are missing. Takes about two to three minutes.

    python -m unittest discover -s tests -p "test_drill_e2e.py" -v
"""

import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "billing-portal"
sys.path.insert(0, str(ROOT))

from engine.contract import load_contract  # noqa: E402
from engine.docker import Docker  # noqa: E402
from engine.runner import PROBE_IMAGE, Drill  # noqa: E402

IMAGES = ("postgres:16-alpine", "restoreproof-demo/billing-portal:1.0", PROBE_IMAGE)


def docker_ready() -> str | None:
    d = Docker()
    ok, info = d.available()
    if not ok:
        return info
    missing = [i for i in IMAGES if d.image_info(i) is None]
    return f"missing images {missing}; run examples/billing-portal/setup_demo.py" if missing else None


SKIP = docker_ready()


@unittest.skipIf(SKIP, f"docker drill tests skipped: {SKIP}")
class DrillScenarios(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="rp-e2e-"))
        cls.home = cls.tmp / "home"
        cls.project = cls.tmp / "billing-portal"
        cls.project.mkdir()
        shutil.copy(DEMO / "restoreproof.yaml", cls.project / "restoreproof.yaml")
        subprocess.run([sys.executable, str(DEMO / "make_backups.py"), str(cls.project / "backups")],
                       check=True, capture_output=True)
        cls.broken = cls.project / "backups" / "nightly-2026-11-06"
        cls.good = cls.project / "backups" / "nightly-2026-11-07"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def drill(self, recovery_dir, contract_text=None, secrets=None):
        path = self.project / "restoreproof.yaml"
        if contract_text is not None:
            path = self.project / "variant.yaml"
            path.write_text(contract_text, encoding="utf-8")
        ev = Drill(load_contract(path), home=self.home, recovery_dir=recovery_dir, secrets=secrets).run()
        left = Docker().resources(ev["run_id"])
        self.assertFalse(any(left.values()), f"drill left resources behind: {left}")
        self.assertTrue(ev["cleanup"]["ok"])
        self.assertTrue(ev["cleanup"]["scratch_removed"])
        evidence_text = (Path(ev["reports"]["evidence"])).read_text(encoding="utf-8")
        self.assertNotIn("POSTGRES_PASSWORD=", evidence_text)
        return ev

    def check(self, ev, name):
        return next(c for c in ev["checks"] if c["name"] == name)

    def copy_set(self, name):
        dst = self.project / "backups" / name
        shutil.copytree(self.good, dst)
        return dst

    def test_1_broken_backup_is_partial_with_exact_evidence(self):
        ev = self.drill(self.broken)
        self.assertEqual(ev["verdict"], "PARTIAL")
        refs = self.check(ev, "every invoice PDF referenced in the database exists")
        self.assertEqual(refs["status"], "FAIL")
        self.assertEqual(refs["evidence"]["missing"], 55)
        self.assertEqual(refs["evidence"]["references"], 1204)
        self.assertEqual(refs["evidence"]["missing_by_folder"][0][0], "invoices/2026/10")
        journey = self.check(ev, "a customer opens invoice INV-2152 and downloads the PDF")
        self.assertEqual(journey["status"], "FAIL")
        self.assertEqual(journey["evidence"]["steps"][-1]["status"], 404)
        boot = next(c for c in ev["checks"] if c["kind"] == "service")
        self.assertEqual(boot["status"], "PASS")
        self.assertFalse(ev["objectives"]["rto_met"])

    def test_2_good_backup_passes_the_same_contract(self):
        ev = self.drill(self.good)
        self.assertEqual(ev["verdict"], "PASS")
        self.assertTrue(all(c["status"] == "PASS" for c in ev["checks"] if c["mandatory"]))
        self.assertTrue(ev["objectives"]["rto_met"])
        self.assertTrue(ev["objectives"]["rpo_met"])

    def test_3_missing_database_dump_is_fail_not_inconclusive(self):
        d = self.copy_set("no-db")
        (d / "db.sql.gz").unlink()
        ev = self.drill(d)
        self.assertEqual(ev["verdict"], "FAIL")
        self.assertIn("database artifact exists", ev["verdict_reason"])

    def test_4_tampered_archive_fails_its_recorded_digest(self):
        d = self.copy_set("tampered")
        with open(d / "uploads.tar.gz", "ab") as fh:
            fh.write(b"\0" * 16)
        ev = self.drill(d)
        self.assertEqual(ev["verdict"], "FAIL")
        self.assertTrue(any("digest" in c["name"] and c["status"] == "FAIL" for c in ev["checks"]))

    def test_5_malicious_archive_is_rejected_before_extraction(self):
        d = self.copy_set("malicious")
        with tarfile.open(d / "uploads.tar.gz", "w:gz") as tar:
            info = tarfile.TarInfo("../../escaped.txt")
            info.size = 4
            tar.addfile(info, io.BytesIO(b"pwnd"))
        status = json.loads((d / "backup-status.json").read_text())
        status["artifacts"]["uploads.tar.gz"].pop("sha256")
        (d / "backup-status.json").write_text(json.dumps(status))
        ev = self.drill(d)
        self.assertEqual(ev["verdict"], "FAIL")
        safe = self.check(ev, "uploads archive is safe to extract")
        self.assertEqual(safe["status"], "FAIL")
        self.assertIn("path traversal", safe["summary"])
        self.assertFalse((self.home / "escaped.txt").exists())
        self.assertFalse((self.project / "escaped.txt").exists())

    def test_6_app_that_cannot_reach_its_database_fails_at_boot(self):
        text = (self.project / "restoreproof.yaml").read_text(encoding="utf-8").replace(
            "{db.user}:{db.password}", "wronguser:{db.password}")
        ev = self.drill(self.good, contract_text=text)
        self.assertEqual(ev["verdict"], "FAIL")
        boot = next(c for c in ev["checks"] if c["kind"] == "service")
        self.assertEqual(boot["status"], "FAIL")
        self.assertTrue(all(c["status"] == "SKIPPED" for c in ev["checks"] if c["kind"] == "journey"))

    def test_7_missing_secret_blocks_without_starting_anything(self):
        text = (self.project / "restoreproof.yaml").read_text(encoding="utf-8").replace(
            "    UPLOAD_DIR: /data/uploads",
            "    UPLOAD_DIR: /data/uploads\n    SIGNING_KEY: \"{secret.SIGNING_KEY}\"\n  secrets: [SIGNING_KEY]")
        ev = self.drill(self.good, contract_text=text)
        self.assertEqual(ev["verdict"], "BLOCKED")
        self.assertIn("SIGNING_KEY", ev["verdict_reason"])
        self.assertEqual(ev["cleanup"]["removed"], 0)

    def test_8_secret_values_never_reach_evidence(self):
        text = (self.project / "restoreproof.yaml").read_text(encoding="utf-8").replace(
            "    UPLOAD_DIR: /data/uploads",
            "    UPLOAD_DIR: /data/uploads\n    SIGNING_KEY: \"{secret.SIGNING_KEY}\"\n  secrets: [SIGNING_KEY]")
        secret = "sk_live_DO_NOT_LEAK_5f3a9c"
        ev = self.drill(self.good, contract_text=text, secrets={"SIGNING_KEY": secret})
        self.assertEqual(ev["verdict"], "PASS")
        run_dir = Path(ev["reports"]["evidence"]).parent
        for f in run_dir.rglob("*"):
            if f.is_file():
                self.assertNotIn(secret, f.read_text(encoding="utf-8", errors="replace"), f.name)


if __name__ == "__main__":
    unittest.main()
