"""One command for "is this change done?": lint, secret scan, every test (including the app render test).

  python scripts/verify.py

Prints one line per step; exit 1 when a step fails. A step whose tool is missing, or a test run in which
nothing ran, is SKIPPED and said so, never reported as a pass. Uses the repo's .venv when there is one.
Adapted from the engineering-loop template; fingerprint() must stay identical to the Stop hook's."""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
PY = str(VENV) if VENV.is_file() else sys.executable


def has(mod: str) -> bool:
    return subprocess.run([PY, "-c", f"import {mod}"], capture_output=True).returncode == 0


def steps() -> list[tuple[str, list[str] | None, str]]:
    return [
        ("lint (ruff)", [PY, "-m", "ruff", "check", "."] if has("ruff") else None, "ruff not installed"),
        ("secret scan", [PY, "scripts/secret_scan.py"], ""),
        ("tests (pytest)", [PY, "-m", "pytest", "-q", "-rs"] if has("pytest") else None, "pytest not installed"),
    ]


def not_a_pass(text: str) -> str:
    """Why a run that exited 0 still is not a pass ('' when it is)."""
    if re.search(r"no tests ran", text):
        return "ran 0 tests"
    if m := re.search(r"(\d+) skipped", text):
        reasons = "; ".join(re.findall(r"^SKIPPED .*$", text, re.M))
        return f"{m.group(1)} tests skipped: {reasons or 'see pytest -rs'}"
    return ""


def run() -> list[dict]:
    results = []
    for name, cmd, why in steps():
        t = time.time()
        if cmd is None:
            status, detail = "SKIPPED", why
        else:
            p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                               env=os.environ | {"PYTHONIOENCODING": "utf-8"})
            out = p.stdout + "\n" + p.stderr
            tail = [line for line in out.strip().splitlines() if line.strip()][-8:]
            if p.returncode != 0:
                status, detail = "FAIL", "\n    ".join(tail)
            elif why := not_a_pass(out):
                status, detail = "SKIPPED", why
            else:
                status, detail = "PASS", tail[-1] if tail else ""
        results.append({"step": name, "status": status, "seconds": round(time.time() - t, 1), "detail": detail})
        print(f"{status:8} {name} ({results[-1]['seconds']} s): {detail}", flush=True)
    return results


def _git(*args) -> bytes:
    p = subprocess.run(["git", *args], cwd=ROOT, capture_output=True)
    return p.stdout if p.returncode == 0 else b""


def fingerprint() -> str:
    """Hash of the tree beyond HEAD (tracked changes + untracked, not-ignored files)."""
    h = hashlib.sha256(_git("diff", "HEAD", "--binary", "--no-ext-diff", "--no-color"))
    untracked = _git("ls-files", "-o", "--exclude-standard", "-z").decode("utf-8", "replace").split("\0")
    for rel in sorted(p for p in untracked if p):
        h.update(rel.encode() + b"\0")
        try:
            h.update((ROOT / rel).read_bytes())
        except OSError:
            pass
    return h.hexdigest()


def main() -> int:
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    fp = fingerprint()
    results = run()
    moved = fingerprint() != fp
    failed = [r["step"] for r in results if r["status"] == "FAIL"]
    skipped = [r["step"] for r in results if r["status"] == "SKIPPED"]
    (ROOT / ".verify").mkdir(exist_ok=True)
    (ROOT / ".verify" / "last.json").write_text(json.dumps({
        "fingerprint": fp, "passed": not failed and not skipped and not moved, "tree_changed_during_run": moved,
        "skipped": skipped, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}), encoding="utf-8")
    if moved:
        print("\nverify: the tree changed while verify ran: run it again")
    print(f"\nverify: {len(results) - len(failed) - len(skipped)} passed, {len(failed)} failed, {len(skipped)} skipped")
    return 1 if failed or skipped else 0  # a skipped step is not a pass


if __name__ == "__main__":
    sys.exit(main())
