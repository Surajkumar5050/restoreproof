"""The databases a drill can restore: one small adapter per engine.

Each adapter knows five things about its engine: the throwaway server's environment, how to tell it
is ready, how to check a backup file before touching Docker, how to load that backup, and how to run
one read-only query and return its rows. Queries come from the contract and run only inside the
drill's own throwaway container.

Passwords never appear on a host command line: they live in the container's environment (written
through an env file) and every command that needs one expands it inside the container.
"""

from __future__ import annotations

import gzip
from pathlib import Path

from .docker import Result

SEP = "\x1f"


def gzip_complete(path: Path, what: str) -> str:
    """Read a gzip stream to its end; raises if it is truncated or corrupt."""
    total = 0
    with gzip.open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            total += len(chunk)
    return f"gzip stream is complete ({total:,} bytes of {what})"


def is_gzip(path: Path) -> bool:
    with open(path, "rb") as fh:
        return fh.read(2) == b"\x1f\x8b"


def head(path: Path, n: int) -> bytes:
    """The first n bytes of the file's content, through gzip when it is compressed."""
    opener = gzip.open if is_gzip(path) else open
    with opener(path, "rb") as fh:
        return fh.read(n)


def _ident(name: str, quote: str) -> str:
    return quote + name.replace(quote, quote * 2) + quote


class Engine:
    key = ""
    label = ""
    port: int | None = None
    default_image = ""
    artifact_suffixes: tuple[str, ...] = ()
    query_language = "SQL"
    units = ("tables", "rows")
    server = True  # False: there is no database server (SQLite); the container only holds the file

    def container_env(self, db: dict, password: str) -> dict[str, str]:
        return {}

    def container_command(self, timeout: int) -> list[str]:
        return []

    def ready_command(self, db: dict) -> list[str] | None:
        return None

    def check_format(self, path: Path) -> str:
        raise NotImplementedError

    def restore(self, drill, db: dict, art: Path) -> Result:
        raise NotImplementedError

    def query(self, drill, db: dict, text: str, timeout: float = 120) -> Result:
        raise NotImplementedError

    def inventory(self, drill, db: dict) -> dict[str, int | None]:
        raise NotImplementedError

    def deliver(self, drill, db: dict) -> Result | None:
        """Hand the restored database to the app container before it starts (SQLite only)."""
        return None

    def env_values(self, drill, db: dict) -> dict[str, str]:
        return {"db.host": drill.db_name, "db.port": str(self.port), "db.user": db["user"],
                "db.password": drill.db_password, "db.name": db["name"]}

    def _count_all(self, drill, db, tables: list[str], count_sql) -> dict[str, int | None]:
        counts: dict[str, int | None] = {}
        for t in tables[:60]:
            c = self.query(drill, db, count_sql(t))
            value = c.out.strip().split("\n")[0] if c.ok else ""
            counts[t] = int(value) if value.isdigit() else None
        return counts


class Postgres(Engine):
    key = "postgres"
    label = "PostgreSQL"
    port = 5432
    default_image = "postgres:16-alpine"
    artifact_suffixes = (".sql", ".sql.gz", ".dump", ".pgdump")

    def container_env(self, db, password):
        return {"POSTGRES_PASSWORD": password, "POSTGRES_USER": db["user"], "POSTGRES_DB": db["name"]}

    def ready_command(self, db):
        # TCP readiness only succeeds after the image's init phase (its temporary server is socket-only).
        return ["pg_isready", "-q", "-h", "127.0.0.1", "-U", db["user"]]

    def check_format(self, path):
        name = path.name.lower()
        if name.endswith(".gz"):
            return gzip_complete(path, "SQL")
        if name.endswith((".dump", ".pgdump")):
            with open(path, "rb") as fh:
                if fh.read(5) != b"PGDMP":
                    raise ValueError("not a pg_dump custom-format archive (missing PGDMP header)")
            return "pg_dump custom-format header present"
        return "plain SQL file"

    def restore(self, drill, db, art):
        name = art.name.lower()
        timeout = drill.d["limits"]["timeout"]
        if name.endswith(".gz"):
            return drill.dk(["exec", "-i", drill.db_name, "sh", "-c",
                             'gunzip -c | psql -X -q -v ON_ERROR_STOP=1 -U "$1" -d "$2"', "sh", db["user"], db["name"]],
                            timeout=timeout, stdin_file=art)
        if name.endswith(".sql"):
            return drill.dk(["exec", "-i", drill.db_name, "psql", "-X", "-q", "-v", "ON_ERROR_STOP=1",
                             "-U", db["user"], "-d", db["name"]], timeout=timeout, stdin_file=art)
        cp = drill.dk(["cp", str(art), f"{drill.db_name}:/tmp/restore.dump"], timeout=300)
        if not cp.ok:
            return cp
        return drill.dk(["exec", drill.db_name, "pg_restore", "--no-owner", "--no-privileges", "--exit-on-error",
                         "-U", db["user"], "-d", db["name"], "/tmp/restore.dump"], timeout=timeout)

    def query(self, drill, db, text, timeout=120):
        # Over the container's local socket (trust auth inside the throwaway container).
        return drill.dk(["exec", drill.db_name, "psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-At", "-F", SEP,
                         "-U", db["user"], "-d", db["name"], "-c", text], timeout=timeout)

    def inventory(self, drill, db):
        r = self.query(drill, db, "select table_name from information_schema.tables where table_schema = 'public' "
                                  "and table_type = 'BASE TABLE' order by 1")
        tables = [x for x in r.out.split("\n") if x] if r.ok else []
        return self._count_all(drill, db, tables, lambda t: f"select count(*) from public.{_ident(t, chr(34))}")


