"""Entry point of the packaged program (CreatorResearch.exe), and usable as `python launcher.py`.

Started with no arguments it opens the dashboard in your browser. Started with arguments it runs a
command instead, so a scheduled task can call `CreatorResearch.exe weekly 1 -o digest.md`.

Beside the program it keeps `data\\creator_intel.db` (your work) and reads an optional `.env` file
(your keys). Closing the console window stops the dashboard.
"""
import hmac
import math
import os
import re
import socket
import sys
import threading
import time
from pathlib import Path


def home() -> Path:
    """Where the user's data and .env live: the exe's folder when packaged, the repo otherwise."""
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def bundled() -> Path:
    """Where app.py sits: inside the package's unpacked files when packaged, the repo otherwise."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


# Every name a .env file may set, one by one: this program's own settings and nothing else. A .env that
# came with someone else's copy must not be able to set a proxy, a certificate file or another service's
# address. The value is the name a setting belongs to: it counts only when that one is set in the same
# file, so a stray line cannot change how a service you have not set up would behave, nor use a key the
# file did not bring. None = stands alone. A new setting must be added here or the exe will ignore it.
ENV_NAMES = {
    "BRAVE_API_KEY": None, "YOUTUBE_API_KEY": None, "ANTHROPIC_API_KEY": None, "APIFY_TOKEN": None,
    "CI_DB": None, "CI_NO_BROWSER": None, "CI_WEBHOOK_URL": None, "CI_MODEL": None, "CI_PASSWORD": None,
    "CI_BRAVE_USD_PER_REQUEST": "BRAVE_API_KEY",
    "CI_YOUTUBE_RIGHTS": "YOUTUBE_API_KEY",  # can only widen (the default is store only), so it needs its key
    "CI_BROKER_RIGHTS": None,  # can only narrow (the default is everything), so it must never be dropped
    "CI_APIFY_TIKTOK": "APIFY_TOKEN", "CI_APIFY_INSTAGRAM": "APIFY_TOKEN", "CI_APIFY_YOUTUBE": "APIFY_TOKEN",
    "CI_AI_URL": "CI_AI_MODEL",  # a server with no model named cannot answer
    "CI_AI_MODEL": "CI_AI_URL", "CI_AI_KEY": "CI_AI_URL",
}


# Characters no value may hold: control characters, and the Unicode line and paragraph separators. A pasted
# value carrying one of the latter could otherwise be read back as several lines, that is, several settings.
UNSAFE = re.compile("[\x00-\x1f\x7f-\x9f  ]")


def _lines(text: str) -> list[str]:
    """Split on real line ends only. str.splitlines also splits on the characters above, which is how
    one value could turn into extra settings."""
    return re.split(r"\r\n|\n|\r", text)


def load_env(path: Path) -> None:
    """KEY=value lines from a .env file. Never overrides a variable that is already set; skips blank
    values and comments. A name that is not in ENV_NAMES, or whose companion setting is missing, is
    skipped and named in a message (the name only, never the value). A file it cannot read is skipped
    with a message: the program must still start."""
    if not path.is_file():
        return
    raw, text = path.read_bytes(), None
    for encoding in ("utf-8-sig", "utf-16"):  # Windows PowerShell's `>` writes UTF-16
        try:
            text = raw.decode(encoding)
            break
        except UnicodeError:
            continue
    if text is None:
        print(f"Ignoring {path.name}: it is not plain text. Save it as UTF-8.", flush=True)
        return
    found: dict[str, str] = {}
    unknown = 0
    for line in _lines(text):
        key, value = _pair(line) or ("", "")
        if not value:
            continue
        if key not in ENV_NAMES:
            # echoed only when it looks like a setting's name: a pasted key followed by "=" must not be printed
            if re.fullmatch(r"(CI|BRAVE|YOUTUBE|ANTHROPIC|APIFY)_[A-Z0-9_]{1,40}", key):
                print(f"Ignoring {key} in {path.name}: it is not a setting of this program.", flush=True)
            else:
                unknown += 1
            continue
        if UNSAFE.search(value):  # the system refuses such a value, and the program must still start
            print(f"Ignoring {key} in {path.name}: its value holds a control character.", flush=True)
            continue
        found.setdefault(key, value)
    if unknown:
        print(f"Ignoring {unknown} line(s) in {path.name} that do not name a setting of this program.", flush=True)
    # Drop what lacks its companion, then what depended on the dropped one, until nothing changes. The
    # companion must be in this same file: a file may not lean on a key or token it did not bring, or a
    # stray .env could run its own scraper on your Apify token or send your AI key to its own server.
    while True:
        lone = [key for key in found if ENV_NAMES[key] and ENV_NAMES[key] not in found]
        if not lone:
            break
        for key in lone:
            del found[key]
            print(f"Ignoring {key} in {path.name}: it only counts when {ENV_NAMES[key]} is set in this file too.",
                  flush=True)
    for key, value in found.items():
        os.environ.setdefault(key, value)


def _pair(line: str) -> tuple[str, str] | None:
    """(name, value) of one KEY=value line, or None for a blank line, a comment or anything else."""
    line = line.strip()
    if line.startswith("export "):
        line = line[len("export "):].lstrip()
    key, sep, value = line.partition("=")
    key, value = key.strip(), value.strip()
    if not sep or line.startswith("#"):
        return None
    closing = value.find(value[0], 1) if value[:1] in ("'", '"') else -1
    # a quoted value ends at its closing quote; otherwise a comment starts at whitespace + '#'
    return key, value[1:closing] if closing > 0 else re.split(r"\s+#", value, maxsplit=1)[0].strip()


def save_env(path: Path, changes: dict) -> list[str]:
    """Set or remove settings in the .env file (an empty value removes one) and in this running program,
    so a key typed into the tool works at once and is still there at the next start. Other lines of the
    file, comments included, are kept. The same rules as load_env apply, checked before anything is
    written: only names in ENV_NAMES, one-line values, and a setting only with the one it belongs to.
    Raises ValueError saying what is wrong; the file is then untouched. Returns the names it removed on its
    own account (the old AI server's key, when the address changed)."""
    clean = {key: (value or "").strip() for key, value in changes.items()}
    for key, value in clean.items():
        if key not in ENV_NAMES:
            raise ValueError(f"{key} is not a setting of this program")
        if UNSAFE.search(value):
            raise ValueError(f"{key}: a value is one line, with no control or line-break characters")
        if "'" in value and '"' in value:
            raise ValueError(f"{key}: a value cannot hold both kinds of quotation mark")
    text = ""
    if path.is_file():
        try:
            text = path.read_bytes().decode("utf-8-sig")
        except UnicodeError:
            raise ValueError(f"{path.name} is not saved as UTF-8 text; fix or remove it first") from None
    before = dict(pair for line in _lines(text) if (pair := _pair(line)) and pair[1] and not UNSAFE.search(pair[1]))
    dropped = []
    # A key belongs to the server it was typed for. When the address changes and no key is typed with it,
    # the old key goes: it must never be sent to the new address.
    if "CI_AI_URL" in clean and clean["CI_AI_URL"] != (before.get("CI_AI_URL") or os.environ.get("CI_AI_URL") or "") \
            and "CI_AI_KEY" not in clean and (before.get("CI_AI_KEY") or os.environ.get("CI_AI_KEY")):
        clean["CI_AI_KEY"] = ""
        dropped.append("CI_AI_KEY")
    kept = [line for line in _lines(text) if not ((pair := _pair(line)) and pair[0] in clean)]
    while kept and not kept[-1].strip():
        kept.pop()
    names = ({key for key in before if key not in clean} | {key for key, value in clean.items() if value})
    # only what this save itself sets or strands: a lone line someone wrote by hand is the file's business
    lone = sorted(key for key in names if ENV_NAMES.get(key) and ENV_NAMES[key] not in names
                  and (key in clean or ENV_NAMES[key] in clean))
    if lone:
        raise ValueError(f"{lone[0]} only works together with {ENV_NAMES[lone[0]]}: set both, or remove both "
                         f"(a setting that is not on this form is removed by editing {path.name})")

    def written(value: str) -> str:  # quoted when load_env would otherwise cut it at a '#' or read its first character as a quote
        if not (re.search(r"\s#", value) or value[0] in "'\""):
            return value
        mark = "'" if '"' in value else '"'
        return f"{mark}{value}{mark}"

    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")  # written whole, then swapped in: a crash cannot leave half a file
    try:
        temp.write_text("\n".join(kept + [f"{key}={written(value)}" for key, value in clean.items() if value]) + "\n",
                        encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)  # a failed save must not leave a second copy of the keys lying about
    for key, value in clean.items():
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)
    return dropped


