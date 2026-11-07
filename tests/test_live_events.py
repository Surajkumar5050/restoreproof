"""The events behind the Desktop live drill view.

Unit tests need no Docker. LiveDrillEvents runs one real drill (the demo's broken backup) and checks
that every visual state the view draws comes from an event the engine actually emitted.

    python -m unittest discover -s tests -p "test_live_events.py" -v
"""

import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "billing-portal"
sys.path.insert(0, str(ROOT))

from engine import archive, probe  # noqa: E402
from engine.docker import Docker  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_drill_e2e import SKIP  # noqa: E402


class ExtractProgress(unittest.TestCase):
    def test_counts_every_regular_file_once_and_ends_at_the_total(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            src = tmp / "a.tar.gz"
            with tarfile.open(src, "w:gz") as tar:
                for i in range(7):
                    data = f"file {i}".encode()
                    info = tarfile.TarInfo(f"docs/{i}.txt")
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
                d = tarfile.TarInfo("docs/empty")
                d.type = tarfile.DIRTYPE
                tar.addfile(d)
            seen, hashed = [], []
            manifest = archive.extract(src, tmp / "out", progress=lambda done, total: seen.append((done, total)),
                                       hashed=lambda done, total: hashed.append((done, total)))
            self.assertEqual(len(manifest), 7)
            self.assertEqual(seen, [(i, 7) for i in range(1, 8)])
            self.assertEqual(hashed, [(i, 7) for i in range(1, 8)])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_failing_callback_never_breaks_extraction(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            src = tmp / "a.tar"
            with tarfile.open(src, "w") as tar:
                info = tarfile.TarInfo("x.txt")
                info.size = 1
                tar.addfile(info, io.BytesIO(b"x"))

            def boom(done, total):
                raise RuntimeError("view crashed")
            self.assertEqual(list(archive.extract(src, tmp / "out", progress=boom)), ["x.txt"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class _App(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/api/doc":
            body, ctype, status = json.dumps({"pdf_url": "/files/missing.pdf"}).encode(), "application/json", 200
        else:
            body, ctype, status = b'{"error": "file not found"}', "application/json", 404
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class ProbeStreaming(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _App)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def job(self, stream):
        return {"mode": "journey", "base": self.base, "stream": stream, "step_timeout": 5, "steps": [
            {"name": "open", "method": "GET", "path": "/api/doc", "expect_status": 200,
             "capture": {"pdf_url": "$.pdf_url"}},
            {"name": "download", "method": "GET", "path": "{pdf_url}", "expect_status": 200}]}

    def test_streams_each_request_and_response_and_keeps_the_same_result(self):
        out = io.StringIO()
        with redirect_stdout(out):
            streamed = probe.journey(self.job(True))
        lines = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([next(iter(m)) for m in lines], ["request", "response", "request", "response"])
        self.assertEqual(lines[2]["request"]["path"], "/files/missing.pdf")  # filled from the captured value
        self.assertEqual(lines[1]["response"]["status"], 200)
        self.assertTrue(lines[1]["response"]["ok"])
        self.assertEqual(lines[3]["response"]["status"], 404)
        self.assertFalse(lines[3]["response"]["ok"])
        self.assertEqual(lines[3]["response"]["failed"], ["status 200"])

        quiet = io.StringIO()
        with redirect_stdout(quiet):
            plain = probe.journey(self.job(False))
        self.assertEqual(quiet.getvalue(), "")  # without "stream" the probe prints nothing extra

        def strip(r):
            return [{k: v for k, v in s.items() if k != "elapsed"} for s in r["steps"]]
        self.assertEqual(streamed["ok"], plain["ok"])
        self.assertEqual(strip(streamed), strip(plain))


class DockerStream(unittest.TestCase):
    def test_hands_over_lines_as_they_arrive_and_returns_the_full_output(self):
        seen = []
        code = "import sys\nfor i in range(3):\n    print(i, flush=True)\nprint(sys.stdin.read().strip())"
        r = Docker(binary=sys.executable).stream(["-c", code], stdin=b"done\n", timeout=30, on_line=seen.append)
        self.assertTrue(r.ok)
        self.assertEqual([s.strip() for s in seen], ["0", "1", "2", "done"])
        self.assertEqual(r.out.split(), ["0", "1", "2", "done"])

    def test_a_timeout_is_reported_not_raised(self):
        r = Docker(binary=sys.executable).stream(["-c", "import time; time.sleep(30)"], stdin=b"", timeout=1,
                                                 on_line=lambda line: None)
        self.assertEqual(r.code, 124)


@unittest.skipIf(SKIP, f"docker drill tests skipped: {SKIP}")
class LiveDrillEvents(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from engine.contract import load_contract
        from engine.runner import Drill

        cls.tmp = Path(tempfile.mkdtemp(prefix="rp-live-"))
        project = cls.tmp / "billing-portal"
        project.mkdir()
        shutil.copy(DEMO / "restoreproof.yaml", project / "restoreproof.yaml")
        subprocess.run([sys.executable, str(DEMO / "make_backups.py"), str(project / "backups")],
                       check=True, capture_output=True)
        cls.ev = Drill(load_contract(project / "restoreproof.yaml"), home=cls.tmp / "home",
                       recovery_dir=project / "backups" / "nightly-2026-11-06").run()
        run_dir = Path(cls.ev["reports"]["evidence"]).parent
        cls.events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def of(self, kind, **match):
        return [e for e in self.events if e["kind"] == kind and all(e.get(k) == v for k, v in match.items())]

    def test_verdict_is_unchanged(self):
        self.assertEqual(self.ev["verdict"], "PARTIAL")

    def test_events_are_in_time_order(self):
        times = [e["t"] for e in self.events]
        self.assertEqual(times, sorted(times))

    def test_artifacts_are_announced_with_their_digest_result(self):
        arts = {e["name"]: e for e in self.of("artifact")}
        self.assertEqual(set(arts), {"database", "uploads"})
        self.assertTrue(all(a["present"] and a["digest"] == "match" for a in arts.values()))

    def test_every_resource_created_is_later_removed(self):
        up = {e["name"]: e for e in self.events if e["kind"] == "resource" and e["action"] in ("created", "started")}
        down = {e["name"] for e in self.of("resource", action="removed")}
        self.assertEqual({e["role"] for e in up.values()}, {"network", "database", "app", "probe"})
        self.assertTrue(set(up) <= down, f"not shown as removed: {set(up) - down}")
        self.assertTrue(self.of("cleanup", ok=True))
        net_removed = self.of("resource", action="removed", type="network")[0]["t"]
        app_removed = next(e for e in self.of("resource", action="removed") if e.get("role") == "app")["t"]
        self.assertLessEqual(app_removed, net_removed)  # containers leave before their network

    def test_file_counters_end_at_the_real_file_count(self):
        for unit in ("files", "checksummed"):
            ticks = self.of("progress", what="uploads", unit=unit)
            self.assertGreater(len(ticks), 5, unit)
            self.assertLessEqual(len(ticks), 45, unit)
            self.assertEqual((ticks[-1]["done"], ticks[-1]["total"]), (1149, 1149), unit)
            self.assertEqual([t["done"] for t in ticks], sorted(t["done"] for t in ticks), unit)
        written = self.of("progress", what="uploads", unit="files")[-1]["t"]
        self.assertLessEqual(written, self.of("progress", what="uploads", unit="checksummed")[0]["t"])

    def test_database_rows_match_the_check(self):
        rows = self.of("restored", what="database")[0]
        self.assertEqual((rows["tables"], rows["rows"]), (2, 1290))

    def test_reference_map_matches_the_evidence(self):
        ref = self.of("references")[0]
        check = next(c for c in self.ev["checks"] if c["kind"] == "reference")["evidence"]
        self.assertEqual((ref["total"], ref["missing"]), (1204, 55))
        self.assertEqual(ref["missing_runs"], check["missing_runs"])
        self.assertEqual(sum(n for _, n in ref["missing_runs"]), 55)

    def test_journey_requests_and_responses_stream_in_order(self):
        j = self.of("journey")
        self.assertEqual([(e["phase"], e["index"]) for e in j],
                         [("request", 0), ("response", 0), ("request", 1), ("response", 1)])
        self.assertEqual(j[1]["status"], 200)
        self.assertEqual(j[3]["status"], 404)
        self.assertEqual(j[2]["path"], "/files/invoices/2026/10/INV-2152.pdf")
        journey_check = next(c for c in self.events if c["kind"] == "check" and c["check"]["kind"] == "journey")
        self.assertLessEqual(j[-1]["t"], journey_check["t"])  # streamed before the check was recorded
        self.assertNotIn("stream", json.dumps(next(c for c in self.ev["checks"] if c["kind"] == "journey")))


if __name__ == "__main__":
    unittest.main()