class MySQL(Engine):
    key = "mysql"
    label = "MySQL"
    port = 3306
    default_image = "mysql:8.4"
    artifact_suffixes = (".sql", ".sql.gz")
    # The client is `mariadb` in MariaDB images and `mysql` in MySQL ones; the password is expanded
    # inside the container from its own environment.
    CLIENT = 'c=$(command -v mariadb || command -v mysql); export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"; '

    def container_env(self, db, password):
        env = {"MYSQL_ROOT_PASSWORD": password, "MYSQL_DATABASE": db["name"]}
        if db["user"] != "root":
            env.update(MYSQL_USER=db["user"], MYSQL_PASSWORD=password)
        return env

    def ready_command(self, db):
        # The image's init phase runs a server with networking off, so a TCP ping answers only after it.
        return ["sh", "-c", 'a=$(command -v mariadb-admin || command -v mysqladmin); '
                            'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" "$a" ping -h127.0.0.1 -uroot --silent']

    def check_format(self, path):
        if path.name.lower().endswith(".gz"):
            return gzip_complete(path, "SQL")
        return "plain SQL file"

    def restore(self, drill, db, art):
        unzip = "gunzip -c | " if art.name.lower().endswith(".gz") else ""
        return drill.dk(["exec", "-i", drill.db_name, "sh", "-c", self.CLIENT + unzip + '"$c" -uroot "$1"',
                         "sh", db["name"]], timeout=drill.d["limits"]["timeout"], stdin_file=art)

    def query(self, drill, db, text, timeout=120):
        r = drill.dk(["exec", drill.db_name, "sh", "-c", self.CLIENT + 'exec "$c" -uroot -N -B --raw "$1" -e "$2"',
                      "sh", db["name"], text], timeout=timeout)
        return Result(r.code, r.out.replace("\r\n", "\n").replace("\t", SEP), r.err)

    def inventory(self, drill, db):
        r = self.query(drill, db, "select table_name from information_schema.tables where table_schema = database() "
                                  "and table_type = 'BASE TABLE' order by 1")
        tables = [x for x in r.out.split("\n") if x] if r.ok else []
        return self._count_all(drill, db, tables, lambda t: f"select count(*) from {_ident(t, '`')}")


class MariaDB(MySQL):
    key = "mariadb"
    label = "MariaDB"
    default_image = "mariadb:11.4"


class MongoDB(Engine):
    key = "mongodb"
    label = "MongoDB"
    port = 27017
    default_image = "mongo:7.0"
    artifact_suffixes = (".archive", ".archive.gz", ".agz")
    query_language = "mongosh expression"
    units = ("collections", "documents")
    ARCHIVE_MAGIC = b"\x6d\xe2\x99\x81"
    # Connect over the container's own address: during the image's init phase mongod listens on
    # localhost only, so this answers once the real server is up.
    CONNECT = 'h=$(hostname -i | cut -d" " -f1); '
    AUTH = ('--host "$h" -u "$MONGO_INITDB_ROOT_USERNAME" -p "$MONGO_INITDB_ROOT_PASSWORD" '
            '--authenticationDatabase admin')
    # Prints each result row on its own line, columns joined by SEP. Dates print as ISO 8601.
    WRAP = ("const __v = v => v instanceof Date ? v.toISOString() : (v === null || v === undefined ? '' : "
            "(typeof v === 'object' ? EJSON.stringify(v) : String(v)));\n"
            "let __r = (%s);\n"
            "if (__r && typeof __r.toArray === 'function') __r = __r.toArray();\n"
            "for (const __x of (Array.isArray(__r) ? __r : [__r])) "
            "print(Array.isArray(__x) ? __x.map(__v).join('\\x1f') : __v(__x));")

    def container_env(self, db, password):
        return {"MONGO_INITDB_ROOT_USERNAME": db["user"], "MONGO_INITDB_ROOT_PASSWORD": password,
                "MONGO_INITDB_DATABASE": db["name"]}

    def ready_command(self, db):
        return ["sh", "-c", self.CONNECT + f'mongosh --quiet {self.AUTH} --eval "db.adminCommand({{ping: 1}}).ok"']

    def check_format(self, path):
        if head(path, 4) != self.ARCHIVE_MAGIC:
            raise ValueError("not a mongodump archive (make it with mongodump --archive, optionally --gzip)")
        if is_gzip(path):
            return "mongodump archive, " + gzip_complete(path, "archive")
        return "mongodump archive header present"

    def restore(self, drill, db, art):
        gz = " --gzip" if is_gzip(art) else ""
        return drill.dk(["exec", "-i", drill.db_name, "sh", "-c",
                         self.CONNECT + f"mongorestore --quiet {self.AUTH} --archive --drop{gz}"],
                        timeout=drill.d["limits"]["timeout"], stdin_file=art)

    def query(self, drill, db, text, timeout=120):
        return drill.dk(["exec", drill.db_name, "sh", "-c",
                         self.CONNECT + f'exec mongosh --quiet {self.AUTH} "$1" --eval "$2"',
                         "sh", db["name"], self.WRAP % text], timeout=timeout)

    def inventory(self, drill, db):
        r = self.query(drill, db, "db.getCollectionNames().sort().map(c => [c, db.getCollection(c).countDocuments()])")
        counts: dict[str, int | None] = {}
        for line in (r.out.split("\n") if r.ok else [])[:60]:
            if SEP in line:
                name, n = line.split(SEP, 1)
                counts[name] = int(n) if n.strip().isdigit() else None
        return counts


