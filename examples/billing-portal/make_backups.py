"""Generate the Billing Portal demo backup sets.

Writes two recovery points, both made from the same deterministic dataset:

  backups/nightly-2026-11-06/   the backup job said "success", but the uploads
                                archive is missing every invoice PDF from the
                                last days of October (a bad exclude pattern)
  backups/nightly-2026-11-07/   a complete, corrected backup set

Each set has a database backup, an uploads archive (uploads.tar.gz) and the
backup job's own status file (backup-status.json). The database backup depends
on --engine:

  postgres (default)   db.sql.gz      plain SQL, as pg_dump writes it
  mysql, mariadb       db.sql.gz      plain SQL, as mysqldump writes it
  sqlite               db.sqlite      the database file itself
  mongodb              db.archive.gz  mongodump --archive --gzip (made in a
                                      throwaway mongo:7.0 container, so it
                                      needs Docker and that image)

Usage: python make_backups.py [output_dir] [--engine ENGINE]
"""

import argparse
import gzip
import hashlib
import io
import json
import random
import secrets
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

INVOICES = 1204
CUSTOMERS = 86
SEED = 20261106
# The broken backup's exclude pattern drops these folders.
MISSING_PREFIX = "invoices/2026/10/"
MISSING_FROM_DAY = 17

FIRST = ["Acme", "Globex", "Initech", "Umbrella", "Hooli", "Vandelay", "Stark", "Wayne",
         "Wonka", "Tyrell", "Cyberdyne", "Soylent", "Pied Piper", "Aperture", "Black Mesa",
         "Massive Dynamic", "Gringotts", "Oscorp", "Monarch", "Dunder"]
LAST = ["Labs", "Industries", "Logistics", "Foods", "Systems", "Partners", "Studio",
        "Analytics", "Supply", "Health"]


def pdf_bytes(number: str, customer: str, amount_cents: int) -> bytes:
    text = f"Invoice {number} - {customer} - ${amount_cents / 100:,.2f}"
    stream = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref))
    return out.getvalue()


def dataset():
    rng = random.Random(SEED)
    customers = []
    names = set()
    while len(customers) < CUSTOMERS:
        name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
        if name in names:
            continue
        names.add(name)
        customers.append((len(customers) + 1, name, name.lower().replace(" ", ".") + "@example.test"))
    start = date(2025, 11, 1)
    span = (date(2026, 11, 6) - start).days
    days = sorted(rng.randrange(span + 1) for _ in range(INVOICES))
    invoices = []
    for i, offset in enumerate(days):
        issued = start + timedelta(days=offset)
        number = f"INV-{1000 + i}"
        cust = customers[rng.randrange(CUSTOMERS)]
        amount = rng.randrange(4_900, 980_000)
        path = f"invoices/{issued:%Y/%m}/{number}.pdf"
        created = datetime(issued.year, issued.month, issued.day, 9, 0, tzinfo=timezone.utc) + timedelta(
            minutes=rng.randrange(0, 13 * 60))
        invoices.append((i + 1, number, cust[0], cust[1], amount, issued, path, created))
    return customers, invoices


def sql_dump(customers, invoices) -> bytes:
    def q(value):
        return str(value).replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n")

    lines = [
        "-- PostgreSQL database dump (Billing Portal, generated for the RestoreProof demo)",
        "SET client_encoding = 'UTF8';",
        "CREATE TABLE customers (id integer PRIMARY KEY, name text NOT NULL, email text NOT NULL);",
        "CREATE TABLE invoices (id integer PRIMARY KEY, number text UNIQUE NOT NULL,",
        "  customer_id integer NOT NULL REFERENCES customers(id), amount_cents integer NOT NULL,",
        "  currency text NOT NULL DEFAULT 'USD', issued_on date NOT NULL, pdf_path text,",
        "  created_at timestamptz NOT NULL);",
        "COPY customers (id, name, email) FROM stdin;",
    ]
    lines += [f"{c[0]}\t{q(c[1])}\t{q(c[2])}" for c in customers]
    lines += ["\\.", "COPY invoices (id, number, customer_id, amount_cents, currency, issued_on, pdf_path, created_at) FROM stdin;"]
    lines += [f"{v[0]}\t{v[1]}\t{v[2]}\t{v[4]}\tUSD\t{v[5].isoformat()}\t{v[6]}\t{v[7].isoformat()}" for v in invoices]
    lines += ["\\.", "CREATE INDEX invoices_customer_idx ON invoices (customer_id);", ""]
    return "\n".join(lines).encode()


