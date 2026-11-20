"""Pre-publish checks on your own draft: title length, thumbnail readability at small size, cover
aspect ratio, the screen areas the app covers, and loudness. Everything runs on this computer.

A check that had nothing to look at reports "not checked", never "ok".
The thresholds are starting points, not platform rules: tune them against your own posts.
"""
import re
import shutil
import subprocess
import warnings
from pathlib import Path

from PIL import Image, ImageDraw, ImageStat

# (hard limit, characters that reliably show without truncation). Platforms change these; re-check yearly.
TITLE_LIMITS = {"youtube": (100, 60), "tiktok": (2200, 100), "instagram": (2200, 125)}
VERTICAL = {"tiktok", "instagram"}  # covers are 9:16 there; pass vertical=True for YouTube Shorts
THUMB_WIDTH = 120  # roughly how wide a thumbnail is in a phone's suggested list
CONTRAST_FAIL, CONTRAST_WARN = 0.06, 0.12  # luminance spread (0-1) after shrinking; calibration knobs
TARGET_LUFS, LUFS_TOLERANCE = -14.0, 2.0
MAX_PIXELS = 40_000_000  # far above any thumbnail or cover frame
# Fractions of a 9:16 frame the app draws over: (left, top, right, bottom). Approximate.
COVERED = [(0.0, 0.0, 1.0, 0.10), (0.0, 0.80, 1.0, 1.0), (0.85, 0.45, 1.0, 0.80)]


def _finding(check: str, level: str, message: str) -> dict:
    return {"check": check, "level": level, "message": message}


def open_image(image) -> Image.Image:
    """A fully decoded picture from a path, an open file or a PIL image. Raises ValueError for anything
    unreadable or larger than MAX_PIXELS (a tiny file can claim a size that would exhaust memory)."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)  # we apply our own, lower limit
            picture = image if isinstance(image, Image.Image) else Image.open(image)
        if picture.width * picture.height > MAX_PIXELS:
            raise ValueError(f"Image is too large ({picture.width}x{picture.height}).")
        picture.load()
    except ValueError:
        raise
    except Exception:  # Pillow raises many unrelated error types on damaged files
        raise ValueError("Not a readable image.") from None
    return picture


def check_title(platform: str, title: str) -> list[dict]:
    if platform not in TITLE_LIMITS:
        raise ValueError(f"unknown platform: {platform}")
    hard, visible = TITLE_LIMITS[platform]
    n = len(title.strip())
    if n == 0:
        return [_finding("title length", "fail", "The title is empty.")]
    if n > hard:
        return [_finding("title length", "fail", f"{n} characters; the limit is {hard}.")]
    if n > visible:
        return [_finding("title length", "warn", f"{n} characters; only about the first {visible} are sure to show.")]
    return [_finding("title length", "ok", f"{n} characters.")]


def check_image(platform: str, image: Image.Image, vertical: bool | None = None) -> list[dict]:
    vertical = platform in VERTICAL if vertical is None else vertical
    width, height = image.size
    small = image.convert("L").resize((THUMB_WIDTH, max(1, round(THUMB_WIDTH * height / width))))
    spread = ImageStat.Stat(small).stddev[0] / 255
    level = "fail" if spread < CONTRAST_FAIL else "warn" if spread < CONTRAST_WARN else "ok"
    out = [_finding("contrast at 120px", level, f"Light-dark spread {spread:.2f} when shrunk to {THUMB_WIDTH}px wide"
                    + ("." if level == "ok" else "; it will look flat in a list of thumbnails."))]
    want, name = (9 / 16, "9:16") if vertical else (16 / 9, "16:9")
    off = abs(width / height - want) / want
    out.append(_finding("aspect ratio", "ok" if off <= 0.05 else "warn",
                        f"{width}x{height}" + ("." if off <= 0.05 else f"; {name} is expected here.")))
    return out


def safe_zone_overlay(image: Image.Image) -> Image.Image:
    """The cover with the areas a vertical feed draws over (top bar, caption, buttons) tinted red,
    so you can see whether a face or text sits underneath."""
    base = image.convert("RGBA")
    tint = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(tint)
    w, h = base.size
    for left, top, right, bottom in COVERED:
        draw.rectangle([round(left * w), round(top * h), round(right * w) - 1, round(bottom * h) - 1],
                       fill=(255, 0, 0, 110))
    return Image.alpha_composite(base, tint).convert("RGB")


def parse_lufs(ffmpeg_output: str) -> float | None:
    """Integrated loudness from ffmpeg's ebur128 report (the last 'I: ... LUFS' is the summary)."""
    found =re.findall(r"\bI:\s*(-?\d+(?:\.\d+)?) LUFS", ffmpeg_output)
    return float(found[-1]) if found else None


def find_ffmpeg() -> str | None:
    """ffmpeg from the system path. One sitting in the current folder is ignored: Windows looks there
    first, and the program may be started from a downloads folder that anything can write to."""
    found = shutil.which("ffmpeg")
    if found and Path(found).resolve().parent == Path.cwd().resolve():
        return None
    return found


def loudness(path) -> float | None:
    """Integrated loudness in LUFS, or None when ffmpeg is missing or the file has no readable audio."""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None
    # Argument list, no shell; the resolved path cannot be mistaken for an option. -protocol_whitelist file
    # stops a crafted media file from making ffmpeg fetch anything over the network; -nostdin keeps it
    # from reading the keyboard.
    try:
        done = subprocess.run([ffmpeg, "-hide_banner", "-nostats", "-nostdin", "-protocol_whitelist", "file",
                               "-i", str(Path(path).resolve()), "-vn", "-af", "ebur128", "-f", "null", "-"],
                              capture_output=True, text=True, errors="replace", timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_lufs(done.stderr)


def check_audio(path) -> list[dict]:
    lufs = loudness(path)
    if lufs is None:
        return [_finding("loudness", "not checked", "ffmpeg is not installed, or the file has no readable audio.")]
    ok = abs(lufs - TARGET_LUFS) <= LUFS_TOLERANCE
    return [_finding("loudness", "ok" if ok else "warn",
                     f"{lufs:.1f} LUFS" + ("." if ok else f"; aim for about {TARGET_LUFS:.0f}."))]


def check(platform: str, title: str, image=None, audio=None, vertical: bool | None = None) -> list[dict]:
    """image: a path, an open file or a PIL image. audio: a path to an audio or video file."""
    out = check_title(platform, title)
    if image is None:
        out.append(_finding("thumbnail", "not checked", "No image given."))
    else:
        try:
            picture = open_image(image)
            out += check_image(platform, picture, vertical)
        except ValueError as exc:
            out.append(_finding("thumbnail", "fail", str(exc)))
    out += check_audio(audio) if audio is not None else [_finding("loudness", "not checked", "No audio given.")]
    return out