SQLITE_QUERY = (
    "import sqlite3, sys\n"
    "con = sqlite3.connect('file:' + sys.argv[1] + '?mode=ro', uri=True)\n"
    "for row in con.execute(sys.argv[2]):\n"
    "    print('\\x1f'.join('' if v is None else str(v) for v in row))\n")


class SQLite(Engine):
    key = "sqlite"
    label = "SQLite"
    port = None
    default_image = "python:3.12-alpine"  # only to read the file: it has Python's sqlite3
    artifact_suffixes = (".db", ".sqlite", ".sqlite3", ".db.gz", ".sqlite.gz", ".sqlite3.gz")
    server = False
    INSIDE = "/restore/database.sqlite"
    HEADER = b"SQLite format 3\x00"

    def container_command(self, timeout):
        return ["sleep", str(timeout)]

    def check_format(self, path):
        if head(path, 16) != self.HEADER:
            raise ValueError("not an SQLite database file (missing 'SQLite format 3' header)")
        if is_gzip(path):
            return "SQLite database, " + gzip_complete(path, "database")
        return "SQLite database header present"

    def local_file(self, drill, art: Path) -> Path:
        """The plain database file: the artifact itself, or a decompressed copy in scratch."""
        if not is_gzip(art):
            return art
        out = drill.scratch / "database.sqlite"
        if not out.is_file():
            with gzip.open(art, "rb") as src, open(out, "wb") as dst:
                while chunk := src.read(1 << 20):
                    dst.write(chunk)
        return out

    def restore(self, drill, db, art):
        local = self.local_file(drill, art)
        mk = drill.dk(["exec", drill.db_name, "mkdir", "-p", "/restore"], timeout=30)
        if not mk.ok:
            return mk
        cp = drill.dk(["cp", str(local), f"{drill.db_name}:{self.INSIDE}"], timeout=300)
        if not cp.ok:
            return cp
        check = self.query(drill, db, "pragma integrity_check", timeout=drill.d["limits"]["timeout"])
        if not check.ok:
            return check
        verdict = check.out.strip().split("\n")[0]
        if verdict != "ok":
            return Result(1, "", f"integrity_check reported: {check.out.strip()[:300]}")
        return check

    def query(self, drill, db, text, timeout=120):
        return drill.dk(["exec", drill.db_name, "python", "-c", SQLITE_QUERY, self.INSIDE, text], timeout=timeout)

    def inventory(self, drill, db):
        r = self.query(drill, db, "select name from sqlite_master where type = 'table' "
                                  "and name not like 'sqlite_%' order by 1")
        tables = [x for x in r.out.split("\n") if x] if r.ok else []
        return self._count_all(drill, db, tables, lambda t: f"select count(*) from {_ident(t, chr(34))}")

    def deliver(self, drill, db):
        art = drill.recovery_dir / db["artifact"]
        return drill.dk(["cp", str(self.local_file(drill, art)), f"{drill.app_name}:{db['mount']}"], timeout=600)

    def env_values(self, drill, db):
        return {"db.path": db["mount"]}


ENGINES: dict[str, Engine] = {e.key: e for e in (Postgres(), MySQL(), MariaDB(), MongoDB(), SQLite())}


def get(key: str) -> Engine:
    return ENGINES[key]