def mysql_dump(customers, invoices, flavour: str) -> bytes:
    def q(value):
        return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"

    lines = [
        f"-- {flavour} dump (Billing Portal, generated for the RestoreProof demo)",
        "SET NAMES utf8mb4;",
        "CREATE TABLE customers (id INT PRIMARY KEY, name VARCHAR(200) NOT NULL, email VARCHAR(200) NOT NULL);",
        "CREATE TABLE invoices (id INT PRIMARY KEY, number VARCHAR(32) NOT NULL UNIQUE,",
        "  customer_id INT NOT NULL, amount_cents INT NOT NULL, currency VARCHAR(3) NOT NULL DEFAULT 'USD',",
        "  issued_on DATE NOT NULL, pdf_path VARCHAR(255), created_at DATETIME NOT NULL,",
        "  FOREIGN KEY (customer_id) REFERENCES customers (id));",
        "INSERT INTO customers VALUES " + ",".join(f"({c[0]},{q(c[1])},{q(c[2])})" for c in customers) + ";",
    ]
    for i in range(0, len(invoices), 200):
        rows = invoices[i:i + 200]
        lines.append("INSERT INTO invoices VALUES " + ",".join(
            f"({v[0]},{q(v[1])},{v[2]},{v[4]},'USD','{v[5].isoformat()}',{q(v[6])},"
            f"'{v[7].strftime('%Y-%m-%d %H:%M:%S')}')" for v in rows) + ";")
    lines += ["CREATE INDEX invoices_customer_idx ON invoices (customer_id);", ""]
    return "\n".join(lines).encode()


def sqlite_file(path: Path, customers, invoices) -> None:
    path.unlink(missing_ok=True)
    con = sqlite3.connect(path)
    with con:
        con.execute("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL)")
        con.execute("CREATE TABLE invoices (id INTEGER PRIMARY KEY, number TEXT UNIQUE NOT NULL, "
                    "customer_id INTEGER NOT NULL REFERENCES customers(id), amount_cents INTEGER NOT NULL, "
                    "currency TEXT NOT NULL DEFAULT 'USD', issued_on TEXT NOT NULL, pdf_path TEXT, "
                    "created_at TEXT NOT NULL)")
        con.executemany("INSERT INTO customers VALUES (?, ?, ?)", customers)
        con.executemany("INSERT INTO invoices VALUES (?, ?, ?, ?, 'USD', ?, ?, ?)",
                        [(v[0], v[1], v[2], v[4], v[5].isoformat(), v[6], v[7].isoformat()) for v in invoices])
        con.execute("CREATE INDEX invoices_customer_idx ON invoices (customer_id)")
    con.close()


def docker_binary() -> str:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from engine.docker import docker_binary as find

    found = find()
    if not found:
        sys.exit("Docker is needed to write a MongoDB backup (mongodump runs in a throwaway mongo:7.0 container).")
    return found