# Wrong tries at the dashboard password, counted for the whole program and not per browser tab, so opening
# more tabs does not buy more guesses. ponytail: kept in memory, so restarting the program clears the wait;
# whoever can restart it can also read .env.
_tries = {"fails": 0, "until": 0.0}
_tries_lock = threading.Lock()  # each browser's try runs on its own thread


def unlock(typed: str, now: float | None = None) -> str | None:
    """None when `typed` is the dashboard password (CI_PASSWORD) or none is set; otherwise what to tell the
    person. Each wrong try doubles the wait before the next one is looked at, up to five minutes."""
    password = os.environ.get("CI_PASSWORD", "")
    if not password:
        return None
    with _tries_lock:
        now = time.monotonic() if now is None else now
        if now < _tries["until"]:
            return f"Too many wrong tries. Wait {math.ceil(_tries['until'] - now)} seconds, then type it again."
        if hmac.compare_digest(typed.strip().encode(), password.encode()):
            _tries["fails"] = 0
            return None
        _tries["fails"] += 1
        _tries["until"] = now + min(2 ** min(_tries["fails"], 9), 300)
        return "Wrong password."


def prepare() -> None:
    """Must run before creator_intel is imported: it reads some settings at import time."""
    load_env(home() / ".env")
    os.environ.setdefault("CI_DB", str(home() / "data" / "creator_intel.db"))


