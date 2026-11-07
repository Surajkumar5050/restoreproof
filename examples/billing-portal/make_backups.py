"""Generate the Billing Portal demo backup sets.

Writes two recovery points, both made from the same deterministic dataset:

  backups/nightly-2026-11-06/   the backup job said "success", but the uploads
                                archive is missing every invoice PDF from the
                                last days of October (a bad exclude pattern)
  backups/nightly-2026-11-07/   a complete, corrected backup set

Each set has a plain-SQL PostgreSQL dump (db.sql.gz), an uploads archive
(uploads.tar.gz) and the backup job's own status file (backup-status.json).

Usage: python make_backups.py [output_dir]
"""

import gzip
import hashlib
import io
import json
import random
import sys
import tarfile
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
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "backups"
    customers, invoices = dataset()
    dump = sql_dump(customers, invoices)
    sets = [
        ("nightly-2026-11-06", True, "2026-11-06T23:00:00Z"),
        ("nightly-2026-11-07", False, "2026-11-07T23:00:00Z"),
    ]
    for name, broken, finished in sets:
        out = root / name
        out.mkdir(parents=True, exist_ok=True)
        write_gzip(out / "db.sql.gz", dump)
        files = write_uploads(out / "uploads.tar.gz", invoices, broken)
        status = {
            "job": "billing-portal-nightly",
            "status": "success",
            "finished_at": finished,
            "artifacts": {
                "db.sql.gz": {"bytes": (out / "db.sql.gz").stat().st_size, "sha256": sha256(out / "db.sql.gz")},
                "uploads.tar.gz": {"bytes": (out / "uploads.tar.gz").stat().st_size,
                                   "sha256": sha256(out / "uploads.tar.gz"), "files": files},
            },
        }
        (out / "backup-status.json").write_text(json.dumps(status, indent=2) + "\n")
        print(f"{name}: status=success, {files} upload files")


if __name__ == "__main__":
    main()
