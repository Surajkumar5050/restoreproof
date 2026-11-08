"""Databases beyond PostgreSQL: MySQL, MariaDB, MongoDB and SQLite.

Contract rules, backup-format checks and discovery run anywhere. The drills at the end need Docker,
the demo app image and each database's image; a missing image skips only that engine's drills.

    python -m unittest discover -s tests -p "test_databases.py" -v
"""

import gzip
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "billing-portal"
sys.path.insert(0, str(ROOT))

from engine import databases  # noqa: E402
from engine.contract import ContractError, load_contract  # noqa: E402
from engine.discover import discover  # noqa: E402
from engine.docker import Docker  # noqa: E402
from engine.runner import PROBE_IMAGE, Drill  # noqa: E402

BASE = textwrap.dedent("""
    restoreproof: 1
    app: {name: shop}
    recovery_point: {dir: backups/night}
    database: @@DB@@
    files: [{name: uploads, artifact: uploads.tar.gz, mount: /data/uploads}]
    service:
      image: "shop:1"
      env: {DATABASE_URL: "@@URL@@"}
    checks:
      data:
        - {name: rows came back, @@QUERY@@, expect: {min: 1}}
      journeys:
        - {name: home page, steps: [{get: /}]}
""")


def contract(tmp: Path, db: str, url: str, query: str) -> Path:
    path = tmp / "restoreproof.yaml"
    path.write_text(BASE.replace("@@DB@@", db).replace("@@URL@@", url).replace("@@QUERY@@", query), encoding="utf-8")
    return path


class EngineContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def load(self, db, url, query):
        return load_contract(contract(self.tmp, db, url, query))

    def errors(self, db, url, query):
        with self.assertRaises(ContractError) as cm:
            self.load(db, url, query)
        return " | ".join(cm.exception.errors)

    def test_every_engine_gets_its_own_default_image(self):
        cases = {
            "mysql": ("{engine: mysql, artifact: db.sql.gz}", "mysql://{db.user}:{db.password}@{db.host}/{db.name}"),
            "mariadb": ("{engine: mariadb, artifact: db.sql}", "mysql://{db.host}:{db.port}"),
            "mongodb": ("{engine: mongodb, artifact: db.archive.gz}", "mongodb://{db.host}:{db.port}"),
        }
        for engine, (db, url) in cases.items():
            q = "query: db.c.countDocuments()" if engine == "mongodb" else "sql: select count(*) from t"
            c = self.load(db, url, q)
            self.assertEqual(c.data["database"]["image"], databases.get(engine).default_image, engine)
        c = self.load("{engine: sqlite, artifact: app.db, mount: /data/app.db}", "sqlite:///{db.path}",
                      "sql: select count(*) from t")
        self.assertEqual(c.data["database"]["mount"], "/data/app.db")

    def test_mongo_is_an_alias(self):
        c = self.load("{engine: mongo, artifact: db.archive}", "mongodb://{db.host}", "query: db.c.countDocuments()")
        self.assertEqual(c.data["database"]["engine"], "mongodb")

    def test_unknown_engine_names_the_supported_ones(self):
        msg = self.errors("{engine: oracle, artifact: db.sql}", "x", "sql: select 1")
        for name in ("postgres", "mysql", "mariadb", "mongodb", "sqlite"):
            self.assertIn(name, msg)

    def test_artifact_must_match_the_engine(self):
        self.assertIn("for MySQL must end with one of .sql, .sql.gz",
                      self.errors("{engine: mysql, artifact: db.archive}", "x", "sql: select 1"))
        self.assertIn(".archive", self.errors("{engine: mongodb, artifact: db.sql.gz}", "x", "query: db.c.find()"))
        self.assertIn(".sqlite", self.errors("{engine: sqlite, artifact: db.dump, mount: /a.db}", "x", "sql: select 1"))

    def test_mongodb_queries_must_only_read(self):
        for bad in ("db.c.deleteMany({})", "db.c.drop()", "db.c.insertOne({a: 1})",
                    "db.c.aggregate([{$out: 'x'}])", "db.c.find(); db.c.drop()"):
            with self.subTest(query=bad):
                self.assertIn("query", self.errors("{engine: mongodb, artifact: db.archive}", "x", f'query: "{bad}"'))

    def test_sql_engines_still_refuse_writes(self):
        for engine, art in (("mysql", "db.sql"), ("sqlite", "db.sqlite, mount: /a.db")):
            with self.subTest(engine=engine):
                self.assertIn("read-only", self.errors(f"{{engine: {engine}, artifact: {art}}}", "x",
                                                       "sql: delete from t"))

    def test_sqlite_needs_a_mount_and_has_no_server_placeholders(self):
        self.assertIn("database.mount", self.errors("{engine: sqlite, artifact: db.sqlite}", "x", "sql: select 1"))
        self.assertIn("{db.path}", self.errors("{engine: sqlite, artifact: db.sqlite, mount: /a.db}",
                                                "{db.host}", "sql: select 1"))
        self.assertIn("only for SQLite", self.errors("{engine: mysql, artifact: db.sql}", "{db.path}", "sql: select 1"))

    def test_the_demo_contracts_load(self):
        for engine in databases.ENGINES:
            name = "restoreproof.yaml" if engine == "postgres" else f"restoreproof.{engine}.yaml"
            with self.subTest(engine=engine):
                self.assertEqual(load_contract(DEMO / name).data["database"]["engine"], engine)