def _answering(port: int) -> bool:
    """Is something already listening there? Binding alone cannot tell on Windows, where
    127.0.0.1:port can be bound while another program holds the same port on all addresses."""
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def free_port(preferred: int = 8501) -> int:
    for port in (preferred, 0):  # 0 = let the system pick
        if port and _answering(port):
            continue
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise OSError("no free port on this computer")


# The look of the dashboard. The packaged program has no config file, so the theme travels as flags;
# keep it the same as .streamlit/config.toml, which is what `streamlit run app.py` uses.
LOOKS = {
    "light": {"primaryColor": "#0b6e63", "backgroundColor": "#ffffff", "secondaryBackgroundColor": "#eef4f2",
              "textColor": "#18262d"},
    "dark": {"primaryColor": "#3fb8a6", "backgroundColor": "#111a19", "secondaryBackgroundColor": "#1c2b29",
             "textColor": "#e6efed"},  # night mode
}
# No --theme.base: with a day and a night look both defined, the page menu lets the user choose.
THEME = [f"--theme.{mode}.{option}={value}" for mode, look in LOOKS.items() for option, value in look.items()]


def streamlit_args(app: Path, port: int, open_browser: bool) -> list[str]:
    return ["streamlit", "run", str(app),
            "--server.address=127.0.0.1",  # no login exists, so the dashboard is never offered to the network
            f"--server.port={port}",
            f"--server.headless={'false' if open_browser else 'true'}",
            "--browser.gatherUsageStats=false",
            "--client.toolbarMode=minimal",  # hides Streamlit's "Deploy" button, which means nothing here
            "--global.developmentMode=false",
            *THEME]


def run_dashboard() -> None:
    from streamlit.web import cli
    port = free_port()
    print(f"Creator research dashboard: http://127.0.0.1:{port}  (close this window to stop it)", flush=True)
    sys.argv = streamlit_args(bundled() / "app.py", port, open_browser=not os.environ.get("CI_NO_BROWSER"))
    cli.main()


def main(argv: list[str] | None = None) -> int:
    prepare()
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        from creator_intel.__main__ import main as command
        return command(argv)
    run_dashboard()
    return 0


if __name__ == "__main__":
    sys.exit(main())
