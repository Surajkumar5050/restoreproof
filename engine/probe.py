"""Journey probe: runs INSIDE the drill's isolated network, in a throwaway
python:alpine container, so the restored app is never published to the host.

It reads a JSON job on stdin and prints one JSON result on stdout. Standard
library only. The engine sends this file's source as the program to run.
"""

import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request

MAX_BODY = 5 * 1024 * 1024


def fetch(base, method, path, headers, body, timeout):
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(base + path, data=data, method=method, headers=hdrs)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(MAX_BODY + 1)
            status, rh = resp.status, dict(resp.headers.items())
    except urllib.error.HTTPError as exc:
        raw = exc.read(MAX_BODY + 1)
        status, rh = exc.code, dict(exc.headers.items())
    return status, {k.lower(): v for k, v in rh.items()}, raw[:MAX_BODY], time.monotonic() - started


def json_path(doc, expr):
    cur = doc
    for part in expr[2:].split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


def subset(expected, actual):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and subset(v, actual[k]) for k, v in expected.items())
    return expected == actual


def fill(text, variables):
    if not isinstance(text, str):
        return text
    return re.sub(r"\{([a-z_][a-z0-9_]*)\}", lambda m: str(variables.get(m.group(1), m.group(0))), text)


def health(job):
    deadline = time.monotonic() + job["timeout"]
    last = "no response yet"
    attempts = 0
    while time.monotonic() < deadline:
        attempts += 1
        try:
            status, _, raw, elapsed = fetch(job["base"], "GET", job["path"], {}, None, 3)
            if 200 <= status < 300:
                return {"ok": True, "status": status, "attempts": attempts, "elapsed": round(elapsed, 3)}
            last = f"HTTP {status}: {raw[:200].decode('utf-8', 'replace')}"
        except Exception as exc:  # connection refused while the app boots
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(1)
    return {"ok": False, "attempts": attempts, "error": last}


def journey(job):
    variables, steps = {}, []
    for step in job["steps"]:
        path = fill(step["path"], variables)
        rec = {"name": step["name"], "method": step["method"], "path": path, "checks": []}
        steps.append(rec)
        try:
            status, headers, raw, elapsed = fetch(job["base"], step["method"], path, step.get("headers"),
                                                  step.get("json"), job.get("step_timeout", 10))
        except Exception as exc:
            rec.update(ok=False, error=f"{type(exc).__name__}: {exc}")
            return {"ok": False, "steps": steps}
        rec.update(status=status, elapsed=round(elapsed, 3), bytes=len(raw),
                   sha256=hashlib.sha256(raw).hexdigest(),
                   content_type=headers.get("content-type", ""),
                   body_preview=raw[:300].decode("utf-8", "replace"))
        doc = None
        try:
            doc = json.loads(raw) if raw[:1] in (b"{", b"[") else None
        except ValueError:
            doc = None

        def check(label, passed, detail=""):
            rec["checks"].append({"check": label, "ok": bool(passed), "detail": detail})

        check(f"status {step['expect_status']}", status == step["expect_status"], f"got {status}")
        if step.get("expect_json") is not None:
            check("JSON fields " + json.dumps(step["expect_json"]), doc is not None and subset(step["expect_json"], doc),
                  json.dumps(step["expect_json"]))
        for name, want in (step.get("expect_header") or {}).items():
            got = headers.get(name.lower(), "")
            check(f"{name.lower()} {want}", str(want).lower() in got.lower(), f"got {got!r}")
        if step.get("expect_body_contains"):
            check(f"body containing {step['expect_body_contains']!r}",
                  str(step["expect_body_contains"]).encode() in raw)
        if step.get("expect_body_prefix"):
            check(f"body starting {step['expect_body_prefix']!r}",
                  raw.startswith(str(step["expect_body_prefix"]).encode()))
        for var, expr in (step.get("capture") or {}).items():
            value = json_path(doc, expr) if doc is not None else None
            check(f"a value at {expr}", value is not None, repr(value)[:120])
            if value is not None:
                variables[var] = value
        rec["ok"] = all(c["ok"] for c in rec["checks"])
        if not rec["ok"]:
            return {"ok": False, "steps": steps}
    return {"ok": True, "steps": steps}


if __name__ == "__main__":
    job = json.loads(sys.stdin.readline() or sys.stdin.read())
    result = health(job) if job["mode"] == "health" else journey(job)
    sys.stdout.write(json.dumps(result))
