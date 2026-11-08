"""Tests for the first-run paths: /drill help, /drill with no drills yet, and /drill demo.

    python -m unittest discover -s tests -p "test_getting_started.py" -v
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_review_fixes import load_plugin  # noqa: E402


class FakeDocker:
    def __init__(self, running=True, images=()):
        self.running, self.images, self.calls = running, set(images), []

    def available(self):
        return (True, "27.0") if self.running else (False, "docker engine is not reachable")

    def image_info(self, image):
        return {"image": image} if image in self.images else None

    def run(self, args, timeout=120):
        self.calls.append(list(args))
        if args[0] in ("build", "pull"):
            self.images.add(args[3] if args[0] == "build" else args[2])
        return type("R", (), {"ok": True, "out": "", "err": ""})()


class FirstRun(unittest.TestCase):
    def setUp(self):
        self.plugin = load_plugin()
        self.tools_mod = sys.modules["restoreproof_under_test.tools"]
        self.demo = __import__("restoreproof_under_test.demo", fromlist=["prepare"])
        self.home = Path(tempfile.mkdtemp())
        self.patches = [mock.patch.object(self.plugin, "home_dir", lambda: self.home),
                        mock.patch.object(self.tools_mod, "home_dir", lambda: self.home)]
        for p in self.patches:
            p.start()
        self.tools = self.tools_mod.Tools(None)

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_help_explains_both_ways_in(self):
        text = self.plugin.drill_command(self.tools, "help")
        self.assertIn("/drill demo", text)
        self.assertIn("Set up a RestoreProof drill", text)
        self.assertIn("Docker running", text)

    def test_no_drills_yet_shows_how_to_start_instead_of_a_dead_end(self):
        for raw in ("", "status", "history", "report"):
            self.assertEqual(self.plugin.drill_command(self.tools, raw), self.plugin.GETTING_STARTED, raw)

    def test_demo_says_plainly_when_docker_is_not_running(self):
        with mock.patch.object(self.demo, "Docker", lambda: FakeDocker(running=False)):
            text = self.plugin.drill_command(self.tools, "demo")
        self.assertTrue(text.startswith("RestoreProof demo: Docker is needed"), text)

    def test_prepare_builds_and_pulls_only_what_is_missing_and_writes_backups_outside_the_plugin(self):
        docker = FakeDocker(images={"postgres:16-alpine"})
        backups = self.demo.prepare(self.home, docker=docker, engine="postgres")
        self.assertEqual([c[0] for c in docker.calls], ["build", "pull"])
        self.assertEqual(docker.calls[1][-1], "python:3.12-alpine")
        self.assertTrue(backups.is_relative_to(self.home))
        for night in self.demo.NIGHTS.values():
            self.assertTrue((backups / night / "db.sql.gz").is_file(), night)
        again = FakeDocker(images=docker.images)
        self.demo.prepare(self.home, docker=again)
        self.assertEqual(again.calls, [])  # second run: nothing to build, pull or write

    def demo_call(self, raw):
        seen = {}

        def fake_run(args, **kwargs):
            seen.update(args)
            return '{"error": "stopped in test"}'

        def fake_prepare(home, engine="postgres"):
            seen["engine"] = engine
            return self.home / "demo" / engine

        with mock.patch.object(self.demo, "prepare", fake_prepare), \
                mock.patch.object(self.tools, "run_drill", fake_run):
            text = self.plugin.drill_command(self.tools, raw)
        return seen, text

    def test_demo_points_at_the_example_contract_and_the_chosen_night(self):
        seen, _ = self.demo_call("demo fixed")
        self.assertEqual(Path(seen["contract_path"]), self.demo.CONTRACT)
        self.assertEqual(Path(seen["recovery_point"]).name, "nightly-2026-11-07")

    def test_demo_can_run_on_another_database(self):
        seen, _ = self.demo_call("demo fixed mongo")
        self.assertEqual(seen["engine"], "mongodb")
        self.assertEqual(Path(seen["contract_path"]).name, "restoreproof.mongodb.yaml")
        self.assertTrue(Path(seen["contract_path"]).is_file())
        seen, text = self.demo_call("demo oracle")
        self.assertNotIn("contract_path", seen)
        self.assertIn("unknown option 'oracle'", text)


class CardForPeople(unittest.TestCase):
    def test_card_carries_the_verdict_in_words_and_the_one_fix(self):
        from test_engine import evidence

        from engine import report
        card = report.card(evidence("PARTIAL"))
        self.assertEqual(card["status"], "Not fully recoverable")
        self.assertTrue(card["next_step"])
        self.assertIsNone(report.card(evidence("PASS"))["next_step"])


if __name__ == "__main__":
    unittest.main()