class BackupFormats(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_sqlite_file_and_gzip_are_recognised_and_other_files_are_not(self):
        db = self.tmp / "app.sqlite"
        con = sqlite3.connect(db)
        con.execute("create table t (a)")
        con.commit()
        con.close()
        self.assertIn("SQLite", databases.SQLite().check_format(db))
        gz = self.tmp / "app.sqlite.gz"
        gz.write_bytes(gzip.compress(db.read_bytes()))
        self.assertIn("gzip stream is complete", databases.SQLite().check_format(gz))
        fake = self.tmp / "fake.sqlite"
        fake.write_bytes(b"not a database")
        with self.assertRaises(ValueError):
            databases.SQLite().check_format(fake)

    def test_mongodump_archive_magic_is_checked_through_gzip(self):
        arc = self.tmp / "db.archive.gz"
        arc.write_bytes(gzip.compress(databases.MongoDB.ARCHIVE_MAGIC + b"rest"))
        self.assertIn("mongodump archive", databases.MongoDB().check_format(arc))
        bad = self.tmp / "db.archive"
        bad.write_bytes(b"{\"json\": true}")
        with self.assertRaises(ValueError):
            databases.MongoDB().check_format(bad)

    def test_truncated_gzip_is_caught(self):
        sql = self.tmp / "db.sql.gz"
        sql.write_bytes(gzip.compress(b"create table t (a int);" * 1000)[:-20])
        with self.assertRaises((EOFError, OSError, ValueError)):
            databases.MySQL().check_format(sql)


class Discovery(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def project(self, compose: str | None, files: dict[str, bytes]) -> Path:
        root = self.tmp / "app"
        night = root / "backups" / "night-1"
        night.mkdir(parents=True)
        if compose:
            (root / "compose.yaml").write_text(compose, encoding="utf-8")
        for name, data in files.items():
            (night / name).write_bytes(data)
        return root

    def test_mysql_compose_and_dump(self):
        dump = b"-- MySQL dump\nCREATE TABLE `docs` (`id` int, `file_path` varchar(255));\n"
        root = self.project(textwrap.dedent("""
            services:
              db: {image: "mysql:8.4", environment: {MYSQL_DATABASE: shop, MYSQL_USER: shop}}
              web: {build: ., ports: ["8080:8080"], environment: {DATABASE_URL: "mysql://shop:pw@db/shop"}}
        """), {"db.sql.gz": gzip.compress(dump), "uploads.tar.gz": b""})
        c = discover(root)["contract"]
        self.assertEqual(c["database"]["engine"], "mysql")
        self.assertEqual((c["database"]["name"], c["database"]["user"]), ("shop", "shop"))
        self.assertEqual(c["service"]["env"]["DATABASE_URL"], "mysql://{db.user}:{db.password}@{db.host}:{db.port}/{db.name}")
        self.assertEqual(c["checks"]["references"][0]["sql"], "select id, file_path from docs where file_path is not null")

    def test_mongodb_from_the_archive_alone(self):
        root = self.project(None, {"db.archive.gz": gzip.compress(databases.MongoDB.ARCHIVE_MAGIC)})
        c = discover(root)["contract"]
        self.assertEqual(c["database"]["engine"], "mongodb")
        self.assertEqual(c["checks"]["data"][0]["query"], "db.getCollectionNames().length")

    def test_sqlite_schema_is_read_without_changing_the_file(self):
        db = self.tmp / "seed.sqlite"
        con = sqlite3.connect(db)
        con.execute("create table invoices (number text, pdf_path text, created_at text)")
        con.commit()
        con.close()
        data = db.read_bytes()
        root = self.project(None, {"app.sqlite": data, "uploads.tar.gz": b""})
        result = discover(root)
        c = result["contract"]
        self.assertEqual(c["database"]["engine"], "sqlite")
        self.assertTrue(c["database"]["mount"].startswith("TODO"))  # the app's path is the user's to give
        self.assertEqual(c["checks"]["references"][0]["sql"],
                         "select number, pdf_path from invoices where pdf_path is not null")
        self.assertEqual((root / "backups" / "night-1" / "app.sqlite").read_bytes(), data)


# -- real drills -------------------------------------------------------------------------------------

def missing_for(engine: str) -> str | None:
    d = Docker()
    ok, info = d.available()
    if not ok:
        return info
    need = [databases.get(engine).default_image, "restoreproof-demo/billing-portal:1.1", PROBE_IMAGE]
    missing = [i for i in dict.fromkeys(need) if d.image_info(i) is None]
    return f"missing images {missing}" if missing else None


class EngineDrill:
    """The same Billing Portal drill on one engine: the broken night is PARTIAL with the exact 55 missing
    files, the good night is PASS, and nothing is left behind either time."""

    engine = ""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix=f"rp-{cls.engine}-"))
        shutil.copy(DEMO / f"restoreproof.{cls.engine}.yaml", cls.tmp / "restoreproof.yaml")
        subprocess.run([sys.executable, str(DEMO / "make_backups.py"), str(cls.tmp / f"backups-{cls.engine}"),
                        "--engine", cls.engine], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def drill(self, night):
        drill = Drill(load_contract(self.tmp / "restoreproof.yaml"), home=self.tmp / "home",
                      recovery_dir=self.tmp / f"backups-{self.engine}" / night)
        ev = drill.run()
        left = Docker().resources(ev["run_id"])
        self.assertFalse(any(left.values()), f"drill left resources behind: {left}")
        self.assertTrue(ev["cleanup"]["ok"])
        text = Path(ev["reports"]["evidence"]).read_text(encoding="utf-8")
        self.assertNotIn(drill.db_password, text)  # the throwaway password never reaches the evidence
        return ev

    def test_broken_backup_is_partial_with_the_missing_files(self):
        ev = self.drill("nightly-2026-11-06")
        self.assertEqual(ev["verdict"], "PARTIAL", ev["verdict_reason"])
        ref = next(c for c in ev["checks"] if c["kind"] == "reference")
        self.assertEqual((ref["evidence"]["missing"], ref["evidence"]["references"]), (55, 1204))
        self.assertEqual(ev["objectives"]["rpo_seconds"], 50940.0)

    def test_good_backup_passes(self):
        ev = self.drill("nightly-2026-11-07")
        self.assertEqual(ev["verdict"], "PASS", ev["verdict_reason"])
        restored = next(c for c in ev["checks"] if c["name"] == "database restored from backup")
        self.assertEqual(restored["evidence"]["engine"], self.engine)


def drill_case(engine):
    skip = missing_for(engine)
    cls = type(f"{engine.title()}Drill", (EngineDrill, unittest.TestCase), {"engine": engine})
    return unittest.skipIf(skip, f"{engine} drills skipped: {skip}")(cls)


SQLiteDrill = drill_case("sqlite")
MySQLDrill = drill_case("mysql")
MariaDBDrill = drill_case("mariadb")
MongoDBDrill = drill_case("mongodb")

if __name__ == "__main__":
    unittest.main()
