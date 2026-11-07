"""Safe inspection and extraction of file/upload archives.

Every member is checked before a single byte is written: no absolute paths, no
'..', no links, no devices, and bounded size, member count and expansion ratio.
Extraction then uses Python's 'data' filter as a second line of defence.
"""

from __future__ import annotations

import hashlib
import posixpath
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

MAX_RATIO = 200  # uncompressed bytes per archive byte before we call it a bomb


@dataclass
class ArchiveReport:
    ok: bool
    members: int = 0
    files: int = 0
    total_bytes: int = 0
    problems: list[str] = field(default_factory=list)


def _member_problem(m: tarfile.TarInfo) -> str | None:
    name = m.name
    if name.startswith(("/", "\\")) or (len(name) > 1 and name[1] == ":"):
        return f"absolute path: {name}"
    norm = posixpath.normpath(name.replace("\\", "/"))
    if norm == ".." or norm.startswith("../") or "/../" in f"/{norm}/":
        return f"path traversal: {name}"
    if m.issym() or m.islnk():
        return f"link member not allowed: {name} -> {m.linkname}"
    if m.isdev() or m.isfifo() or m.ischr() or m.isblk():
        return f"device or fifo member not allowed: {name}"
    if not (m.isfile() or m.isdir()):
        return f"unsupported member type: {name}"
    return None


def inspect(path: Path, *, max_bytes: int, max_members: int) -> ArchiveReport:
    report = ArchiveReport(ok=False)
    compressed = max(path.stat().st_size, 1)
    try:
        with tarfile.open(path, "r:*") as tar:
            for m in tar:
                report.members += 1
                problem = _member_problem(m)
                if problem:
                    report.problems.append(problem)
                    if len(report.problems) >= 20:
                        break
                if m.isfile():
                    report.files += 1
                    report.total_bytes += m.size
                if report.members > max_members:
                    report.problems.append(f"more than {max_members} members")
                    break
                if report.total_bytes > max_bytes:
                    report.problems.append(f"expands beyond the {max_bytes} byte limit")
                    break
    except (tarfile.TarError, EOFError, OSError) as exc:
        report.problems.append(f"archive is unreadable or truncated: {exc}")
        return report
    if report.total_bytes > 1024 * 1024 and report.total_bytes / compressed > MAX_RATIO:
        report.problems.append(
            f"suspicious expansion ratio {report.total_bytes / compressed:.0f}:1 (limit {MAX_RATIO}:1)")
    report.ok = not report.problems
    return report


def _tell(callback: Callable[[int, int], None] | None, done: int, total: int) -> None:
    if callback is not None:
        try:
            callback(done, total)
        except Exception:
            pass


def extract(path: Path, dest: Path, progress: Callable[[int, int], None] | None = None,
            hashed: Callable[[int, int], None] | None = None) -> dict[str, dict]:
    """Extract into an empty directory and return {relative path: {sha256, bytes}}.

    progress(done, total) is called after each regular file is written and hashed(done, total) after
    each one is checksummed, so a caller can show real counters. Both must be cheap; errors they raise
    are ignored."""
    dest.mkdir(parents=True, exist_ok=True)
    if any(dest.iterdir()):
        raise RuntimeError(f"extraction target is not empty: {dest}")
    with tarfile.open(path, "r:*") as tar:
        members = [m for m in tar.getmembers() if _member_problem(m) is None]
        total = sum(1 for m in members if m.isfile())
        done = 0
        for m in members:
            tar.extract(m, dest, filter="data")
            if m.isfile():
                done += 1
                _tell(progress, done, total)
    files = [f for f in dest.rglob("*") if f.is_file()]
    manifest: dict[str, dict] = {}
    for i, f in enumerate(files, 1):
        rel = f.relative_to(dest).as_posix()
        h = hashlib.sha256(f.read_bytes()).hexdigest()
        manifest[rel] = {"sha256": h, "bytes": f.stat().st_size}
        _tell(hashed, i, len(files))
    return manifest
