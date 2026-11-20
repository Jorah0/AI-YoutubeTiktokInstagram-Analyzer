"""Build dist/CreatorResearch/CreatorResearch.exe and prove it starts.

  python scripts/build_exe.py           build, then smoke-test the built program
  python scripts/build_exe.py --smoke   only smoke-test an existing build

The result is a folder, not a single file: a single-file exe would unpack several hundred megabytes
on every start. Copy or zip the whole dist/CreatorResearch folder; the exe needs what is beside it.
The smoke test starts the real exe (no browser), waits for the dashboard to answer, loads the page,
and runs one command through it. A build that fails the smoke test exits 1.
"""
import contextlib
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
PY = str(VENV) if VENV.is_file() else sys.executable
NAME = "CreatorResearch"
OUT = ROOT / "dist" / NAME
EXE = OUT / (NAME + (".exe" if os.name == "nt" else ""))
# Big packages that happen to be installed on this machine and that nothing here uses.
EXCLUDE = ["torch", "tensorflow", "transformers", "matplotlib", "scipy", "sklearn", "IPython", "notebook",
           "jupyter", "pytest", "PyInstaller", "tkinter"]


@contextlib.contextmanager
def kept_user_files(out: Path, stash: Path):
    """PyInstaller empties the output folder. If someone has been running the program from there,
    their database and keys are in it: move them aside for the build and put them back after."""
    names = [n for n in (".env", "data") if (out / n).exists()]
    if names:
        stash.mkdir(parents=True, exist_ok=True)
        for n in names:
            shutil.move(str(out / n), str(stash / n))
    try:
        yield
    finally:
        for n in names:
            out.mkdir(parents=True, exist_ok=True)
            shutil.move(str(stash / n), str(out / n))
        if names:
            stash.rmdir()


def lan_address() -> str | None:
    """This computer's address on the local network, if it has one (no packet is sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
        except OSError:
            return None


def build() -> None:
    with kept_user_files(OUT, ROOT / ".kept_user_files"):
        _build()


def _build() -> None:
    cmd = [PY, "-m", "PyInstaller", "--noconfirm", "--clean", "--name", NAME, "--onedir", "--console",
           "--collect-all", "streamlit",  # its web front end and package metadata are data, not imports
           "--collect-submodules", "creator_intel",  # app.py is run as a script, so its imports are not traced
           "--add-data", f"{ROOT / 'app.py'}{os.pathsep}.",
           "--add-data", f"{ROOT / 'launcher.py'}{os.pathsep}.",  # app.py imports it for the settings form
           "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build"), "--specpath", str(ROOT / "build")]
    for name in EXCLUDE:
        cmd += ["--exclude-module", name]
    subprocess.run(cmd + [str(ROOT / "launcher.py")], cwd=ROOT, check=True)
    shutil.copy(ROOT / ".env.example", OUT / ".env.example")
    (OUT / "READ ME FIRST.txt").write_text(
        "Creator research\n\n"
        f"Double-click {EXE.name}. A console window opens and the dashboard appears in your browser.\n"
        "Close the console window to stop it.\n\n"
        "Your work is saved in the data folder here. Back that folder up.\n"
        "Keys are optional. To add them, copy .env.example to .env in this folder and fill it in.\n"
        "The dashboard is only reachable from this computer.\n\n"
        "Once you have used it, this folder holds your keys (.env) and your data. Do not zip or share it.\n"
        "To give the program to someone else, share a fresh copy that has never been run.\n\n"
        f"Commands also work, for example:  {EXE.name} weekly 1 -o digest.md\n", encoding="utf-8")


def smoke() -> bool:
    if not EXE.is_file():
        print(f"FAIL  {EXE} does not exist")
        return False
    work = Path(tempfile.mkdtemp(prefix="creator_smoke_"))
    # the smoke test must never be able to spend quota or post an alert: no keys, no webhook
    env = {k: v for k, v in os.environ.items()
           if not (k.endswith("_API_KEY") or k.startswith(("ANTHROPIC_", "CI_")))}
    env |= {"CI_DB": str(work / "smoke.db"), "CI_NO_BROWSER": "1"}
    ok = True

    def check(name, passed, detail=""):
        nonlocal ok
        ok = ok and passed
        print(f"{'PASS' if passed else 'FAIL'}  {name}{': ' + detail if detail and not passed else ''}", flush=True)

    done = subprocess.run([str(EXE), "demo"], env=env, capture_output=True, text=True, timeout=180)
    check("exe runs a command (demo)", done.returncode == 0 and "Demo brief" in done.stdout, done.stderr[-300:])
    done = subprocess.run([str(EXE), "doctor"], env=env, capture_output=True, text=True, timeout=180)
    check("exe reads its database (doctor)", "Database: ok: 1 briefs, 10 creators" in done.stdout, done.stdout[:200])

    log = work / "server.log"
    with log.open("w") as out:
        server = subprocess.Popen([str(EXE)], env=env, stdout=out, stderr=subprocess.STDOUT)
    try:
        url, page = None, ""
        for _ in range(90):
            time.sleep(1)
            if server.poll() is not None:
                break
            found = re.search(r"http://127\.0\.0\.1:(\d+)", log.read_text(errors="replace"))
            if not found:
                continue
            url = found.group(0)
            try:
                if urllib.request.urlopen(url + "/_stcore/health", timeout=3).read() == b"ok":
                    page = urllib.request.urlopen(url, timeout=10).read().decode(errors="replace")
                    break
            except OSError:
                pass
        check("dashboard answers its health check", bool(page), url or log.read_text(errors="replace")[-600:])
        check("dashboard serves the app page", "streamlit" in page.lower())
        address = lan_address()
        if url and address and not address.startswith("127."):
            try:  # the real test: from this computer's network address the port must be closed
                socket.create_connection((address, int(url.rsplit(":", 1)[1])), timeout=2).close()
                check("dashboard is closed to the network", False, f"reachable at {address}")
            except OSError:
                check("dashboard is closed to the network", True)
        else:
            print("NOT CHECKED  dashboard is closed to the network: this computer has no network address")
    finally:
        if os.name == "nt":  # the exe starts a child process; take the whole tree down
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(server.pid)], capture_output=True)
        else:
            server.terminate()
        shutil.rmtree(work, ignore_errors=True)
    return ok


def main() -> int:
    if "--smoke" not in sys.argv:
        build()
    passed = smoke()
    size = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file()) / 1e6 if OUT.is_dir() else 0
    print(f"\n{'OK' if passed else 'NOT OK'}: {EXE} ({size:.0f} MB folder)")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