def mongo_archive(customers, invoices) -> bytes:
    """A real mongodump --archive --gzip of the dataset, made in a throwaway mongo:7.0 container."""
    docker = docker_binary()
    name = f"rp-make-backup-{secrets.token_hex(3)}"

    def run(args, data=None):
        r = subprocess.run([docker, *args], input=data, capture_output=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError(f"docker {args[0]} failed: {r.stderr.decode('utf-8', 'replace')[-300:]}")
        return r.stdout

    def date(d):
        return {"$date": (d if isinstance(d, datetime) else datetime(d.year, d.month, d.day, tzinfo=timezone.utc))
                .strftime("%Y-%m-%dT%H:%M:%SZ")}

    run(["run", "-d", "--rm", "--name", name, "--network", "none", "mongo:7.0"])
    try:
        for _ in range(120):
            ping = subprocess.run([docker, "exec", name, "mongosh", "--quiet", "--eval", "db.adminCommand({ping: 1}).ok"],
                                  capture_output=True, timeout=30)
            if ping.returncode == 0 and ping.stdout.strip() == b"1":
                break
            time.sleep(0.5)
        docs = {
            "customers": [{"_id": c[0], "name": c[1], "email": c[2]} for c in customers],
            "invoices": [{"_id": v[0], "number": v[1], "customer_id": v[2], "amount_cents": v[4], "currency": "USD",
                          "issued_on": date(v[5]), "pdf_path": v[6], "created_at": date(v[7])} for v in invoices],
        }
        for coll, rows in docs.items():
            run(["exec", "-i", name, "mongoimport", "--quiet", "--db", "billing", "--collection", coll, "--jsonArray"],
                json.dumps(rows).encode())
        run(["exec", name, "mongosh", "--quiet", "billing", "--eval",
             "db.invoices.createIndex({customer_id: 1}); db.invoices.createIndex({number: 1}, {unique: true})"])
        return run(["exec", name, "mongodump", "--quiet", "--db", "billing", "--archive", "--gzip"])
    finally:
        subprocess.run([docker, "rm", "-f", name], capture_output=True, timeout=60)


DB_FILE = {"postgres": "db.sql.gz", "mysql": "db.sql.gz", "mariadb": "db.sql.gz", "sqlite": "db.sqlite",
           "mongodb": "db.archive.gz"}


def write_database(engine: str, path: Path, customers, invoices, cache: dict) -> None:
    if engine == "sqlite":
        sqlite_file(path, customers, invoices)
        return
    if "bytes" not in cache:  # both nights hold the same database; make it once
        cache["bytes"] = (mongo_archive(customers, invoices) if engine == "mongodb"
                          else sql_dump(customers, invoices) if engine == "postgres"
                          else mysql_dump(customers, invoices, "MariaDB" if engine == "mariadb" else "MySQL"))
    if engine == "mongodb":
        path.write_bytes(cache["bytes"])
    else:
        write_gzip(path, cache["bytes"])


def write_gzip(path: Path, data: bytes):
    # mtime=0 keeps the archive bytes (and so their digests) reproducible.
    with open(path, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
        gz.write(data)


def write_uploads(path: Path, invoices, skip_missing: bool) -> int:
    count = 0
    with open(path, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for v in invoices:
                rel = v[6]
                if skip_missing and rel.startswith(MISSING_PREFIX) and v[5].day >= MISSING_FROM_DAY:
                    continue
                data = pdf_bytes(v[1], v[3], v[4])
                info = tarfile.TarInfo(rel)
                info.size = len(data)
                info.mtime = int(v[7].timestamp())
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(data))
                count += 1
    return count


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description="Write the Billing Portal demo backup sets.")
    parser.add_argument("output_dir", nargs="?", default=str(Path(__file__).parent / "backups"))
    parser.add_argument("--engine", default="postgres", choices=sorted(DB_FILE))
    args = parser.parse_args()
    root = Path(args.output_dir)
    customers, invoices = dataset()
    db_file = DB_FILE[args.engine]
    cache: dict = {}
    sets = [
        ("nightly-2026-11-06", True, "2026-11-06T23:00:00Z"),
        ("nightly-2026-11-07", False, "2026-11-07T23:00:00Z"),
    ]
    for name, broken, finished in sets:
        out = root / name
        out.mkdir(parents=True, exist_ok=True)
        write_database(args.engine, out / db_file, customers, invoices, cache)
        files = write_uploads(out / "uploads.tar.gz", invoices, broken)
        status = {
            "job": "billing-portal-nightly",
            "status": "success",
            "finished_at": finished,
            "artifacts": {
                db_file: {"bytes": (out / db_file).stat().st_size, "sha256": sha256(out / db_file)},
                "uploads.tar.gz": {"bytes": (out / "uploads.tar.gz").stat().st_size,
                                   "sha256": sha256(out / "uploads.tar.gz"), "files": files},
            },
        }
        (out / "backup-status.json").write_text(json.dumps(status, indent=2) + "\n")
        print(f"{name}: status=success, {args.engine} {db_file}, {files} upload files")


if __name__ == "__main__":
    main()
