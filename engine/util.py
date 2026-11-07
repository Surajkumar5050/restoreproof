"""Small shared helpers: YAML loading, hashing, durations, time and redaction."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def load_yaml_text(text: str) -> Any:
    """Parse YAML with whatever safe loader is available.

    Hermes ships ruamel.yaml; standalone installs usually have PyYAML. JSON is a
    subset of YAML, so it is the last resort.
    """
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text)
    except ImportError:
        pass
    try:
        from ruamel.yaml import YAML  # type: ignore

        return YAML(typ="safe", pure=True).load(text)
    except ImportError:
        pass
    return json.loads(text)


def dump_yaml_text(data: Any) -> str:
    try:
        import yaml  # type: ignore

        return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    except ImportError:
        pass
    try:
        import io

        from ruamel.yaml import YAML  # type: ignore

        buf = io.StringIO()
        y = YAML(typ="safe", pure=True)
        y.default_flow_style = False
        y.sort_base_mapping_type_on_output = False
        y.dump(data, buf)
        return buf.getvalue()
    except ImportError:
        return json.dumps(data, indent=2)


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(data: Any) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()


_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)?\s*$")
_UNIT = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, None: 1}


def parse_duration(value: Any) -> float | None:
    """'15m' -> 900.0, '24h' -> 86400.0, 30 -> 30.0. None when absent or invalid."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = _DURATION.match(str(value))
    if not m:
        return None
    return float(m.group(1)) * _UNIT[m.group(2)]


def human_duration(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    seconds = float(seconds)
    if seconds < 0.0005:
        return "<1ms"
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, sec = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return f"{minutes}m {sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h {minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    # psql prints offsets as "+00"; fromisoformat wants "+00:00".
    if re.search(r"[+-]\d\d$", text):
        text += ":00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Stopwatch:
    def __init__(self) -> None:
        self.start = time.monotonic()

    def elapsed(self) -> float:
        return time.monotonic() - self.start


class Redactor:
    """Replaces known secret values in any text that leaves the engine."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets = sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True)

    def add(self, value: str | None) -> None:
        if value and len(value) >= 4 and value not in self._secrets:
            self._secrets.append(value)
            self._secrets.sort(key=len, reverse=True)

    def __call__(self, text: Any) -> Any:
        if not isinstance(text, str):
            return text
        for s in self._secrets:
            text = text.replace(s, "[REDACTED]")
        return text


def tail(text: str, lines: int = 25, limit: int = 4000) -> str:
    out = "\n".join((text or "").splitlines()[-lines:])
    return out[-limit:]


def data_home() -> Path:
    """Where runs and evidence live.

    Inside Hermes the plugin passes plugin_data_dir("restoreproof"). Standalone
    runs use RESTOREPROOF_HOME or ~/.restoreproof.
    """
    env = os.environ.get("RESTOREPROOF_HOME")
    path = Path(env) if env else Path.home() / ".restoreproof"
    path.mkdir(parents=True, exist_ok=True)
    return path
