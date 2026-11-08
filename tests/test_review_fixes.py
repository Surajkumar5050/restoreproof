"""Tests for the catalog review of 504d7c30: every concern raised there has a test here.

    python -m unittest discover -s tests -p "test_review_fixes.py" -v
"""

import importlib.util
import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import store  # noqa: E402
from engine.contract import ContractError, load_contract  # noqa: E402
from engine.docker import docker_env  # noqa: E402
from engine.runner import Drill, contained  # noqa: E402


def load_plugin():
    """Import the plugin package itself (its folder name varies between checkouts)."""
    name = "restoreproof_under_test"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py",
                                                      submodule_search_locations=[str(ROOT)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


CONTRACT = textwrap.dedent("""
    restoreproof: 1
    app: {name: demo}
    recovery_point: {dir: backups/night-1}
    database: {artifact: db.sql.gz, name: app, user: app, image: "postgres:16-alpine"}
    files:
      - {name: FILES_NAME, artifact: uploads.tar.gz, mount: /data/uploads}
    service:
      image: "demo/app:1"
      env: {DATABASE_URL: "postgresql://{db.user}:{db.password}@{db.host}/{db.name}"}
      SECRETS
    checks:
      journeys:
        - name: open doc
          steps: [{get: /api/docs/1, expect_status: 200}]
""")


def write_contract(tmp: Path, files_name="uploads", secrets=""):
    text = CONTRACT.replace("FILES_NAME", files_name).replace("SECRETS", secrets)
    p = tmp / "restoreproof.yaml"
    p.write_text(text, encoding="utf-8")
    return p


class FileGroupNames(unittest.TestCase):
    """Restored files stay inside the scratch folder."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_contract_rejects_names_that_carry_a_path(self):
        for bad in ("../evil", "../../tmp/x", "/etc/cron.d", "a/b", "C:\\\\Windows", ".."):
            with self.subTest(name=bad):
                with self.assertRaises(ContractError) as cm:
                    load_contract(write_contract(self.tmp, files_name=json.dumps(bad)))
                self.assertTrue(any("files[0].name" in e for e in cm.exception.errors))

    def test_contract_accepts_plain_names(self):
        for good in ("uploads", "media-2026", "assets_v2"):
            with self.subTest(name=good):
                self.assertEqual(load_contract(write_contract(self.tmp, files_name=good)).data["files"][0]["name"], good)

    def test_runner_refuses_an_extraction_folder_outside_scratch(self):
        # Defence in depth: even if a name slipped past validation, the runner refuses it.
        drill = Drill(load_contract(write_contract(self.tmp)), home=self.tmp / "home", docker=object())
        drill.scratch.mkdir(parents=True)
        self.assertIsNone(drill.scratch_dest("../escaped"))
        self.assertIsNone(drill.scratch_dest(str(self.tmp.resolve())))
        self.assertEqual(drill.scratch_dest("uploads"), (drill.scratch / "uploads").resolve())

    def test_contained(self):
        base = self.tmp / "base"
        base.mkdir()
        self.assertIsNotNone(contained(base, base / "a" / "b"))
        self.assertIsNone(contained(base, base / ".." / "x"))
        self.assertIsNone(contained(base, self.tmp / "other"))

    def test_artifacts_outside_the_backup_folder_are_refused_and_never_read(self):
        drill = Drill(load_contract(write_contract(self.tmp)), home=self.tmp / "home", docker=object())
        drill.run_dir.mkdir(parents=True)
        outside = self.tmp / "outside.sql.gz"
        outside.write_bytes(b"secret")
        drill._artifact_check("database", drill.recovery_dir / ".." / ".." / "outside.sql.gz", None, 0.0)
        self.assertIn("database", drill.failed_artifacts)
        self.assertEqual(drill.checks[-1]["status"], "FAIL")
        self.assertIn("outside the backup folder", drill.checks[-1]["summary"])
        self.assertNotIn("sha256", drill.checks[-1]["evidence"])  # the file was never hashed


class ApprovalScope(unittest.TestCase):
    """The approval covers what actually runs."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.plugin = load_plugin()

    def request(self, contract_path, recovery_point=None):
        return self.plugin.approval_request(load_contract(contract_path), recovery_point)

    def test_rule_key_is_bound_to_the_backup_folder(self):
        c = write_contract(self.tmp)
        default_key, _ = self.request(c)
        same_key, _ = self.request(c, self.tmp / "backups" / "night-1")
        other_key, _ = self.request(c, self.tmp / "backups" / "night-2")
        self.assertEqual(default_key, same_key)
        self.assertNotEqual(default_key, other_key)

    def test_rule_key_changes_when_the_contract_changes(self):
        a, _ = self.request(write_contract(self.tmp))
        b, _ = self.request(write_contract(self.tmp, files_name="media"))
        self.assertNotEqual(a, b)

    def test_message_names_folder_images_and_secrets(self):
        c = write_contract(self.tmp, secrets="secrets: [SIGNING_KEY]")
        text = c.read_text(encoding="utf-8").replace(
            '{DATABASE_URL: "postgresql://{db.user}:{db.password}@{db.host}/{db.name}"}',
            '{DATABASE_URL: "postgresql://{db.user}:{db.password}@{db.host}/{db.name}", KEY: "{secret.SIGNING_KEY}"}')
        c.write_text(text, encoding="utf-8")
        _, message = self.request(c)
        for expected in ("postgres:16-alpine", "demo/app:1", "python:3.12-alpine", "SIGNING_KEY",
                         str((self.tmp / "backups" / "night-1").resolve())):
            self.assertIn(expected, message)

    def test_message_says_when_no_secrets_are_passed(self):
        _, message = self.request(write_contract(self.tmp))
        self.assertIn("No secrets are passed", message)


class DraftWriting(unittest.TestCase):
    """write_draft never overwrites and stays inside the session's working folder."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.plugin = load_plugin()
        self.tools = sys.modules["restoreproof_under_test.tools"].Tools(None)

    def discover(self, project: Path, cwd: Path):
        with mock.patch.object(type(self.tools), "cwd", lambda _self: cwd):
            return json.loads(self.tools.discover({"project_dir": str(project), "write_draft": True}))

    def test_outside_the_working_folder_nothing_is_written(self):
        project = self.tmp / "elsewhere"
        project.mkdir()
        result = self.discover(project, cwd=self.tmp / "session")
        self.assertIsNone(result["draft_written_to"])
        self.assertIn("outside the session's working folder", result["draft_note"])
        self.assertFalse((project / "restoreproof.draft.yaml").exists())

    def test_an_existing_draft_is_never_overwritten(self):
        project = self.tmp / "app"
        project.mkdir()
        (project / "restoreproof.draft.yaml").write_text("mine", encoding="utf-8")
        result = self.discover(project, cwd=self.tmp)
        self.assertIsNone(result["draft_written_to"])
        self.assertEqual((project / "restoreproof.draft.yaml").read_text(encoding="utf-8"), "mine")

    def test_a_new_draft_inside_the_working_folder_is_written(self):
        project = self.tmp / "app"
        project.mkdir()
        result = self.discover(project, cwd=self.tmp)
        self.assertTrue((project / "restoreproof.draft.yaml").is_file())
        self.assertIsNotNone(result["draft_written_to"])


class RunIds(unittest.TestCase):
    def setUp(self):
        load_plugin()
        self.tools = sys.modules["restoreproof_under_test.tools"].Tools(None)

    def test_only_run_ids_and_relative_refs_are_accepted(self):
        for ok in (None, "latest", "previous", "rp-20261107-081311-702f"):
            self.assertTrue(store.valid_run_ref(ok), ok)
        for bad in ("../../etc", "rp-20261107-081311-702f/../x", "C:\\\\x", "", "rp-1"):
            self.assertFalse(store.valid_run_ref(bad), bad)

    def test_tools_reject_bad_run_ids_before_touching_disk(self):
        for call in (lambda: self.tools.report({"run_id": "../../etc"}),
                     lambda: self.tools.compare({"before": "../x"}),
                     lambda: self.tools.cleanup({"run_id": "../x"})):
            self.assertIn("error", json.loads(call()))

    def test_store_refuses_a_path_as_run_id(self):
        self.assertIsNone(store.load_evidence(Path(tempfile.mkdtemp()), "../../etc"))


class DockerEnvironment(unittest.TestCase):
    def test_provider_keys_never_reach_the_docker_cli(self):
        fake = {"PATH": "/usr/bin", "HOME": "/home/u", "DOCKER_HOST": "unix:///x.sock",
                "OPENAI_API_KEY": "sk-leak", "ANTHROPIC_API_KEY": "leak", "NOUS_API_KEY": "leak",
                "TELEGRAM_BOT_TOKEN": "leak", "AWS_SECRET_ACCESS_KEY": "leak"}
        with mock.patch.dict(os.environ, fake, clear=True):
            env = docker_env()
        self.assertEqual(env.get("DOCKER_HOST"), "unix:///x.sock")
        self.assertIn("PATH", env)
        for leaked in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "NOUS_API_KEY", "TELEGRAM_BOT_TOKEN",
                       "AWS_SECRET_ACCESS_KEY"):
            self.assertNotIn(leaked, env)


if __name__ == "__main__":
    unittest.main()
