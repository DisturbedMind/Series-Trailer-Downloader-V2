#!/usr/bin/env python3
"""
Download TV series trailers into media-server-compatible folders named like:

    C:\\TV Shows\\Series (2026)\\Trailers\\trailer.ext

Existing trailer files are renamed before new downloads, for example:

    Trailers\\trailer.mp4 -> Trailers\\trailer.mp4.old

On a successful new download, matching .old trailer backups are deleted.

Requires:
    python -m pip install -U "yt-dlp[default,curl-cffi]"

FFmpeg is strongly recommended for best-quality video+audio merges:
    winget install Gyan.FFmpeg
"""

from __future__ import annotations

import argparse
import http.client
import importlib.util
import json
import queue
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

yt_dlp = None


def command_version_at_least(executable: str, args: list[str], minimum: tuple[int, int]) -> bool:
    try:
        result = subprocess.run(
            [executable, *args],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        return False
    if result.returncode != 0:
        return False
    match = re.search(r"(\d+)\.(\d+)", f"{result.stdout} {result.stderr}")
    if not match:
        return False
    version = (int(match.group(1)), int(match.group(2)))
    return version >= minimum


def default_js_runtime_setting() -> str:
    runtimes = []
    deno = shutil.which("deno")
    node = shutil.which("node")
    if deno and command_version_at_least(deno, ["--version"], (2, 3)):
        runtimes.append("deno")
    if node and command_version_at_least(node, ["--version"], (22, 0)):
        runtimes.append("node")
    if shutil.which("qjs"):
        runtimes.append("quickjs")
    elif shutil.which("quickjs"):
        runtimes.append(f"quickjs:{shutil.which('quickjs')}")
    return ",".join(runtimes)


SETTINGS_PATH = Path(__file__).with_suffix(".settings.json")
INSTALLER_PATH = Path(__file__).with_name("install.ps1")
DEFAULT_COOKIES_PATH = Path(__file__).with_name("youtube-cookies.txt")
DEFAULT_RESULTS_PATH = Path(__file__).with_name("trailer-results.json")
DEFAULT_LOG_PATH = Path(__file__).with_name("series_trailer_downloader.log")
DEFAULT_SEARCH_DELAY = 2.0
DEFAULT_SERIES_DELAY = 5.0
DEFAULT_DOWNLOAD_SLEEP_MIN = 3.0
DEFAULT_DOWNLOAD_SLEEP_MAX = 8.0
DEFAULT_CANDIDATE_ATTEMPTS = 5
DEFAULT_QUALITY_PROBE_LIMIT = 10
DEFAULT_MAX_TRAILER_HEIGHT = 0
MIN_PYTHON_VERSION = (3, 14)
DEFAULT_FFMPEG_THREADS = 2
DEFAULT_FFMPEG_PRESET = "veryfast"
DEFAULT_FFMPEG_CRF = 22
DEFAULT_FFMPEG_ENCODER = "cpu"
DEFAULT_JS_RUNTIME = default_js_runtime_setting()
DEFAULT_REMOTE_COMPONENTS = "ejs:github"
FFMPEG_PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium")
FFMPEG_ENCODERS = ("cpu", "auto", "nvidia", "intel", "amd")
DEFAULT_SOURCE_ORDER = "tmdb,kinocheck,youtube-api,internet-archive,youtube"
DEFAULT_TMDB_MIN_SIZE = 720
DEFAULT_DAILYMOTION_MIN_HEIGHT = 720
DIRECT_MEDIA_EXTENSIONS = (
    ".mp4",
    ".mov",
    ".m4v",
    ".mkv",
    ".webm",
    ".m2ts",
    ".mts",
    ".ts",
    ".avi",
    ".wmv",
    ".mpg",
    ".mpeg",
)
HTTP_USER_AGENT = "Mozilla/5.0 (compatible; SeriesTrailerDownloader/2.0; +https://local)"
SOURCE_SCORE_BONUSES = {
    "tmdb-vimeo": 2600,
    "tmdb-youtube": 1400,
    "kinocheck": 5600,
    "youtube-api": 3200,
    "internet-archive": 2200,
    "dailymotion": 1600,
}
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
PREFERRED_FORMAT_RECOVERY_LABEL: str | None = None


def strip_ansi(text: object) -> str:
    return ANSI_ESCAPE_RE.sub("", str(text))


TRAILER_WORDS = {"trailer", "teaser", "preview", "promo"}
BAD_WORDS = {
    "reaction",
    "review",
    "explained",
    "breakdown",
    "fanmade",
    "fan-made",
    "concept",
    "parody",
    "recap",
    "ending",
    "clip",
    "interview",
    "behind",
    "bts",
    "soundtrack",
    "lyrics",
    "song",
    "full episode",
    "full season",
    "webrip",
    "hdrip",
    "dvdrip",
    "bluray",
    "brrip",
    "camrip",
    "hdcam",
    "hdts",
    "torrent",
    "xclu",
}
BAD_TMDB_VIDEO_WORDS = {
    "review",
    "promo",
    "spot",
    "tickets",
    "featurette",
    "clip",
    "behind",
    "bts",
    "yours to own",
    "extended preview",
}
BAD_TRAILER_CONTENT_WORDS = {
    "featurette",
    "feature-",
    "feature:",
    "tv spot",
    "spot",
    "clip",
    "behind",
    "bts",
    "interview",
    "promo",
    "sneak peek",
}
FOREIGN_LANGUAGE_WORDS = {
    "aleman",
    "arabic",
    "castellano",
    "deutsch",
    "deutsche",
    "dubbed",
    "espanol",
    "filipino",
    "french",
    "german",
    "hindi",
    "indonesian",
    "italian",
    "japanese",
    "korean",
    "latino",
    "malay",
    "polish",
    "portuguese",
    "rus",
    "russian",
    "spanish",
    "tagalog",
    "tamil",
    "telugu",
    "thai",
    "turkish",
    "urdu",
}
FOREIGN_LANGUAGE_PHRASES = (
    "dual audio",
    "multi audio",
    "multi language",
    "no english",
    "sub espanol",
    "subtitulado",
    "subtitulos",
    "version espanol",
)
THREE_D_MARKER_RE = re.compile(r"\b(?:3d|3-d|stereoscopic)\b", re.I)
EPISODE_FILE_RE = re.compile(r"\bs\d{1,2}e\d{1,3}\b", re.I)
IGNORED_SERIES_DIR_RE = re.compile(
    r"^(?:season\s*\d+|s\d+|specials?|trailers?|extras?|featurettes?|interviews?|"
    r"behind[ ._-]*the[ ._-]*scenes|deleted[ ._-]*scenes|samples?|shorts?|clips?|scenes?|other)$",
    re.I,
)


@dataclass(frozen=True)
class SeriesFolder:
    folder: Path
    display_name: str
    title: str
    year: str | None


@dataclass(frozen=True)
class Candidate:
    url: str
    title: str
    channel: str
    duration: int | None
    score: int


class LockedFileSkipped(Exception):
    def __init__(self, path: Path, action: str):
        self.path = path
        self.action = action
        super().__init__(f"{path.name} is open or locked while trying to {action}")


def normalise_words(text: str) -> set[str]:
    return set(title_match_tokens(text))


def fold_text_ascii(text: str) -> str:
    folded = unicodedata.normalize("NFKD", str(text or ""))
    return folded.encode("ascii", "ignore").decode("ascii").lower()


def title_match_tokens(text: str) -> list[str]:
    numeric_aliases = {
        "zero": "0",
        "one": "1",
        "i": "1",
        "two": "2",
        "ii": "2",
        "three": "3",
        "iii": "3",
        "four": "4",
        "iv": "4",
        "five": "5",
        "v": "5",
        "six": "6",
        "vi": "6",
        "seven": "7",
        "vii": "7",
        "eight": "8",
        "viii": "8",
        "nine": "9",
        "ix": "9",
        "ten": "10",
        "x": "10",
    }
    comparable = text.lower().replace("&", " and ").replace("+", " and ")
    tokens = re.findall(r"[a-z0-9]+", comparable)
    normalized = []
    for token in tokens:
        token = numeric_aliases.get(token, token)
        if token.isdigit() or len(token) > 2:
            normalized.append(token)
    return normalized


def has_foreign_language_marker(series: SeriesFolder, text: str) -> bool:
    folded = fold_text_ascii(text)
    series_words = normalise_words(series.title)
    words = set(re.findall(r"[a-z0-9]+", folded))

    for phrase in FOREIGN_LANGUAGE_PHRASES:
        if phrase in folded and not (normalise_words(phrase) & series_words):
            return True

    for word in FOREIGN_LANGUAGE_WORDS:
        if word in words and word not in series_words:
            return True

    return False


def has_3d_trailer_marker(series: SeriesFolder, text: str) -> bool:
    series_title = fold_text_ascii(series.title)
    if THREE_D_MARKER_RE.search(series_title):
        return False
    return bool(THREE_D_MARKER_RE.search(fold_text_ascii(text)))


def strip_library_title_prefix(title: str) -> str:
    cleaned = title.strip()
    prefix_patterns = (
        r"^\s*\[\s*\d{1,4}\s*\]\s+",
        r"^\s*\(\s*\d{1,4}\s*\)\s+",
        r"^\s*\d{1,4}\s*[-_.]\s+",
        r"^\s*\d{1,4}\s+[-_.]\s+",
    )
    changed = True
    while changed:
        changed = False
        for pattern in prefix_patterns:
            updated = re.sub(pattern, "", cleaned)
            if updated != cleaned:
                cleaned = updated.strip()
                changed = True
    return cleaned


def clean_series_title(title: str) -> str:
    cleaned = strip_library_title_prefix(title)
    cleaned = re.sub(r"[\[{](?:tmdbid|tvdbid|imdbid|tmdb|tvdb|imdb)-[^\]}]+[\]}]", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\[(?:\d{3,4}p|2160p|1080p|720p|480p|4k|uhd|hdr|x264|x265|hevc|bluray|web[- ]?dl|webrip)\]", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s*&\s*", " & ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -_.")
    return cleaned or title.strip()


def title_query_variants(title: str) -> list[str]:
    variants = [title.strip()]
    if "&" in title:
        variants.append(re.sub(r"\s*&\s*", " and ", title).strip())
    if re.search(r"\band\b", title, flags=re.I):
        variants.append(re.sub(r"\band\b", "&", title, flags=re.I).strip())
    return list(dict.fromkeys(variant for variant in variants if variant))


def years_in_text(text: str) -> set[str]:
    return set(re.findall(r"\b(?:19|20)\d{2}\b", text))


def require_yt_dlp() -> None:
    global yt_dlp
    if yt_dlp is not None:
        return
    try:
        import yt_dlp as yt_dlp_module
    except ImportError:
        print("Missing dependency: yt-dlp", file=sys.stderr)
        print("Install it with: python -m pip install -U yt-dlp", file=sys.stderr)
        raise SystemExit(2)
    yt_dlp = yt_dlp_module


def dependency_status() -> list[tuple[str, bool, str]]:
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    python_ok = sys.version_info >= MIN_PYTHON_VERSION
    status = [("Python 3.14+", python_ok, python_version)]
    status.append(("yt-dlp", importlib.util.find_spec("yt_dlp") is not None, "python package yt-dlp[default,curl-cffi]"))
    status.append(
        (
            "yt-dlp impersonation",
            importlib.util.find_spec("curl_cffi") is not None,
            "python package curl_cffi; required by Dailymotion and other TLS-fingerprint protected sources",
        )
    )

    ffmpeg = shutil.which("ffmpeg")
    status.append(("FFmpeg", bool(ffmpeg), ffmpeg or "required for MP4 conversion and audio normalization"))

    deno = shutil.which("deno")
    node = shutil.which("node")
    deno_ok = bool(deno and command_version_at_least(deno, ["--version"], (2, 3)))
    node_ok = bool(node and command_version_at_least(node, ["--version"], (22, 0)))
    js_detail = []
    if deno:
        js_detail.append(f"Deno {'OK' if deno_ok else 'too old'}")
    if node:
        js_detail.append(f"Node {'OK' if node_ok else 'too old'}")
    status.append(("YouTube EJS runtime", deno_ok or node_ok, ", ".join(js_detail) or "install Deno 2.3+ or Node.js 22+"))
    status.append(("Installer", INSTALLER_PATH.exists(), str(INSTALLER_PATH)))
    return status


def missing_dependency_names() -> list[str]:
    return [name for name, ok, _detail in dependency_status() if not ok and name != "Installer"]


def print_dependency_status() -> None:
    print("Dependency check:")
    for name, ok, detail in dependency_status():
        marker = "OK" if ok else "MISSING"
        print(f"  {marker:7} {name}: {detail}")


def is_cookie_decrypt_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "dpapi" in message or "failed to decrypt" in message


def is_forbidden_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "403" in message or "forbidden" in message


def is_format_unavailable_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "requested format is not available" in message or "only images are available" in message


def is_ejs_challenge_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "n challenge" in message or "challenge solver" in message or "javascript runtime" in message


def is_impersonation_dependency_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "attempting impersonation" in message and "none of these impersonate targets are available" in message


def print_impersonation_help() -> None:
    print("    this source needs yt-dlp browser impersonation support")
    print('    fix: python -m pip install -U "yt-dlp[default,curl-cffi]"')
    print("    or run install.ps1 / Install-Repair Dependencies from the GUI")


class QuietDownloadLogger:
    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        pass

    def error(self, msg: str) -> None:
        pass


def without_browser_cookies(opts: dict) -> dict:
    clean_opts = dict(opts)
    clean_opts.pop("cookiesfrombrowser", None)
    return clean_opts


def without_any_cookies(opts: dict) -> dict:
    clean_opts = without_browser_cookies(opts)
    clean_opts.pop("cookiefile", None)
    return clean_opts


def parse_js_runtimes(value: str) -> dict:
    runtimes = {}
    for part in str(value or "").split(","):
        part = part.strip()
        if not part:
            continue
        runtime, _, runtime_path = part.partition(":")
        runtime = runtime.strip().lower()
        runtime_path = runtime_path.strip()
        if not runtime:
            continue
        runtimes[runtime] = {"path": runtime_path} if runtime_path else {}
    return runtimes


def merge_js_runtimes(primary: dict | None, fallback: dict | None = None) -> dict:
    merged = dict(fallback or {})
    for runtime, config in (primary or {}).items():
        merged[runtime] = config
    return merged


def parse_remote_components(value: str) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def with_ejs_solver_options(opts: dict) -> dict:
    tuned_opts = dict(opts)
    detected_runtimes = parse_js_runtimes(DEFAULT_JS_RUNTIME)
    tuned_opts["js_runtimes"] = merge_js_runtimes(tuned_opts.get("js_runtimes"), detected_runtimes)

    remote_components = list(tuned_opts.get("remote_components") or [])
    if "ejs:github" not in remote_components:
        remote_components.append("ejs:github")
    tuned_opts["remote_components"] = remote_components
    return tuned_opts


def ejs_retry_option_sets(opts: dict) -> list[tuple[str, dict]]:
    ejs_opts = with_ejs_solver_options(opts)
    if ejs_opts == opts:
        return []
    return [
        ("detected EJS runtime and GitHub solver components", ejs_opts),
        (
            "detected EJS runtime, GitHub solver components, and alternate YouTube client profile",
            with_youtube_player_clients(ejs_opts, ["default", "web", "web_embedded", "mweb"]),
        ),
    ]


def with_youtube_player_clients(opts: dict, clients: list[str]) -> dict:
    tuned_opts = dict(opts)
    extractor_args = dict(tuned_opts.get("extractor_args") or {})
    youtube_args = dict(extractor_args.get("youtube") or {})
    youtube_args["player_client"] = clients
    extractor_args["youtube"] = youtube_args
    tuned_opts["extractor_args"] = extractor_args
    return tuned_opts


def youtube_retry_option_sets(opts: dict) -> list[tuple[str, dict]]:
    profiles = [
        ("alternate YouTube client profile", with_youtube_player_clients(opts, ["default", "web", "web_embedded", "mweb"])),
        (
            "web-only YouTube client profile",
            with_youtube_player_clients(opts, ["web", "web_embedded"]),
        ),
    ]
    if "cookiefile" in opts or "cookiesfrombrowser" in opts:
        no_cookie_opts = without_any_cookies(opts)
        profiles.extend(
            [
                ("without cookies", no_cookie_opts),
                (
                    "without cookies and alternate YouTube client profile",
                    with_youtube_player_clients(no_cookie_opts, ["default", "web", "web_embedded", "mweb"]),
                ),
            ]
        )
    return profiles

def format_unavailable_retry_option_sets(opts: dict) -> list[tuple[str, dict]]:
    profiles: list[tuple[str, dict]] = []

    def add(label: str, retry_opts: dict) -> None:
        if retry_opts == opts:
            return
        if any(existing_opts == retry_opts for _existing_label, existing_opts in profiles):
            return
        profiles.append((label, retry_opts))

    ejs_opts = with_ejs_solver_options(opts)

    if "cookiefile" in opts or "cookiesfrombrowser" in opts:
        no_cookie_opts = without_any_cookies(ejs_opts)
        add("without cookies", no_cookie_opts)
        add(
            "without cookies and alternate YouTube client profile",
            with_youtube_player_clients(no_cookie_opts, ["default", "web", "web_embedded", "mweb"]),
        )

    add("detected EJS runtime and GitHub solver components", ejs_opts)
    add(
        "alternate YouTube client profile",
        with_youtube_player_clients(ejs_opts, ["default", "web", "web_embedded", "mweb"]),
    )
    add("web-only YouTube client profile", with_youtube_player_clients(ejs_opts, ["web", "web_embedded"]))

    if PREFERRED_FORMAT_RECOVERY_LABEL:
        profiles.sort(key=lambda item: item[0] != PREFERRED_FORMAT_RECOVERY_LABEL)

    return profiles

def extract_cookies_file(browser: str, cookies_file: Path) -> Path:
    require_yt_dlp()
    cookies_file = cookies_file.expanduser()
    cookies_file.parent.mkdir(parents=True, exist_ok=True)
    opts = {
        "cookiesfrombrowser": (browser,),
        "quiet": False,
        "no_warnings": False,
    }
    print(f"Extracting cookies from {browser}...")
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.cookiejar.save(str(cookies_file), ignore_discard=True, ignore_expires=True)
    print(f"Saved cookies file: {cookies_file}")
    return cookies_file


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def polite_sleep(seconds: float) -> None:
    if seconds <= 0:
        return
    jitter = min(seconds * 0.25, 2.0)
    time.sleep(max(0.0, seconds + random.uniform(-jitter, jitter)))


def series_key(series: SeriesFolder) -> str:
    try:
        return str(series.folder.resolve()).lower()
    except OSError:
        return str(series.folder.absolute()).lower()


def load_results(results_file: Path) -> dict:
    if not results_file.exists():
        return {"version": 1, "series": {}}
    try:
        data = json.loads(results_file.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "series": {}}
    if not isinstance(data, dict):
        return {"version": 1, "series": {}}
    data.setdefault("version", 1)
    if not isinstance(data.get("series"), dict):
        data["series"] = {}
    return data


def save_results(results_file: Path, results: dict) -> None:
    results_file.parent.mkdir(parents=True, exist_ok=True)
    results_file.write_text(json.dumps(results, indent=2, sort_keys=True), encoding="utf-8")


def result_trailer_path(record: dict) -> Path | None:
    trailer_path = record.get("trailer_path")
    if not trailer_path:
        return None
    return Path(str(trailer_path))


def successful_result_for_series(series: SeriesFolder, results: dict) -> dict | None:
    record = results.get("series", {}).get(series_key(series))
    if not isinstance(record, dict) or record.get("status") != "success":
        return None
    trailer_path = result_trailer_path(record)
    if trailer_path and trailer_path.exists():
        return record
    return None


def record_success(series: SeriesFolder, target: Path, candidate: Candidate, results: dict) -> None:
    results.setdefault("series", {})[series_key(series)] = {
        "status": "success",
        "series_folder": str(series.folder),
        "series_name": series.display_name,
        "title": series.title,
        "year": series.year,
        "trailer_path": str(target),
        "trailer_name": target.name,
        "trailer_size_bytes": target.stat().st_size if target.exists() else None,
        "source_url": candidate.url,
        "source_title": candidate.title,
        "source_channel": candidate.channel,
        "source_duration": candidate.duration,
        "source_score": candidate.score,
        "last_success_utc": now_utc_iso(),
    }


def record_existing_success(series: SeriesFolder, target: Path, results: dict) -> None:
    results.setdefault("series", {})[series_key(series)] = {
        "status": "success",
        "series_folder": str(series.folder),
        "series_name": series.display_name,
        "title": series.title,
        "year": series.year,
        "trailer_path": str(target),
        "trailer_name": target.name,
        "trailer_size_bytes": target.stat().st_size if target.exists() else None,
        "source_url": None,
        "source_title": "Existing local trailer",
        "source_channel": None,
        "source_duration": None,
        "source_score": None,
        "last_success_utc": now_utc_iso(),
    }


def parse_series_folder(folder: Path) -> SeriesFolder:
    display_name = folder.name.strip()
    match = re.match(r"^(?P<title>.+?)\s*\((?P<year>\d{4})\)(?:\s*[\[{][^\]}]+[\]}])*$", display_name)
    if not match:
        return SeriesFolder(folder, display_name, clean_series_title(display_name), None)
    return SeriesFolder(folder, display_name, clean_series_title(match.group("title")), match.group("year"))


def is_ignored_series_directory(path: Path) -> bool:
    return path.name.startswith((".", "$", "@")) or bool(IGNORED_SERIES_DIR_RE.fullmatch(path.name.strip()))


def looks_like_series_directory(path: Path) -> bool:
    try:
        children = list(path.iterdir())
    except OSError:
        return False
    if any(child.is_dir() and re.fullmatch(r"(?:season\s*\d+|s\d+|specials?)", child.name, re.I) for child in children):
        return True
    return any(child.is_file() and EPISODE_FILE_RE.search(child.stem) for child in children)


def iter_series_folders(root: Path, recursive: bool = False) -> Iterable[SeriesFolder]:
    if not recursive:
        candidates = root.iterdir()
    else:
        candidates = (path for path in root.rglob("*") if path.is_dir() and looks_like_series_directory(path))
    for child in sorted(candidates, key=lambda p: str(p).lower()):
        if child.is_dir() and not is_ignored_series_directory(child):
            yield parse_series_folder(child)


def trailer_directory(series: SeriesFolder) -> Path:
    """Return an existing case-insensitive trailers folder or the portable default."""
    try:
        existing = next(
            (child for child in series.folder.iterdir() if child.is_dir() and child.name.lower() == "trailers"),
            None,
        )
    except OSError:
        existing = None
    return existing or (series.folder / "Trailers")


def trailer_search_directories(series: SeriesFolder) -> list[Path]:
    return [trailer_directory(series), series.folder]


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    counter = 1
    while True:
        candidate = path.with_name(f"{path.name}.{counter}")
        if not candidate.exists():
            return candidate
        counter += 1


def is_file_access_error(exc: OSError) -> bool:
    return isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {5, 32, 33}


def is_matching_trailer_backup(series: SeriesFolder, item: Path) -> bool:
    lower_name = item.name.lower()
    if item.parent == series.folder and not lower_name.startswith(f"{series.display_name}-trailer".lower()):
        return False
    original_name = re.sub(r"\.old(?:\.\d+)?$", "", lower_name)
    return item.is_file() and original_name.endswith(DIRECT_MEDIA_EXTENSIONS) and original_name != lower_name


def delete_old_trailer_backups(series: SeriesFolder, dry_run: bool) -> list[Path]:
    deleted: list[Path] = []
    for directory in trailer_search_directories(series):
        if not directory.exists():
            continue
        for item in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
            if not is_matching_trailer_backup(series, item):
                continue
            if dry_run:
                deleted.append(item)
                continue
            try:
                item.unlink()
            except OSError as exc:
                if is_file_access_error(exc):
                    print(f"  skipped locked backup: {item.name} ({exc})")
                    continue
                raise
            deleted.append(item)
    return deleted


def find_current_trailers(series: SeriesFolder) -> list[Path]:
    prefix = f"{series.display_name}-trailer".lower()
    current: list[Path] = []
    for directory in trailer_search_directories(series):
        if not directory.exists():
            continue
        for item in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
            if not item.is_file():
                continue
            lower_name = item.name.lower()
            if directory == series.folder and not lower_name.startswith(prefix):
                continue
            if not lower_name.endswith(DIRECT_MEDIA_EXTENSIONS):
                continue
            if lower_name.endswith(".old") or ".old." in lower_name:
                continue
            if lower_name.endswith((".part", ".ytdl", ".tmp")):
                continue
            current.append(item)
    return current


def restore_renamed_trailers(renamed: list[tuple[Path, Path]]) -> list[tuple[Path, Path]]:
    restored: list[tuple[Path, Path]] = []
    for original, backup in renamed:
        if backup.exists() and not original.exists():
            try:
                backup.rename(original)
            except OSError as exc:
                if is_file_access_error(exc):
                    print(f"    could not restore locked backup: {backup.name} ({exc})")
                    continue
                raise
            else:
                restored.append((backup, original))
    return restored


def rename_existing_trailers(series: SeriesFolder, dry_run: bool) -> list[tuple[Path, Path]]:
    prefix = f"{series.display_name}-trailer".lower()
    renamed: list[tuple[Path, Path]] = []

    for directory in trailer_search_directories(series):
        if not directory.exists():
            continue
        for item in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
            if not item.is_file():
                continue
            lower_name = item.name.lower()
            if directory == series.folder and not lower_name.startswith(prefix):
                continue
            if not lower_name.endswith(DIRECT_MEDIA_EXTENSIONS):
                continue
            if lower_name.endswith(".old") or ".old." in lower_name:
                continue

            target = unique_path(item.with_name(f"{item.name}.old"))
            if dry_run:
                renamed.append((item, target))
                continue
            try:
                item.rename(target)
            except OSError as exc:
                if is_file_access_error(exc):
                    restore_renamed_trailers(renamed)
                    raise LockedFileSkipped(item, "rename it for re-download") from exc
                raise
            renamed.append((item, target))

    return renamed


def slugify_title(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return re.sub(r"-+", "-", slug)


def source_order_from_value(value: str | None) -> list[str]:
    raw = value or DEFAULT_SOURCE_ORDER
    order = [part.strip().lower() for part in raw.split(",") if part.strip()]
    allowed = {"tmdb", "kinocheck", "youtube-api", "internet-archive", "dailymotion", "youtube"}
    return [source for source in order if source in allowed]


def http_get_text(url: str, timeout: int = 15) -> str | None:
    request = urllib.request.Request(url, headers={"User-Agent": HTTP_USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            try:
                body = response.read()
            except http.client.IncompleteRead as exc:
                body = exc.partial
            return body.decode("utf-8", errors="replace")
    except Exception:
        return None


def http_get_json(url: str, headers: dict[str, str] | None = None, timeout: int = 15) -> dict | None:
    request = urllib.request.Request(url, headers={"User-Agent": HTTP_USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            try:
                body = response.read()
            except http.client.IncompleteRead as exc:
                body = exc.partial
            return json.loads(body.decode("utf-8", errors="replace"))
    except Exception:
        return None


def candidate_from_metadata(
    series: SeriesFolder,
    url: str,
    title: str,
    channel: str,
    duration: int | None,
    max_duration: int,
    score_bonus: int,
    max_height: int = DEFAULT_MAX_TRAILER_HEIGHT,
) -> Candidate | None:
    if exceeds_max_height_by_label(max_height, title, url):
        return None
    entry = {"webpage_url": url, "title": title, "channel": channel, "duration": duration}
    score = score_candidate(series, entry, max_duration)
    if score is None:
        return None
    return Candidate(
        url=url,
        title=title,
        channel=channel,
        duration=duration,
        score=score + score_bonus + quality_score(title, url),
    )


def quality_score(*parts: object) -> int:
    text = " ".join(str(part or "") for part in parts).lower()
    score = 0
    if any(token in text for token in ("lossless", "prores", "pro-res", "dts-hd", "truehd")):
        score += 6000
    if any(token in text for token in ("4320", "8k")):
        score += 5000
    if any(token in text for token in ("2160", "4k", "uhd")):
        score += 4200
    elif any(token in text for token in ("1440", "qhd", "2k")):
        score += 2600
    elif "1080" in text:
        score += 1800
    elif "720" in text:
        score += 900
    elif "480" in text:
        score += 250
    if any(token in text for token in ("hdr", "10bit", "10-bit", "x265", "hevc")):
        score += 700
    if any(token in text for token in ("5.1", "7.1", "atmos")):
        score += 300
    return score


def quality_height_from_text(*parts: object) -> int | None:
    text = " ".join(str(part or "") for part in parts).lower()
    if any(token in text for token in ("4320", "8k")):
        return 4320
    if any(token in text for token in ("2160", "4k", "uhd")):
        return 2160
    if any(token in text for token in ("1440", "qhd", "2k")):
        return 1440
    if "1080" in text:
        return 1080
    if "720" in text:
        return 720
    if "480" in text:
        return 480
    return None


def exceeds_max_height_by_label(max_height: int, *parts: object) -> bool:
    max_height = normalise_max_height(max_height)
    labeled_height = quality_height_from_text(*parts)
    return bool(max_height > 0 and labeled_height and labeled_height > max_height)


def is_direct_media_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    return parsed.scheme in {"http", "https"} and parsed.path.lower().endswith(DIRECT_MEDIA_EXTENSIONS)


def direct_media_url_looks_downloadable(url: str, timeout: int = 10) -> tuple[bool, str]:
    user_agent = "QuickTime/7.6.2" if "movietrailers.apple.com" in urllib.parse.urlparse(url).netloc.lower() else HTTP_USER_AGENT
    headers = {"User-Agent": user_agent, "Range": "bytes=0-0"}
    for method in ("HEAD", "GET"):
        request = urllib.request.Request(url, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                content_type = str(response.headers.get("Content-Type") or "").lower()
                if any(kind in content_type for kind in ("text/html", "text/plain", "application/json", "xml")):
                    return False, f"non-media content type: {content_type}"
                return True, content_type or "unknown content type"
        except urllib.error.HTTPError as exc:
            if method == "HEAD" and exc.code in {403, 405, 501}:
                continue
            return False, f"HTTP {exc.code}"
        except Exception as exc:
            if method == "HEAD":
                continue
            return False, str(exc)
    return False, "preflight failed"


def tmdb_headers_and_key(token: str | None) -> tuple[dict[str, str], str | None]:
    value = (token or "").strip() or os_environ_first("TMDB_BEARER_TOKEN", "TMDB_ACCESS_TOKEN", "TMDB_API_KEY")
    if not value:
        return {}, None
    if len(value) > 60 or value.count(".") >= 2:
        return {"Authorization": f"Bearer {value}"}, None
    return {}, value


def os_environ_first(*names: str) -> str:
    import os

    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return ""


def api_key_headers(token: str | None, env_names: tuple[str, ...]) -> dict[str, str]:
    value = (token or "").strip() or os_environ_first(*env_names)
    if not value:
        return {}
    return {"Authorization": f"Bearer {value}", "x-api-key": value}


def tmdb_series_details(series: SeriesFolder, token: str | None) -> dict | None:
    headers, api_key = tmdb_headers_and_key(token)
    if not headers and not api_key:
        return None
    query = urllib.parse.urlencode(
        {
            "query": series.title,
            "include_adult": "false",
            "language": "en-US",
            **({"first_air_date_year": series.year} if series.year else {}),
            **({"api_key": api_key} if api_key else {}),
        }
    )
    search_url = f"https://api.themoviedb.org/3/search/tv?{query}"
    search = http_get_json(search_url, headers=headers)
    results = (search or {}).get("results") or []
    if not results:
        return None
    title_words = normalise_words(series.title)
    def match_score(item: dict) -> int:
        item_title = str(item.get("name") or item.get("original_name") or "")
        release_date = str(item.get("first_air_date") or "")
        return len(title_words & normalise_words(item_title)) + (
            20 if series.year and release_date.startswith(series.year) else 0
        )

    best = max(results, key=match_score)
    required_words = max(1, min(3, len(title_words)))
    if len(title_words & normalise_words(str(best.get("name") or best.get("original_name") or ""))) < required_words:
        return None
    if series.year and not str(best.get("first_air_date") or "").startswith(series.year):
        return None
    return best if best.get("id") else None


def collect_tmdb_candidates(
    series: SeriesFolder,
    max_duration: int,
    token: str | None,
    max_height: int = 0,
) -> tuple[list[Candidate], int | None]:
    details = tmdb_series_details(series, token)
    if not details:
        return [], None
    headers, api_key = tmdb_headers_and_key(token)
    query = urllib.parse.urlencode({"language": "en-US", **({"api_key": api_key} if api_key else {})})
    videos_url = f"https://api.themoviedb.org/3/tv/{details['id']}/videos?{query}"
    videos = http_get_json(videos_url, headers=headers)
    candidates: list[Candidate] = []
    for item in (videos or {}).get("results") or []:
        site = str(item.get("site") or "").lower()
        key = str(item.get("key") or "").strip()
        video_type = str(item.get("type") or "").lower()
        if not key or video_type not in {"trailer", "teaser"}:
            continue
        name = str(item.get("name") or f"{series.display_name} trailer")
        name_lower = name.lower()
        if any(word in name_lower for word in BAD_TMDB_VIDEO_WORDS):
            continue
        if site == "vimeo":
            url = f"https://vimeo.com/{key}"
            bonus = SOURCE_SCORE_BONUSES["tmdb-vimeo"]
        elif site == "youtube":
            url = f"https://www.youtube.com/watch?v={key}"
            bonus = SOURCE_SCORE_BONUSES["tmdb-youtube"]
        else:
            continue
        try:
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if size and size < DEFAULT_TMDB_MIN_SIZE:
            continue
        if max_height and size and size > max_height:
            continue
        if item.get("official"):
            bonus += 600
        if video_type == "trailer":
            bonus += 500
        else:
            bonus -= 300
        if "official" in name_lower:
            bonus += 250
        if "trailer" in name_lower:
            bonus += 150
        candidate_title = f"{name} [{size}p]" if size else name
        base_score = score_candidate(
            series,
            {
                "title": f"{series.title} {series.year or ''} official trailer",
                "channel": f"TMDb {item.get('site')}",
                "duration": None,
            },
            max_duration,
        )
        if base_score is None:
            continue
        score = (
            bonus
            + quality_score(candidate_title, url, f"{size}p" if size else "")
            + base_score
        )
        candidates.append(
            Candidate(
                url=url,
                title=candidate_title,
                channel=f"TMDb {item.get('site')}",
                duration=None,
                score=score,
            )
        )
    return candidates, int(details["id"])


def collect_kinocheck_candidates(
    series: SeriesFolder,
    max_duration: int,
    tmdb_id: int | None = None,
    token: str | None = None,
) -> list[Candidate]:
    if not tmdb_id:
        return []
    url = f"https://api.kinocheck.com/shows?tmdb_id={tmdb_id}&language=en&categories=Trailer"
    data = http_get_json(url, headers=api_key_headers(token, ("KINOCHECK_API_KEY", "KINOCHECK_TOKEN")))
    if not data:
        data = http_get_json(
            f"https://api.kinocheck.com/shows?tmdb_id={tmdb_id}&language=en",
            headers=api_key_headers(token, ("KINOCHECK_API_KEY", "KINOCHECK_TOKEN")),
        )
    if not data:
        return []
    candidates: list[Candidate] = []
    items = [data.get("trailer")] if data.get("trailer") else []
    items.extend(data.get("videos") or [])
    seen: set[str] = set()
    for item in items:
        if not item:
            continue
        categories = {str(category).lower() for category in item.get("categories") or []}
        if categories and "trailer" not in categories:
            continue
        video_id = str(item.get("youtube_video_id") or "").strip()
        if not video_id or video_id in seen:
            continue
        seen.add(video_id)
        title = str(item.get("title") or f"{series.display_name} trailer")
        if not any(word in title.lower() for word in TRAILER_WORDS):
            continue
        views = int(item.get("views") or 0)
        candidate = candidate_from_metadata(
            series,
            f"https://www.youtube.com/watch?v={video_id}",
            title,
            "KinoCheck",
            None,
            max_duration,
            SOURCE_SCORE_BONUSES["kinocheck"] + min(views // 10_000, 25),
        )
        if candidate:
            candidates.append(candidate)
    return candidates


def collect_archive_org_candidates(
    series: SeriesFolder,
    search_results: int,
    max_duration: int,
    max_height: int = 0,
) -> list[Candidate]:
    terms = f"{series.title} {series.year or ''} trailer".strip()
    query = f'collection:movie_trailers AND title:({terms})'
    url = "https://archive.org/advancedsearch.php?" + urllib.parse.urlencode(
        {
            "q": query,
            "fl[]": ["identifier", "title"],
            "rows": max(1, min(search_results, 10)),
            "page": 1,
            "output": "json",
        },
        doseq=True,
    )
    data = http_get_json(url)
    docs = ((data or {}).get("response") or {}).get("docs") or []
    candidates: list[Candidate] = []
    for doc in docs:
        identifier = str(doc.get("identifier") or "")
        title = str(doc.get("title") or identifier)
        if not identifier:
            continue
        if score_candidate(series, {"title": title, "channel": "Internet Archive", "duration": None}, max_duration) is None:
            continue
        metadata = http_get_json(f"https://archive.org/metadata/{urllib.parse.quote(identifier)}")
        files = (metadata or {}).get("files") or []
        media_files = []
        for file_info in files:
            name = str(file_info.get("name") or "")
            if not name.lower().endswith(DIRECT_MEDIA_EXTENSIONS):
                continue
            try:
                size = int(file_info.get("size") or 0)
            except ValueError:
                size = 0
            if size and size > 1_500_000_000:
                continue
            if exceeds_max_height_by_label(max_height, name):
                continue
            media_files.append((name, size))
        for name, _size in sorted(media_files, key=lambda item: quality_score(item[0]), reverse=True):
            direct = f"https://archive.org/download/{urllib.parse.quote(identifier)}/{urllib.parse.quote(name)}"
            candidate = candidate_from_metadata(
                series,
                direct,
                title,
                "Internet Archive",
                None,
                max_duration,
                SOURCE_SCORE_BONUSES["internet-archive"],
                max_height,
            )
            if candidate:
                candidates.append(candidate)
                break
    return candidates


def normalise_max_height(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def max_height_label(max_height: int) -> str:
    max_height = normalise_max_height(max_height)
    if max_height <= 0:
        return "unlimited"
    if max_height >= 2160:
        return "4K/2160p"
    return f"{max_height}p"


def max_format_height(info: dict, max_height: int = 0) -> int | None:
    max_height = normalise_max_height(max_height)
    heights = []
    for fmt in info.get("formats") or []:
        try:
            height = int(fmt.get("height") or 0)
        except (TypeError, ValueError):
            height = 0
        if height > 0 and (max_height <= 0 or height <= max_height):
            heights.append(height)
    return max(heights) if heights else None


def quality_height_bonus(height: int | None) -> int:
    if not height:
        return 0
    if height >= 2160:
        return 9000
    if height >= 1440:
        return 6500
    if height >= 1080:
        return 4000
    if height >= 720:
        return 1800
    return 0


def quality_height_label(height: int | None) -> str:
    if not height:
        return ""
    if height >= 2160:
        return "4K"
    return f"{height}p"


def probe_candidate_height(
    candidate: Candidate,
    ydl_base_opts: dict,
    cancel_event: threading.Event | None = None,
    max_height: int = 0,
) -> int | None:
    if yt_dlp is None or is_direct_media_url(candidate.url):
        return None
    opts = {
        **ydl_base_opts,
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            check_cancel(cancel_event)
            info = ydl.extract_info(candidate.url, download=False)
    except Exception as exc:
        check_cancel(cancel_event)
        if "cookiesfrombrowser" in opts and is_cookie_decrypt_error(exc):
            try:
                with yt_dlp.YoutubeDL(without_browser_cookies(opts)) as ydl:
                    check_cancel(cancel_event)
                    info = ydl.extract_info(candidate.url, download=False)
            except Exception:
                return None
        else:
            return None
    return max_format_height(info or {}, max_height)


def probe_dailymotion_height(url: str) -> int | None:
    if yt_dlp is None:
        return None
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        if is_impersonation_dependency_error(exc):
            print_impersonation_help()
        return None
    return max_format_height(info or {})


def collect_dailymotion_candidates(
    series: SeriesFolder,
    search_results: int,
    max_duration: int,
    max_height: int = 0,
) -> list[Candidate]:
    potential: list[Candidate] = []
    seen: set[str] = set()
    for query in build_queries(series, include_network_queries=False)[:4]:
        url = "https://api.dailymotion.com/videos?" + urllib.parse.urlencode(
            {
                "search": query,
                "fields": "id,title,owner.screenname,duration,url",
                "limit": max(1, min(search_results, 20)),
            }
        )
        data = http_get_json(url)
        for item in (data or {}).get("list") or []:
            raw_url = str(item.get("url") or "")
            if not raw_url or raw_url in seen:
                continue
            seen.add(raw_url)
            title = str(item.get("title") or raw_url)
            if series.year and series.year not in title:
                continue
            title_lower = title.lower()
            if "official trailer" not in title_lower and "official teaser" not in title_lower:
                continue
            try:
                duration = int(item.get("duration")) if item.get("duration") else None
            except ValueError:
                duration = None
            candidate = candidate_from_metadata(
                series,
                raw_url,
                title,
                str(item.get("owner.screenname") or "Dailymotion"),
                duration,
                max_duration,
                SOURCE_SCORE_BONUSES["dailymotion"],
            )
            if candidate:
                potential.append(candidate)

    candidates: list[Candidate] = []
    for candidate in sorted(potential, key=lambda item: item.score, reverse=True)[: max(5, min(search_results, 12))]:
        height = probe_dailymotion_height(candidate.url)
        if height is None:
            continue
        if height < DEFAULT_DAILYMOTION_MIN_HEIGHT:
            print(f"    dailymotion skipped low-quality candidate ({height}p): {candidate.title}")
            continue
        if max_height and height > max_height:
            continue
        candidates.append(
            Candidate(
                url=candidate.url,
                title=f"{candidate.title} [{height}p]",
                channel=candidate.channel,
                duration=candidate.duration,
                score=candidate.score + quality_score(f"{height}p"),
            )
        )
    return candidates


def parse_iso8601_duration_seconds(value: str | None) -> int | None:
    if not value:
        return None
    match = re.fullmatch(
        r"P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?",
        value,
    )
    if not match:
        return None
    days = int(match.group("days") or 0)
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = int(match.group("seconds") or 0)
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def collect_youtube_api_candidates(
    series: SeriesFolder,
    search_results: int,
    max_duration: int,
    include_network_queries: bool,
    token: str | None,
) -> list[Candidate]:
    api_key = (token or "").strip() or os_environ_first("YOUTUBE_API_KEY", "YOUTUBE_DATA_API_KEY")
    if not api_key:
        print("    youtube-api skipped: no YouTube Data API key configured")
        return []

    video_ids: list[str] = []
    snippets: dict[str, dict] = {}
    seen: set[str] = set()
    for query in build_queries(series, include_network_queries)[:6]:
        params = {
            "part": "snippet",
            "type": "video",
            "videoEmbeddable": "true",
            "safeSearch": "none",
            "maxResults": max(1, min(search_results, 25)),
            "q": query,
            "key": api_key,
        }
        data = http_get_json("https://www.googleapis.com/youtube/v3/search?" + urllib.parse.urlencode(params))
        for item in (data or {}).get("items") or []:
            video_id = str(((item.get("id") or {}).get("videoId")) or "")
            if not video_id or video_id in seen:
                continue
            seen.add(video_id)
            video_ids.append(video_id)
            snippets[video_id] = item.get("snippet") or {}

    candidates: list[Candidate] = []
    if not video_ids:
        return candidates
    for offset in range(0, len(video_ids), 50):
        batch = video_ids[offset : offset + 50]
        params = {
            "part": "snippet,contentDetails,statistics,status",
            "id": ",".join(batch),
            "key": api_key,
        }
        details = http_get_json("https://www.googleapis.com/youtube/v3/videos?" + urllib.parse.urlencode(params))
        for item in (details or {}).get("items") or []:
            video_id = str(item.get("id") or "")
            snippet = item.get("snippet") or snippets.get(video_id, {})
            status = item.get("status") or {}
            if status.get("privacyStatus") and status.get("privacyStatus") != "public":
                continue
            title = str(snippet.get("title") or "")
            channel = str(snippet.get("channelTitle") or "")
            title_lower = title.lower()
            if "trailer" not in title_lower and "teaser" not in title_lower:
                continue
            if any(word in title_lower for word in ("extended preview", "first look", "first minutes", "opening scene")):
                continue
            duration = parse_iso8601_duration_seconds((item.get("contentDetails") or {}).get("duration"))
            definition = str((item.get("contentDetails") or {}).get("definition") or "").lower()
            stats = item.get("statistics") or {}
            try:
                views = int(stats.get("viewCount") or 0)
            except (TypeError, ValueError):
                views = 0

            base_score = score_candidate(
                series,
                {
                    "title": title,
                    "channel": channel,
                    "duration": duration,
                },
                max_duration,
            )
            if base_score is None:
                continue

            haystack = f"{title} {channel}".lower()
            trusted_bonus = trusted_youtube_channel_bonus(channel)
            official_signal = "official" in haystack or trusted_bonus >= 750
            if not official_signal and trusted_bonus == 0:
                # The API search is cleaner than scraper search, but repost channels still
                # often over-claim quality in titles. Keep them as weak fallbacks only.
                untrusted_penalty = 900
            else:
                untrusted_penalty = 0
            bonus = SOURCE_SCORE_BONUSES["youtube-api"] + trusted_bonus - untrusted_penalty
            if definition == "hd":
                bonus += 350
            if "official" in haystack:
                bonus += 250
            if "official trailer" in haystack:
                bonus += 250
            bonus += min(views // 1_000_000, 30)
            candidates.append(
                Candidate(
                    url=f"https://www.youtube.com/watch?v={video_id}",
                    title=title,
                    channel=channel,
                    duration=duration,
                    score=base_score + bonus + quality_score(title, channel),
                )
            )
    return candidates


def trusted_youtube_channel_bonus(channel: str) -> int:
    haystack = channel.lower()
    trusted = {
        "netflix": 1000,
        "apple tv": 950,
        "hbo": 950,
        "max": 850,
        "prime video": 900,
        "amazon prime video": 900,
        "disney plus": 900,
        "disney+": 900,
        "hulu": 850,
        "paramount plus": 850,
        "peacock": 800,
        "showtime": 800,
        "fx networks": 800,
        "amc": 800,
        "starz": 750,
        "bbc": 750,
        "sky tv": 750,
        "universal pictures": 900,
        "warner bros": 900,
        "paramount pictures": 900,
        "sony pictures": 850,
        "20th century studios": 850,
        "marvel entertainment": 850,
        "lionsgate": 800,
        "a24": 800,
        "focus features": 800,
        "the fast saga": 750,
        "movieclips trailers": 650,
        "rotten tomatoes trailers": 600,
        "rotten tomatoes classic trailers": 600,
        "kinocheck": 550,
    }
    return max((bonus for name, bonus in trusted.items() if name in haystack), default=0)


def build_queries(series: SeriesFolder, include_network_queries: bool) -> list[str]:
    bases = [f"{title} {series.year}".strip() if series.year else title for title in title_query_variants(series.title)]
    queries = []
    for base in bases:
        queries.extend(
            [
                f"{base} official trailer",
                f"{base} trailer",
                f"{base} teaser trailer",
                f"{base} official series trailer",
                f"{base} official tv series trailer",
                f"{base} season 1 trailer",
                f"{base} kinocheck trailer",
                f"{base} ign trailer",
            ]
        )
    if include_network_queries:
        for base in bases:
            queries.extend(
                [
                    f"{base} netflix trailer",
                    f"{base} hbo trailer",
                    f"{base} prime video trailer",
                    f"{base} apple tv trailer",
                    f"{base} official network trailer",
                ]
            )
    return list(dict.fromkeys(queries))


def score_candidate(series: SeriesFolder, entry: dict, max_duration: int) -> int | None:
    title = str(entry.get("title") or "")
    channel = str(entry.get("channel") or entry.get("uploader") or "")
    duration = entry.get("duration")

    if duration and int(duration) > max_duration:
        return None

    haystack = f"{title} {channel}".lower()
    title_words = normalise_words(series.title)
    haystack_words = normalise_words(haystack)
    hit_words = title_words & haystack_words
    title_number_words = {word for word in title_words if word.isdigit()}
    candidate_years = years_in_text(haystack)

    if title_words and len(hit_words) < max(1, min(3, len(title_words))):
        return None
    if title_number_words and not (title_number_words & haystack_words):
        return None
    if series.year and candidate_years and series.year not in candidate_years:
        return None
    if series.year and series.year in candidate_years:
        score = 8
    elif series.year:
        # Yearless candidates are allowed only as fallbacks. Wrong-year
        # candidates are rejected above.
        score = -8
    else:
        score = 0

    if not any(word in haystack for word in TRAILER_WORDS):
        return None

    if re.search(r"\b(?:s\d{1,2}e\d{1,3}|episode\s+\d+)\b", haystack):
        return None

    if has_foreign_language_marker(series, haystack):
        return None
    if has_3d_trailer_marker(series, title):
        return None

    score += 10
    score += 3 * sum(1 for word in TRAILER_WORDS if word in haystack)
    score += 5 if "official" in haystack else 0
    score += 4 if "trailer" in haystack else 0
    score += 10 if series.year and series.year in candidate_years else 0
    score += 3 * len(title_number_words & haystack_words)
    score += 5 if any(kind in haystack for kind in ("series trailer", "tv series trailer", "show trailer")) else 0
    score += 3 if re.search(r"\bseason\s*(?:1|one)\b", haystack) else 0
    score -= 12 if re.search(r"\bseason\s*(?:[2-9]|[1-9]\d+)\b", haystack) else 0
    score += 2 if "kinocheck" in haystack else 0
    score += 3 if any(word in haystack_words for word in ("english", "eng")) else 0
    score -= 8 * sum(1 for word in BAD_WORDS if word in haystack)

    if duration:
        # Most trailers are roughly 60-240 seconds.
        seconds = int(duration)
        if 60 <= seconds <= 240:
            score += 4
        elif seconds < 30:
            score -= 4

    return score


def collect_candidates(
    series: SeriesFolder,
    search_results: int,
    max_duration: int,
    include_network_queries: bool,
    ydl_base_opts: dict,
    search_delay: float = DEFAULT_SEARCH_DELAY,
    source_order: str | None = None,
    tmdb_token: str | None = None,
    kinocheck_token: str | None = None,
    youtube_api_key: str | None = None,
    max_height: int = DEFAULT_MAX_TRAILER_HEIGHT,
) -> list[Candidate]:
    def candidate_from_entry(entry: dict, seen: set[str], score_bonus: int = 0) -> Candidate | None:
        raw_url = str(entry.get("webpage_url") or entry.get("url") or "")
        video_id = str(entry.get("id") or "")
        dedupe_key = video_id or raw_url
        if not dedupe_key or dedupe_key in seen:
            return None

        score = score_candidate(series, entry, max_duration)
        if score is None:
            return None

        seen.add(dedupe_key)
        if raw_url.startswith("http"):
            url = raw_url
        else:
            url_id = video_id or raw_url
            url = f"https://www.youtube.com/watch?v={url_id}"

        return Candidate(
            url=url,
            title=str(entry.get("title") or video_id or raw_url),
            channel=str(entry.get("channel") or entry.get("uploader") or ""),
            duration=int(entry["duration"]) if entry.get("duration") else None,
            score=score + score_bonus + quality_score(entry.get("title"), raw_url),
        )

    def search_with_options(search_opts: dict) -> list[Candidate]:
        seen: set[str] = set()
        found: list[Candidate] = []
        queries = build_queries(series, include_network_queries)

        with yt_dlp.YoutubeDL(search_opts) as ydl:
            for query_index, query in enumerate(queries):
                if query_index:
                    polite_sleep(search_delay)
                try:
                    info = ydl.extract_info(f"ytsearch{search_results}:{query}", download=False)
                except Exception as exc:
                    if "cookiesfrombrowser" in search_opts and is_cookie_decrypt_error(exc):
                        raise
                    if is_forbidden_error(exc):
                        raise
                    print(f"    search failed for {query!r}: {exc}")
                    continue

                for entry in info.get("entries") or []:
                    if not entry:
                        continue
                    candidate = candidate_from_entry(entry, seen)
                    if candidate:
                        found.append(candidate)
        return found

    source_names = source_order_from_value(source_order)
    non_youtube_candidates: list[Candidate] = []
    tmdb_id: int | None = None

    for source in source_names:
        try:
            if source == "tmdb":
                if not ((tmdb_token or "").strip() or os_environ_first("TMDB_BEARER_TOKEN", "TMDB_ACCESS_TOKEN", "TMDB_API_KEY")):
                    print("    tmdb skipped: no TMDb token/key configured")
                    continue
                found, tmdb_id = collect_tmdb_candidates(series, max_duration, tmdb_token, max_height)
            elif source == "kinocheck":
                if not tmdb_id and ((tmdb_token or "").strip() or os_environ_first("TMDB_BEARER_TOKEN", "TMDB_ACCESS_TOKEN", "TMDB_API_KEY")):
                    details = tmdb_series_details(series, tmdb_token)
                    tmdb_id = int(details["id"]) if details and details.get("id") else None
                found = collect_kinocheck_candidates(series, max_duration, tmdb_id, kinocheck_token)
            elif source == "youtube-api":
                found = collect_youtube_api_candidates(series, search_results, max_duration, include_network_queries, youtube_api_key)
            elif source == "internet-archive":
                found = collect_archive_org_candidates(series, search_results, max_duration, max_height)
            elif source == "dailymotion":
                found = collect_dailymotion_candidates(series, search_results, max_duration, max_height)
            else:
                continue
        except Exception as exc:
            print(f"    {source} source failed: {exc}")
            continue
        if found:
            print(f"    {source} found {len(found)} candidate(s)")
            non_youtube_candidates.extend(found)

    if "youtube" not in source_names:
        return sorted(dedupe_candidates(non_youtube_candidates), key=lambda c: c.score, reverse=True)

    opts = {
        **ydl_base_opts,
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
    }

    retried_without_cookies = False
    try:
        candidates = search_with_options(opts)
    except Exception as exc:
        if "cookiesfrombrowser" in opts and is_cookie_decrypt_error(exc):
            print("    browser cookie decrypt failed; retrying searches without browser cookies")
            retried_without_cookies = True
            candidates = search_with_options(without_browser_cookies(opts))
        elif is_forbidden_error(exc):
            candidates = []
            retried_without_cookies = "cookiefile" in opts or "cookiesfrombrowser" in opts
            unblocked = False
            for label, retry_opts in youtube_retry_option_sets(opts):
                try:
                    print(f"    YouTube returned 403; retrying searches {label}")
                    candidates = search_with_options(retry_opts)
                    unblocked = True
                    break
                except Exception as retry_exc:
                    if is_forbidden_error(retry_exc) or is_cookie_decrypt_error(retry_exc):
                        continue
                    raise
            if not unblocked:
                print("    YouTube search is still blocked with HTTP 403; refresh cookies or try again later")
        else:
            raise

    if candidates:
        return sorted(dedupe_candidates(non_youtube_candidates + candidates), key=lambda c: c.score, reverse=True)

    if "cookiesfrombrowser" not in opts or retried_without_cookies:
        return sorted(dedupe_candidates(non_youtube_candidates), key=lambda c: c.score, reverse=True)

    print("    retrying searches without browser cookies")
    return sorted(
        dedupe_candidates(non_youtube_candidates + search_with_options(without_browser_cookies(opts))),
        key=lambda c: c.score,
        reverse=True,
    )


def dedupe_candidates(candidates: list[Candidate]) -> list[Candidate]:
    unique: dict[str, Candidate] = {}
    for candidate in candidates:
        key = candidate.url
        existing = unique.get(key)
        if existing is None or candidate.score > existing.score:
            unique[key] = candidate
    return list(unique.values())


def rerank_candidates_by_available_quality(
    candidates: list[Candidate],
    ydl_base_opts: dict,
    probe_limit: int,
    cancel_event: threading.Event | None = None,
    max_height: int = 0,
) -> list[Candidate]:
    if not candidates or probe_limit <= 0:
        return candidates

    ordered = sorted(candidates, key=lambda c: c.score, reverse=True)
    adjusted: list[Candidate] = []
    probed = 0
    for candidate in ordered:
        check_cancel(cancel_event)
        if probed >= probe_limit:
            adjusted.append(candidate)
            continue
        probed += 1
        height = probe_candidate_height(candidate, ydl_base_opts, cancel_event, max_height)
        bonus = quality_height_bonus(height)
        if bonus <= 0:
            adjusted.append(candidate)
            continue
        label = quality_height_label(height)
        print(f"    quality probe: {label} available - {candidate.title}")
        adjusted.append(
            Candidate(
                url=candidate.url,
                title=candidate.title,
                channel=candidate.channel,
                duration=candidate.duration,
                score=candidate.score + bonus,
            )
        )
    return sorted(adjusted, key=lambda c: c.score, reverse=True)


def clean_temp_dir(temp_dir: Path) -> None:
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)


def find_downloaded_file(temp_dir: Path, base_name: str) -> Path | None:
    matches = [
        p
        for p in temp_dir.iterdir()
        if p.is_file()
        and p.stem == base_name
        and not p.name.lower().endswith((".part", ".ytdl", ".tmp"))
    ]
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime)


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def ffprobe_path() -> str | None:
    return shutil.which("ffprobe")


def probe_media_stream(path: Path, timeout: int = 20) -> tuple[bool, str, int | None, int | None]:
    ffprobe = ffprobe_path()
    if not ffprobe:
        return True, "ffprobe not available", None, None
    if not path.exists() or path.stat().st_size <= 0:
        return False, "file is empty or missing", None, None
    command = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_type,width,height,duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "ffprobe timed out", None, None
    except Exception as exc:
        return False, str(exc), None, None
    details = (result.stderr or result.stdout or "").strip()
    if result.returncode != 0:
        return False, details or f"ffprobe exited with code {result.returncode}", None, None
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return False, "ffprobe returned invalid JSON", None, None
    streams = data.get("streams") or []
    for stream in streams:
        if stream.get("codec_type") != "video":
            continue
        try:
            width = int(stream.get("width") or 0) or None
        except (TypeError, ValueError):
            width = None
        try:
            height = int(stream.get("height") or 0) or None
        except (TypeError, ValueError):
            height = None
        dimensions = f" ({width}x{height})" if width and height else ""
        return True, f"ok{dimensions}", width, height
    return False, "no video stream found", None, None


def probe_media_file(path: Path, timeout: int = 20) -> tuple[bool, str]:
    valid, detail, _width, _height = probe_media_stream(path, timeout)
    return valid, detail


def is_invalid_media_error_message(message: object) -> bool:
    text = str(message).lower()
    return any(
        marker in text
        for marker in (
            "moov atom not found",
            "invalid data found when processing input",
            "error opening input",
            "no video stream found",
            "file is empty",
        )
    )


def run_ffmpeg(command: list[str], cancel_event: threading.Event | None = None) -> None:
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    while True:
        try:
            check_cancel(cancel_event)
            stdout, stderr = process.communicate(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            continue
        except CancelledByUser:
            process.kill()
            process.communicate()
            raise
    if process.returncode != 0:
        details = (stderr or stdout or "").strip()
        raise RuntimeError(details[-1200:] if details else f"ffmpeg exited with code {process.returncode}")


def install_dependencies() -> int:
    if not INSTALLER_PATH.exists():
        print(f"Installer not found: {INSTALLER_PATH}")
        return 1
    command = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(INSTALLER_PATH),
    ]
    return subprocess.call(command)


def ffmpeg_video_encoder_option_sets(encoder: str, preset: str, crf: int) -> list[tuple[str, list[str]]]:
    encoder = (encoder or DEFAULT_FFMPEG_ENCODER).strip().lower()
    if encoder not in FFMPEG_ENCODERS:
        encoder = DEFAULT_FFMPEG_ENCODER

    cpu_options = (
        "CPU libx264",
        ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p"],
    )
    gpu_options = {
        "nvidia": (
            "NVIDIA NVENC",
            ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", str(crf), "-pix_fmt", "yuv420p"],
        ),
        "intel": (
            "Intel Quick Sync",
            ["-c:v", "h264_qsv", "-global_quality", str(crf), "-pix_fmt", "nv12"],
        ),
        "amd": (
            "AMD AMF",
            ["-c:v", "h264_amf", "-quality", "balanced", "-rc", "cqp", "-qp_i", str(crf), "-qp_p", str(crf), "-pix_fmt", "yuv420p"],
        ),
    }

    if encoder == "cpu":
        return [cpu_options]
    if encoder == "auto":
        return [gpu_options["nvidia"], gpu_options["intel"], gpu_options["amd"], cpu_options]
    return [gpu_options[encoder], cpu_options]


def convert_to_normalized_mp4(
    source: Path,
    output: Path,
    threads: int = DEFAULT_FFMPEG_THREADS,
    preset: str = DEFAULT_FFMPEG_PRESET,
    crf: int = DEFAULT_FFMPEG_CRF,
    encoder: str = DEFAULT_FFMPEG_ENCODER,
    cancel_event: threading.Event | None = None,
) -> Path:
    check_cancel(cancel_event)
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise RuntimeError("FFmpeg was not found on PATH. Install it with: winget install Gyan.FFmpeg")

    threads = max(1, min(int(threads), 16))
    preset = preset if preset in FFMPEG_PRESETS else DEFAULT_FFMPEG_PRESET
    crf = max(16, min(int(crf), 30))

    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()

    base_command = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-threads",
        str(threads),
        "-filter_threads",
        "1",
        "-filter_complex_threads",
        "1",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
    ]
    tail_command = ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
    encoder_options = ffmpeg_video_encoder_option_sets(encoder, preset, crf)
    last_error: Exception | None = None

    for encoder_label, video_options in encoder_options:
        command = base_command + video_options + tail_command
        try:
            run_ffmpeg(command + ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11", str(output)], cancel_event)
            return output
        except RuntimeError as exc:
            last_error = exc
            if output.exists():
                output.unlink()
            try:
                run_ffmpeg(command + [str(output)], cancel_event)
                return output
            except RuntimeError as retry_exc:
                last_error = retry_exc
                if output.exists():
                    output.unlink()
                if encoder_label != encoder_options[-1][0]:
                    print(f"    FFmpeg {encoder_label} conversion failed; trying next encoder")
                continue

    raise RuntimeError(last_error or "FFmpeg conversion failed")


def download_format_option_sets(
    base_opts: dict,
    outtmpl: str,
    cancel_event: threading.Event | None = None,
    max_height: int = DEFAULT_MAX_TRAILER_HEIGHT,
) -> list[tuple[str, dict]]:
    common_opts = {
        **base_opts,
        "outtmpl": outtmpl,
        "noplaylist": True,
        "windowsfilenames": True,
        "quiet": True,
        "no_warnings": True,
        "logger": QuietDownloadLogger(),
    }
    if cancel_event is not None:
        common_opts["progress_hooks"] = [cancel_progress_hook(cancel_event)]
    max_height = normalise_max_height(max_height)
    if max_height > 0:
        label = max_height_label(max_height)
        return [
            (
                f"highest available video+audio up to {label}",
                {
                    **common_opts,
                    "format": (
                        f"bv*[height<={max_height}]+ba/"
                        f"bestvideo*[height<={max_height}]+bestaudio/"
                        f"best*[height<={max_height}]/b[height<={max_height}]"
                    ),
                },
            ),
            (
                f"best playable single-file up to {label}",
                {
                    **common_opts,
                    "format": f"best[height<={max_height}]/b[height<={max_height}]",
                },
            ),
            (
                f"MP4-compatible fallback up to {label}",
                {
                    **common_opts,
                    "format": (
                        f"best[height<={max_height}][ext=mp4]/"
                        f"b[height<={max_height}][ext=mp4]/"
                        f"best[height<={max_height}]/b[height<={max_height}]"
                    ),
                },
            ),
        ]
    return [
        (
            "highest available video+audio",
            {
                **common_opts,
                "format": "bv*+ba/bestvideo*+bestaudio/best",
            },
        ),
        (
            "yt-dlp automatic best available format",
            {
                **common_opts,
            },
        ),
        (
            "best playable single-file fallback",
            {
                **common_opts,
                "format": "best/b",
            },
        ),
        (
            "MP4-compatible fallback",
            {
                **common_opts,
                "format": "best[ext=mp4]/b[ext=mp4]/best/b",
            },
        ),
    ]


def run_download_with_retries(opts: dict, url: str, cancel_event: threading.Event | None = None) -> None:
    check_cancel(cancel_event)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as exc:
        check_cancel(cancel_event)
        if "cookiesfrombrowser" in opts and is_cookie_decrypt_error(exc):
            print("    browser cookie decrypt failed; retrying download without browser cookies")
            check_cancel(cancel_event)
            with yt_dlp.YoutubeDL(without_browser_cookies(opts)) as ydl:
                ydl.download([url])
        elif is_impersonation_dependency_error(exc):
            print_impersonation_help()
            raise
        elif is_forbidden_error(exc):
            last_error: Exception = exc
            downloaded_ok = False
            for label, retry_opts in youtube_retry_option_sets(opts):
                try:
                    print(f"    YouTube returned 403; retrying download {label}")
                    check_cancel(cancel_event)
                    with yt_dlp.YoutubeDL(retry_opts) as ydl:
                        ydl.download([url])
                    downloaded_ok = True
                    break
                except Exception as retry_exc:
                    last_error = retry_exc
                    if is_forbidden_error(retry_exc) or is_cookie_decrypt_error(retry_exc):
                        continue
                    raise
            if not downloaded_ok:
                print("    YouTube download is still blocked with HTTP 403; refresh cookies or try again later")
                raise last_error
        else:
            raise


def download_direct_media(url: str, output: Path, cancel_event: threading.Event | None = None) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    user_agent = "QuickTime/7.6.2" if "movietrailers.apple.com" in urllib.parse.urlparse(url).netloc.lower() else HTTP_USER_AGENT
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with urllib.request.urlopen(request, timeout=30) as response, output.open("wb") as handle:
            content_type = str(response.headers.get("Content-Type") or "").lower()
            if any(kind in content_type for kind in ("text/html", "text/plain", "application/json", "xml")):
                raise RuntimeError(f"direct media URL returned non-media content type: {content_type}")
            while True:
                check_cancel(cancel_event)
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
    except Exception:
        if output.exists():
            output.unlink(missing_ok=True)
        raise
    return output


def download_candidate(
    series: SeriesFolder,
    candidate: Candidate,
    index: int,
    temp_dir: Path,
    ydl_base_opts: dict,
    dry_run: bool,
    ffmpeg_threads: int = DEFAULT_FFMPEG_THREADS,
    ffmpeg_preset: str = DEFAULT_FFMPEG_PRESET,
    ffmpeg_crf: int = DEFAULT_FFMPEG_CRF,
    ffmpeg_encoder: str = DEFAULT_FFMPEG_ENCODER,
    max_height: int = DEFAULT_MAX_TRAILER_HEIGHT,
    cancel_event: threading.Event | None = None,
) -> Path | None:
    global PREFERRED_FORMAT_RECOVERY_LABEL
    check_cancel(cancel_event)
    trailer_stem = "trailer"
    if dry_run:
        print(f"    would download #{index}: {candidate.title} [{candidate.channel}]")
        print(f"      {candidate.url}")
        return None

    outtmpl = str(temp_dir / f"{trailer_stem}.%(ext)s")
    format_profiles = download_format_option_sets(ydl_base_opts, outtmpl, cancel_event, max_height)
    last_error: Exception | None = None
    print(f"    trying #{index}: {candidate.title}")
    if is_direct_media_url(candidate.url):
        clean_temp_dir(temp_dir)
        ext = Path(urllib.parse.urlparse(candidate.url).path).suffix or ".mp4"
        download_direct_media(candidate.url, temp_dir / f"{trailer_stem}{ext}", cancel_event)
    else:
        for profile_index, (label, opts) in enumerate(format_profiles, start=1):
            clean_temp_dir(temp_dir)
            if profile_index > 1:
                print(f"    retrying #{index} with {label}")
            try:
                run_download_with_retries(opts, candidate.url, cancel_event)
                last_error = None
                break
            except Exception as exc:
                check_cancel(cancel_event)
                last_error = exc
                if is_format_unavailable_error(exc):
                    recovered = False
                    for retry_label, retry_opts in format_unavailable_retry_option_sets(opts):
                        clean_temp_dir(temp_dir)
                        print(f"    YouTube stream list incomplete for #{index}; trying {retry_label}")
                        try:
                            run_download_with_retries(retry_opts, candidate.url, cancel_event)
                            last_error = None
                            PREFERRED_FORMAT_RECOVERY_LABEL = retry_label
                            recovered = True
                            break
                        except Exception as retry_exc:
                            last_error = retry_exc
                            if (
                                is_format_unavailable_error(retry_exc)
                                or is_forbidden_error(retry_exc)
                                or is_cookie_decrypt_error(retry_exc)
                                or is_ejs_challenge_error(retry_exc)
                            ):
                                continue
                            raise
                    if recovered:
                        break
                if is_format_unavailable_error(exc) and profile_index < len(format_profiles):
                    print(f"    selected stream set unavailable for #{index}; trying another best-quality selector")
                    continue
                raise

    if last_error is not None:
        raise last_error

    downloaded = find_downloaded_file(temp_dir, trailer_stem)
    if not downloaded:
        print("    download finished, but the output file was not found")
        return None

    is_valid_download, validation_detail = probe_media_file(downloaded)
    if not is_valid_download:
        raise RuntimeError(f"downloaded file is not valid playable media: {validation_detail}")

    target = unique_path(trailer_directory(series) / "trailer.mp4")
    target.parent.mkdir(parents=True, exist_ok=True)
    converted = temp_dir / f"{trailer_stem}-normalized.mp4"
    print("    converting to MP4 and normalizing audio with FFmpeg")
    try:
        convert_to_normalized_mp4(
            downloaded,
            converted,
            threads=ffmpeg_threads,
            preset=ffmpeg_preset,
            crf=ffmpeg_crf,
            encoder=ffmpeg_encoder,
            cancel_event=cancel_event,
        )
        try:
            shutil.move(str(converted), str(target))
        except OSError as move_exc:
            if is_file_access_error(move_exc):
                raise LockedFileSkipped(target, "save the normalized trailer") from move_exc
            raise
        is_valid_target, target_detail = probe_media_file(target)
        if not is_valid_target:
            if target.exists():
                target.unlink(missing_ok=True)
            raise RuntimeError(f"normalized trailer failed validation: {target_detail}")
    except LockedFileSkipped:
        raise
    except Exception as exc:
        print(f"    FFmpeg conversion failed: {exc}")
        if is_invalid_media_error_message(exc):
            raise RuntimeError(f"downloaded file cannot be decoded: {exc}") from exc
        is_valid_original, original_detail = probe_media_file(downloaded)
        if not is_valid_original:
            raise RuntimeError(f"downloaded original failed validation: {original_detail}") from exc
        print("    saving the original downloaded file instead")
        target = unique_path(trailer_directory(series) / downloaded.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(downloaded), str(target))
        except OSError as move_exc:
            if is_file_access_error(move_exc):
                raise LockedFileSkipped(target, "save the downloaded trailer") from move_exc
            raise
        is_valid_target, target_detail = probe_media_file(target)
        if not is_valid_target:
            if target.exists():
                target.unlink(missing_ok=True)
            raise RuntimeError(f"saved original failed validation: {target_detail}") from exc
    return target


def process_series(series: SeriesFolder, args: argparse.Namespace, ydl_base_opts: dict, results: dict | None = None) -> bool:
    cancel_event = getattr(args, "cancel_event", None)
    check_cancel(cancel_event)
    setattr(args, "_last_series_used_network", False)
    print(f"\n== {series.display_name} ==")

    existing_trailers = find_current_trailers(series)
    redownload_existing = bool(getattr(args, "redownload_existing", False))
    skip_success_history = bool(getattr(args, "skip_success_history", True))
    success_record = successful_result_for_series(series, results or {}) if results is not None else None
    if success_record and skip_success_history and not redownload_existing:
        print(f"  skipped: previous success recorded ({success_record.get('trailer_name')})")
        return False
    if existing_trailers and not redownload_existing:
        if results is not None and not args.dry_run:
            record_existing_success(series, existing_trailers[0], results)
        print(f"  skipped: existing trailer found ({existing_trailers[0].name})")
        return False

    setattr(args, "_last_series_used_network", True)
    max_height = normalise_max_height(getattr(args, "max_height", DEFAULT_MAX_TRAILER_HEIGHT))
    candidates = collect_candidates(
        series=series,
        search_results=args.search_results,
        max_duration=args.max_duration,
        include_network_queries=args.include_network_queries,
        ydl_base_opts=ydl_base_opts,
        search_delay=float(getattr(args, "search_delay", DEFAULT_SEARCH_DELAY)),
        source_order=getattr(args, "source_order", DEFAULT_SOURCE_ORDER),
        tmdb_token=getattr(args, "tmdb_token", None),
        kinocheck_token=getattr(args, "kinocheck_token", None),
        youtube_api_key=getattr(args, "youtube_api_key", None),
        max_height=max_height,
    )

    if not candidates:
        print("  no trailer candidates found")
        return False

    probe_limit = max(
        int(getattr(args, "candidate_attempts", DEFAULT_CANDIDATE_ATTEMPTS)),
        int(getattr(args, "quality_probe_limit", DEFAULT_QUALITY_PROBE_LIMIT)),
    )
    candidates = rerank_candidates_by_available_quality(candidates, ydl_base_opts, probe_limit, cancel_event, max_height)

    renamed: list[tuple[Path, Path]] = []
    if existing_trailers:
        renamed = rename_existing_trailers(series, args.dry_run)
        for old, new in renamed:
            action = "would rename for re-download" if args.dry_run else "renamed for re-download"
            print(f"  {action}: {old.name} -> {new.name}")

    candidate_attempts = max(1, int(getattr(args, "candidate_attempts", DEFAULT_CANDIDATE_ATTEMPTS)))
    candidate_attempts = min(candidate_attempts, len(candidates))
    print(f"  found {len(candidates)} candidate(s); trying up to {candidate_attempts}, saving first success")
    downloaded = 0
    temp_dir = series.folder / ".trailer-download-tmp"
    for index, candidate in enumerate(candidates[:candidate_attempts], start=1):
        try:
            check_cancel(cancel_event)
            target = download_candidate(
                series,
                candidate,
                index,
                temp_dir,
                ydl_base_opts,
                args.dry_run,
                ffmpeg_threads=int(getattr(args, "ffmpeg_threads", DEFAULT_FFMPEG_THREADS)),
                ffmpeg_preset=str(getattr(args, "ffmpeg_preset", DEFAULT_FFMPEG_PRESET)),
                ffmpeg_crf=int(getattr(args, "ffmpeg_crf", DEFAULT_FFMPEG_CRF)),
                ffmpeg_encoder=str(getattr(args, "ffmpeg_encoder", DEFAULT_FFMPEG_ENCODER)),
                max_height=max_height,
                cancel_event=cancel_event,
            )
        except CancelledByUser:
            print("    cancel requested; stopping current series")
            if renamed and not args.dry_run:
                restored = restore_renamed_trailers(renamed)
                for backup, original in restored:
                    print(f"    restored previous trailer after cancel: {backup.name} -> {original.name}")
            if temp_dir.exists() and not args.keep_temp and not args.dry_run:
                shutil.rmtree(temp_dir, ignore_errors=True)
            raise
        except Exception as exc:
            if is_format_unavailable_error(exc):
                print(f"    skipped #{index}: no downloadable video format; trying next candidate")
                if not ydl_base_opts.get("js_runtimes"):
                    print("      no supported JavaScript runtime was detected; install Deno or Node.js 22+ for YouTube EJS challenges")
            elif is_impersonation_dependency_error(exc):
                print(f"    skipped #{index}: yt-dlp impersonation dependency is missing; trying next candidate")
                print_impersonation_help()
            else:
                print(f"    failed #{index}: {candidate.title} - {exc}")
            continue
        if target:
            downloaded += 1
            duration = f", {candidate.duration}s" if candidate.duration else ""
            print(f"    saved: {target.name} ({candidate.title}, score {candidate.score}{duration})")
            if not args.dry_run:
                if results is not None:
                    record_success(series, target, candidate, results)
                removed_backups = delete_old_trailer_backups(series, args.dry_run)
                for backup in removed_backups:
                    print(f"    deleted backup after successful download: {backup.name}")
            break

    if renamed and downloaded == 0 and not args.dry_run:
        restored = restore_renamed_trailers(renamed)
        for backup, original in restored:
            print(f"    restored previous trailer after failed re-download: {backup.name} -> {original.name}")

    if temp_dir.exists() and not args.keep_temp and not args.dry_run:
        shutil.rmtree(temp_dir, ignore_errors=True)

    if not args.dry_run:
        print(f"  downloaded {downloaded} trailer(s)")
    return downloaded > 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download best-quality free/public series trailers into series folders."
    )
    parser.add_argument("--gui", action="store_true", help="Open the graphical interface.")
    parser.add_argument("--check-deps", action="store_true", help="Check required external tools and Python packages, then exit.")
    parser.add_argument("--install-deps", action="store_true", help="Run the bundled Windows install.ps1 dependency bootstrapper, then exit.")
    parser.add_argument(
        "--settings-file",
        default=str(SETTINGS_PATH),
        help="JSON settings file to load for CLI runs. Defaults to series_trailer_downloader.settings.json beside the script.",
    )

    parser.add_argument("--root", default=r"C:\TV Shows", help=r"Series library root. Default: C:\TV Shows")
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Find nested show folders by detecting season folders or episode filenames.",
    )
    parser.add_argument(
        "--max-per-series",
        type=int,
        default=1,
        help="Kept for compatibility; the script now saves one trailer per series.",
    )
    parser.add_argument("--search-results", type=int, default=10, help="YouTube results to inspect per query.")
    parser.add_argument("--max-duration", type=int, default=600, help="Ignore videos longer than this many seconds.")
    parser.add_argument(
        "--max-height",
        type=int,
        default=DEFAULT_MAX_TRAILER_HEIGHT,
        help="Maximum video height to download, e.g. 2160, 1440, 1080, or 720. Default: 0 for unlimited.",
    )
    parser.add_argument(
        "--source-order",
        default=DEFAULT_SOURCE_ORDER,
        help=(
            "Comma-separated source order. Choices: tmdb, kinocheck, youtube-api, "
            "internet-archive, dailymotion, youtube. Remove youtube and youtube-api to avoid Google hosts."
        ),
    )
    parser.add_argument(
        "--tmdb-token",
        help="Optional TMDb API read token or v3 API key. Can also use TMDB_BEARER_TOKEN, TMDB_ACCESS_TOKEN, or TMDB_API_KEY.",
    )
    parser.add_argument(
        "--kinocheck-token",
        help="Optional KinoCheck API key. Can also use KINOCHECK_API_KEY or KINOCHECK_TOKEN.",
    )
    parser.add_argument(
        "--youtube-api-key",
        help="Optional YouTube Data API v3 key. Can also use YOUTUBE_API_KEY or YOUTUBE_DATA_API_KEY.",
    )
    parser.add_argument("--candidate-attempts", type=int, default=DEFAULT_CANDIDATE_ATTEMPTS, help="Candidate videos to try per series before giving up.")
    parser.add_argument("--search-delay", type=float, default=DEFAULT_SEARCH_DELAY, help="Seconds to wait between search queries.")
    parser.add_argument("--series-delay", type=float, default=DEFAULT_SERIES_DELAY, help="Seconds to wait between series folders.")
    parser.add_argument(
        "--download-sleep-min",
        type=float,
        default=DEFAULT_DOWNLOAD_SLEEP_MIN,
        help="Minimum yt-dlp sleep before downloads.",
    )
    parser.add_argument(
        "--download-sleep-max",
        type=float,
        default=DEFAULT_DOWNLOAD_SLEEP_MAX,
        help="Maximum yt-dlp sleep before downloads.",
    )
    parser.add_argument(
        "--ffmpeg-threads",
        type=int,
        default=DEFAULT_FFMPEG_THREADS,
        help="Maximum FFmpeg threads for MP4 conversion. Lower values reduce CPU/RAM use.",
    )
    parser.add_argument(
        "--ffmpeg-preset",
        choices=FFMPEG_PRESETS,
        default=DEFAULT_FFMPEG_PRESET,
        help="FFmpeg x264 preset for MP4 conversion. Faster presets use less CPU.",
    )
    parser.add_argument(
        "--ffmpeg-crf",
        type=int,
        default=DEFAULT_FFMPEG_CRF,
        help="FFmpeg quality value for MP4 conversion. CPU uses x264 CRF; GPU maps this to its nearest quality setting.",
    )
    parser.add_argument(
        "--ffmpeg-encoder",
        choices=FFMPEG_ENCODERS,
        default=DEFAULT_FFMPEG_ENCODER,
        help="FFmpeg video encoder: auto tries NVIDIA, Intel, AMD, then CPU fallback.",
    )
    parser.add_argument(
        "--js-runtime",
        default=DEFAULT_JS_RUNTIME,
        help=r"yt-dlp JavaScript runtime for YouTube challenges, e.g. node, deno, or node:C:\path\to\node.exe.",
    )
    parser.add_argument(
        "--remote-components",
        default=DEFAULT_REMOTE_COMPONENTS,
        help="yt-dlp remote components, e.g. ejs:github. Use blank to disable.",
    )
    parser.add_argument("--include-network-queries", action="store_true", help="Also search network/platform-flavoured queries.")
    parser.add_argument("--cookies-file", help="Optional Netscape cookies.txt file to use for yt-dlp.")
    parser.add_argument("--results-file", help="JSON file that records successful trailer results.")
    parser.add_argument(
        "--log-file",
        nargs="?",
        const=str(DEFAULT_LOG_PATH),
        default=str(DEFAULT_LOG_PATH),
        help='Progress log file. Default: series_trailer_downloader.log beside the script. Use --log-file without a path for the default, or --log-file "" to disable.',
    )
    parser.add_argument("--cookies-from-browser", help="Optional yt-dlp browser cookies source, e.g. chrome or edge.")
    parser.add_argument(
        "--extract-cookies-from-browser",
        choices=("edge", "chrome", "chromium", "firefox", "brave", "vivaldi", "opera"),
        help="Extract browser cookies to --cookies-file, then exit.",
    )
    parser.add_argument("--limit", type=int, help="Only process the first N series folders.")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without renaming/downloading.")
    parser.add_argument(
        "--redownload-existing",
        action="store_true",
        help="Re-download trailers even when a current trailer already exists.",
    )
    parser.add_argument(
        "--ignore-success-history",
        action="store_true",
        help="Ignore the saved results file when deciding what to skip.",
    )
    parser.add_argument("--keep-temp", action="store_true", help="Keep per-series temporary download folders.")
    return parser


def supplied_cli_dests(parser: argparse.ArgumentParser, argv: list[str]) -> set[str]:
    option_dests: dict[str, str] = {}
    for action in parser._actions:
        for option in action.option_strings:
            option_dests[option] = action.dest

    supplied: set[str] = set()
    for token in argv:
        if token == "--":
            break
        option = token.split("=", 1)[0]
        dest = option_dests.get(option)
        if dest:
            supplied.add(dest)
    return supplied


def setting_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off", ""}:
        return False
    return bool(value)


def coerce_cli_setting(key: str, value: object) -> object:
    if key == "limit" and (value is None or str(value).strip() == ""):
        return None
    int_keys = {
        "max_per_series",
        "search_results",
        "max_duration",
        "max_height",
        "candidate_attempts",
        "ffmpeg_threads",
        "ffmpeg_crf",
        "limit",
    }
    float_keys = {
        "search_delay",
        "series_delay",
        "download_sleep_min",
        "download_sleep_max",
    }
    bool_keys = {
        "include_network_queries",
        "recursive",
        "redownload_existing",
        "ignore_success_history",
        "keep_temp",
    }
    if key in int_keys:
        return int(value)
    if key in float_keys:
        return float(value)
    if key in bool_keys:
        return setting_bool(value)
    return value


def apply_settings_file(args: argparse.Namespace, parser: argparse.ArgumentParser, supplied: set[str]) -> bool:
    settings_file = Path(args.settings_file).expanduser()
    explicit_settings_file = "settings_file" in supplied
    if not settings_file.exists():
        if explicit_settings_file:
            print(f"Settings file does not exist: {settings_file}", file=sys.stderr)
            return False
        return True

    try:
        saved = json.loads(settings_file.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Could not read settings file {settings_file}: {exc}", file=sys.stderr)
        return False
    if not isinstance(saved, dict):
        print(f"Settings file must contain a JSON object: {settings_file}", file=sys.stderr)
        return False

    settings = dict(saved)
    if "skip_success_history" in settings and "ignore_success_history" not in settings:
        settings["ignore_success_history"] = not setting_bool(settings["skip_success_history"])

    valid_dests = {action.dest for action in parser._actions}
    transient_dests = {
        "check_deps",
        "dry_run",
        "extract_cookies_from_browser",
        "gui",
        "help",
        "install_deps",
        "settings_file",
    }
    for key, value in settings.items():
        if key not in valid_dests or key in supplied or key in transient_dests:
            continue
        try:
            setattr(args, key, coerce_cli_setting(key, value))
        except (TypeError, ValueError) as exc:
            print(f"Invalid value for {key} in {settings_file}: {exc}", file=sys.stderr)
            return False

    print(f"Loaded settings from: {settings_file}")
    return True


def run_cli(args: argparse.Namespace) -> int:
    log_file_value = str(getattr(args, "log_file", "") or "").strip()
    if not log_file_value:
        return run_cli_inner(args)

    log_file = Path(log_file_value).expanduser()
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_handle = log_file.open("a", encoding="utf-8", buffering=1)
    except OSError as exc:
        print(f"Could not open progress log {log_file}: {exc}", file=sys.stderr)
        return 1

    old_stdout, old_stderr = sys.stdout, sys.stderr
    try:
        with log_handle:
            sys.stdout = TeeWriter(old_stdout, log_handle)
            sys.stderr = TeeWriter(old_stderr, log_handle)
            print()
            print(f"--- Series Trailer Downloader run started {datetime.now().astimezone().isoformat(timespec='seconds')} ---")
            print(f"Writing progress log to: {log_file}")
            exit_code = run_cli_inner(args)
            print(f"--- Series Trailer Downloader run finished with exit code {exit_code} ---")
            return exit_code
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr


def run_cli_inner(args: argparse.Namespace) -> int:
    args.max_per_series = 1
    cookies_file = Path(args.cookies_file).expanduser() if getattr(args, "cookies_file", None) else DEFAULT_COOKIES_PATH
    results_file = Path(args.results_file).expanduser() if getattr(args, "results_file", None) else DEFAULT_RESULTS_PATH
    args.skip_success_history = not bool(getattr(args, "ignore_success_history", False))

    if getattr(args, "extract_cookies_from_browser", None):
        try:
            extract_cookies_file(args.extract_cookies_from_browser, cookies_file)
        except Exception as exc:
            print(f"Cookie extraction failed: {exc}", file=sys.stderr)
            print("Try closing the browser first, or choose a different browser/profile.", file=sys.stderr)
            return 1
        return 0

    root = Path(args.root)

    if not root.exists() or not root.is_dir():
        print(f"Series root does not exist or is not a directory: {root}", file=sys.stderr)
        return 1

    require_yt_dlp()

    if "dailymotion" in source_order_from_value(getattr(args, "source_order", DEFAULT_SOURCE_ORDER)):
        if importlib.util.find_spec("curl_cffi") is None:
            print("Warning: Dailymotion downloads may require yt-dlp browser impersonation support.")
            print('Install it with: python -m pip install -U "yt-dlp[default,curl-cffi]"')

    ydl_base_opts: dict = {}
    if cookies_file.exists():
        ydl_base_opts["cookiefile"] = str(cookies_file)
    elif args.cookies_from_browser:
        ydl_base_opts["cookiesfrombrowser"] = (args.cookies_from_browser,)
    if getattr(args, "download_sleep_min", 0) > 0:
        ydl_base_opts["sleep_interval"] = float(args.download_sleep_min)
        ydl_base_opts["max_sleep_interval"] = max(
            float(getattr(args, "download_sleep_max", args.download_sleep_min)),
            float(args.download_sleep_min),
        )
    js_runtimes = merge_js_runtimes(
        parse_js_runtimes(getattr(args, "js_runtime", "")),
        parse_js_runtimes(DEFAULT_JS_RUNTIME),
    )
    if js_runtimes:
        ydl_base_opts["js_runtimes"] = js_runtimes
    if getattr(args, "remote_components", ""):
        ydl_base_opts["remote_components"] = parse_remote_components(args.remote_components)
    elif DEFAULT_REMOTE_COMPONENTS:
        ydl_base_opts["remote_components"] = parse_remote_components(DEFAULT_REMOTE_COMPONENTS)

    series_folders = list(iter_series_folders(root, recursive=bool(getattr(args, "recursive", False))))
    if args.limit:
        series_folders = series_folders[: args.limit]
    if not series_folders:
        print(f"No series folders found in {root}")
        return 0

    print(f"Scanning {len(series_folders)} series folder(s) under {root}")
    results = load_results(results_file)
    progress_callback = getattr(args, "progress_callback", None)
    if progress_callback:
        progress_callback(0, len(series_folders), "Starting")
    series_delay = float(getattr(args, "series_delay", DEFAULT_SERIES_DELAY))
    locked_retry_queue: list[SeriesFolder] = []
    for index, series in enumerate(series_folders, start=1):
        try:
            check_cancel(getattr(args, "cancel_event", None))
            changed = process_series(series, args, ydl_base_opts, results)
        except CancelledByUser:
            print("\nRun cancelled by user")
            return 130
        except LockedFileSkipped as exc:
            print(f"  skipped: {exc}. Will retry this series after the queue finishes.")
            locked_retry_queue.append(series)
            changed = False
        if changed or not args.dry_run:
            save_results(results_file, results)
        if progress_callback:
            progress_callback(index, len(series_folders), series.display_name)
        if index < len(series_folders) and series_delay > 0 and getattr(args, "_last_series_used_network", False):
            polite_sleep(series_delay)

    if locked_retry_queue:
        print(f"\nRetrying {len(locked_retry_queue)} series(s) that had open or locked files...")
    for retry_index, series in enumerate(locked_retry_queue, start=1):
        try:
            check_cancel(getattr(args, "cancel_event", None))
            changed = process_series(series, args, ydl_base_opts, results)
        except CancelledByUser:
            print("\nRun cancelled by user")
            return 130
        except LockedFileSkipped as exc:
            print(f"  skipped after retry: {exc}. Close the app using that file and run again later.")
            changed = False
        if changed or not args.dry_run:
            save_results(results_file, results)
        if progress_callback:
            progress_callback(len(series_folders), len(series_folders), f"Retry {retry_index}: {series.display_name}")
        if retry_index < len(locked_retry_queue) and series_delay > 0 and getattr(args, "_last_series_used_network", False):
            polite_sleep(series_delay)

    return 0


def load_gui_settings() -> dict:
    defaults = {
        "root": r"C:\TV Shows",
        "recursive": False,
        "include_network_queries": True,
        "cookies_file": str(DEFAULT_COOKIES_PATH),
        "results_file": str(DEFAULT_RESULTS_PATH),
        "log_file": str(DEFAULT_LOG_PATH),
        "extract_cookies_from_browser": "edge",
        "cookies_from_browser": "",
        "source_order": DEFAULT_SOURCE_ORDER,
        "tmdb_token": os_environ_first("TMDB_BEARER_TOKEN", "TMDB_ACCESS_TOKEN", "TMDB_API_KEY"),
        "kinocheck_token": os_environ_first("KINOCHECK_API_KEY", "KINOCHECK_TOKEN"),
        "youtube_api_key": os_environ_first("YOUTUBE_API_KEY", "YOUTUBE_DATA_API_KEY"),
        "search_results": 10,
        "max_duration": 600,
        "max_height": DEFAULT_MAX_TRAILER_HEIGHT,
        "candidate_attempts": DEFAULT_CANDIDATE_ATTEMPTS,
        "search_delay": DEFAULT_SEARCH_DELAY,
        "series_delay": DEFAULT_SERIES_DELAY,
        "download_sleep_min": DEFAULT_DOWNLOAD_SLEEP_MIN,
        "download_sleep_max": DEFAULT_DOWNLOAD_SLEEP_MAX,
        "ffmpeg_threads": DEFAULT_FFMPEG_THREADS,
        "ffmpeg_preset": DEFAULT_FFMPEG_PRESET,
        "ffmpeg_crf": DEFAULT_FFMPEG_CRF,
        "ffmpeg_encoder": DEFAULT_FFMPEG_ENCODER,
        "js_runtime": DEFAULT_JS_RUNTIME,
        "remote_components": DEFAULT_REMOTE_COMPONENTS,
        "limit": "",
        "redownload_existing": False,
        "skip_success_history": True,
        "keep_temp": False,
    }
    if not SETTINGS_PATH.exists():
        return defaults
    try:
        saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return defaults
    defaults.update({key: saved[key] for key in defaults.keys() & saved.keys()})
    return defaults


def save_gui_settings(settings: dict) -> None:
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")


class QueueWriter:
    def __init__(self, output_queue: queue.Queue[object]) -> None:
        self.output_queue = output_queue

    def write(self, text: str) -> int:
        if text:
            self.output_queue.put(text)
        return len(text)

    def flush(self) -> None:
        return None


class TeeWriter:
    def __init__(self, *streams) -> None:
        self.streams = streams
        self.encoding = getattr(streams[0], "encoding", "utf-8") if streams else "utf-8"

    def write(self, text: str) -> int:
        for stream in self.streams:
            stream.write(text)
        return len(text)

    def flush(self) -> None:
        for stream in self.streams:
            stream.flush()

    def isatty(self) -> bool:
        return any(getattr(stream, "isatty", lambda: False)() for stream in self.streams)


class CancelledByUser(Exception):
    pass


def check_cancel(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise CancelledByUser("Cancelled by user")


def cancel_progress_hook(cancel_event: threading.Event | None):
    def hook(_status: dict) -> None:
        check_cancel(cancel_event)

    return hook


def launch_gui() -> int:
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except ImportError:
        print("Tkinter is not available in this Python install.", file=sys.stderr)
        return 2

    palette = {
        "bg": "#f8fafc",
        "hero": "#0f172a",
        "hero_sub": "#99f6e4",
        "surface": "#ffffff",
        "panel": "#ffffff",
        "panel_alt": "#ffffff",
        "border": "#cbd5e1",
        "text": "#1e293b",
        "muted": "#64748b",
        "button": "#e5e7eb",
        "button_active": "#d1d5db",
        "accent": "#0f766e",
        "accent_dark": "#115e59",
        "accent_soft": "#ccfbf1",
        "good": "#0f766e",
        "warn": "#d97706",
        "danger": "#dc2626",
        "danger_dark": "#b91c1c",
        "input": "#ffffff",
        "log": "#ffffff",
    }

    settings = load_gui_settings()
    root = tk.Tk()
    root.title("Series Trailer Downloader")
    root.geometry("1040x720")
    root.minsize(900, 620)
    root.configure(bg=palette["hero"])

    logo_image = None
    header_logo = None
    logo_path = Path(__file__).with_name("assets") / "wolf-banner.png"
    if logo_path.exists():
        try:
            logo_image = tk.PhotoImage(file=str(logo_path))
            root.iconphoto(True, logo_image)
            scale = max(logo_image.width() // 220, logo_image.height() // 96, 1)
            header_logo = logo_image.subsample(scale, scale)
            root._trailer_logo_image = logo_image
            root._trailer_header_logo = header_logo
        except tk.TclError:
            logo_image = None
            header_logo = None

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure(".", font=("Segoe UI", 9), background=palette["bg"], foreground=palette["text"])
    style.configure("App.TFrame", background=palette["bg"])
    style.configure("Nav.TFrame", background=palette["hero"])
    style.configure("Panel.TFrame", background=palette["panel_alt"], relief="solid", borderwidth=1)
    style.configure("Card.TLabelframe", background="#ffffff", bordercolor="#cbd5e1", relief="solid")
    style.configure("Card.TLabelframe.Label", background="#ffffff", foreground="#0f172a", font=("Segoe UI", 10, "bold"))
    style.configure("Subtle.TFrame", background=palette["surface"], relief="flat")
    style.configure("Row.TFrame", background=palette["panel_alt"])
    style.configure("TLabel", background=palette["panel_alt"], foreground=palette["text"], font=("Segoe UI", 9))
    style.configure("Muted.TLabel", background=palette["panel_alt"], foreground=palette["muted"], font=("Segoe UI", 9))
    style.configure("Header.TLabel", background=palette["hero"], foreground="#f8fafc", font=("Segoe UI", 22, "bold"))
    style.configure("Subheader.TLabel", background=palette["hero"], foreground=palette["hero_sub"], font=("Segoe UI", 10))
    style.configure("CardTitle.TLabel", background=palette["panel_alt"], foreground="#0f172a", font=("Segoe UI", 10, "bold"))
    style.configure("Value.TLabel", background=palette["panel_alt"], foreground=palette["accent"], font=("Segoe UI", 16, "bold"))
    style.configure("Tiny.TLabel", background=palette["panel_alt"], foreground=palette["muted"], font=("Segoe UI", 9))
    style.configure("TButton", background=palette["button"], foreground="#111827", padding=(10, 6), borderwidth=0, font=("Segoe UI", 9))
    style.map("TButton", background=[("active", palette["button_active"]), ("disabled", palette["border"])], foreground=[("disabled", palette["muted"])])
    style.configure("Accent.TButton", background=palette["accent"], foreground="#ffffff", padding=(10, 6), borderwidth=0, font=("Segoe UI", 9, "bold"))
    style.map("Accent.TButton", background=[("active", palette["accent_dark"]), ("disabled", palette["border"])])
    style.configure("Danger.TButton", background=palette["danger"], foreground="#ffffff", padding=(10, 6), borderwidth=0, font=("Segoe UI", 9))
    style.map("Danger.TButton", background=[("active", palette["danger_dark"])])
    style.configure("TEntry", fieldbackground=palette["input"], foreground=palette["text"], bordercolor=palette["border"], lightcolor=palette["border"], darkcolor=palette["border"])
    style.configure("TCombobox", fieldbackground=palette["input"], foreground=palette["text"], bordercolor=palette["border"], lightcolor=palette["border"], darkcolor=palette["border"])
    style.configure(
        "Horizontal.TProgressbar",
        background=palette["accent"],
        troughcolor="#e2e8f0",
        bordercolor="#e2e8f0",
        lightcolor=palette["accent"],
        darkcolor=palette["accent_dark"],
    )
    style.configure("TCheckbutton", background=palette["panel_alt"], foreground=palette["text"])
    style.map("TCheckbutton", background=[("active", palette["panel_alt"])])
    style.configure("TNotebook", background=palette["bg"], borderwidth=0)
    style.configure("TNotebook.Tab", background="#e2e8f0", foreground=palette["text"], padding=(18, 8), font=("Segoe UI", 9, "bold"))
    style.map(
        "TNotebook.Tab",
        background=[("selected", palette["panel_alt"]), ("active", "#cbd5e1")],
        foreground=[("selected", palette["text"]), ("active", palette["text"])],
    )

    root_var = tk.StringVar(value=str(settings["root"]))
    recursive_var = tk.BooleanVar(value=bool(settings["recursive"]))
    include_network_queries_var = tk.BooleanVar(value=bool(settings["include_network_queries"]))
    cookies_file_var = tk.StringVar(value=str(settings["cookies_file"]))
    results_file_var = tk.StringVar(value=str(settings["results_file"]))
    log_file_var = tk.StringVar(value=str(settings["log_file"]))
    extract_browser_var = tk.StringVar(value=str(settings["extract_cookies_from_browser"]))
    cookies_var = tk.StringVar(value=str(settings["cookies_from_browser"]))
    source_order_var = tk.StringVar(value=str(settings["source_order"]))
    tmdb_token_var = tk.StringVar(value=str(settings["tmdb_token"]))
    kinocheck_token_var = tk.StringVar(value=str(settings["kinocheck_token"]))
    youtube_api_key_var = tk.StringVar(value=str(settings["youtube_api_key"]))
    search_results_var = tk.StringVar(value=str(settings["search_results"]))
    max_duration_var = tk.StringVar(value=str(settings["max_duration"]))
    max_height_var = tk.StringVar(value=str(settings["max_height"]))
    candidate_attempts_var = tk.StringVar(value=str(settings["candidate_attempts"]))
    search_delay_var = tk.StringVar(value=str(settings["search_delay"]))
    series_delay_var = tk.StringVar(value=str(settings["series_delay"]))
    download_sleep_min_var = tk.StringVar(value=str(settings["download_sleep_min"]))
    download_sleep_max_var = tk.StringVar(value=str(settings["download_sleep_max"]))
    ffmpeg_threads_var = tk.StringVar(value=str(settings["ffmpeg_threads"]))
    ffmpeg_preset_var = tk.StringVar(value=str(settings["ffmpeg_preset"]))
    ffmpeg_crf_var = tk.StringVar(value=str(settings["ffmpeg_crf"]))
    ffmpeg_encoder_var = tk.StringVar(value=str(settings["ffmpeg_encoder"]))
    js_runtime_var = tk.StringVar(value=str(settings["js_runtime"]))
    remote_components_var = tk.StringVar(value=str(settings["remote_components"]))
    limit_var = tk.StringVar(value=str(settings["limit"]))
    redownload_existing_var = tk.BooleanVar(value=bool(settings["redownload_existing"]))
    skip_success_history_var = tk.BooleanVar(value=bool(settings["skip_success_history"]))
    keep_temp_var = tk.BooleanVar(value=bool(settings["keep_temp"]))
    status_var = tk.StringVar(value="Ready")
    progress_var = tk.DoubleVar(value=0)
    progress_text_var = tk.StringVar(value="Idle")
    root_summary_var = tk.StringVar(value=root_var.get())
    cookie_summary_var = tk.StringVar(value="cookies file" if Path(cookies_file_var.get()).exists() else "public search")
    mode_summary_var = tk.StringVar(value="skip existing trailers")
    running_var = tk.BooleanVar(value=False)
    cancel_event = threading.Event()
    log_queue: queue.Queue[object] = queue.Queue()
    busy_buttons: list[ttk.Button] = []

    def panel(parent, **grid_options):
        frame = ttk.Frame(parent, style="Panel.TFrame", padding=14)
        frame.grid(**grid_options)
        return frame

    def card_title(parent, title: str, subtitle: str | None = None) -> None:
        ttk.Label(parent, text=title, style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        if subtitle:
            ttk.Label(parent, text=subtitle, style="Tiny.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 10))

    def add_text_context_menu(widget, editable: bool = True) -> None:
        menu = tk.Menu(widget, tearoff=0)

        def has_selection() -> bool:
            try:
                if isinstance(widget, tk.Text):
                    widget.index("sel.first")
                    widget.index("sel.last")
                    return True
                return bool(widget.selection_present())
            except tk.TclError:
                return False

        def copy_selection() -> None:
            try:
                if isinstance(widget, tk.Text):
                    if widget.tag_ranges("sel"):
                        selected = widget.get("sel.first", "sel.last")
                    else:
                        selected = widget.get("1.0", "end-1c")
                else:
                    selected = widget.selection_get()
                if selected:
                    root.clipboard_clear()
                    root.clipboard_append(selected)
            except tk.TclError:
                pass

        def cut_selection() -> None:
            if not editable:
                return
            try:
                widget.event_generate("<<Cut>>")
            except tk.TclError:
                pass

        def paste_clipboard() -> None:
            if not editable:
                return
            try:
                widget.event_generate("<<Paste>>")
            except tk.TclError:
                pass

        def select_all() -> None:
            try:
                if isinstance(widget, tk.Text):
                    widget.tag_add("sel", "1.0", "end-1c")
                    widget.mark_set("insert", "1.0")
                    widget.see("insert")
                else:
                    widget.selection_range(0, "end")
                    widget.icursor("end")
            except tk.TclError:
                pass

        def show_menu(event) -> str:
            try:
                widget.focus_set()
            except tk.TclError:
                pass
            menu.delete(0, "end")
            if editable:
                menu.add_command(label="Cut", command=cut_selection, state="normal" if has_selection() else "disabled")
            menu.add_command(label="Copy", command=copy_selection, state="normal" if isinstance(widget, tk.Text) or has_selection() else "disabled")
            if editable:
                menu.add_command(label="Paste", command=paste_clipboard)
            menu.add_separator()
            menu.add_command(label="Select All", command=select_all)
            menu.tk_popup(event.x_root, event.y_root)
            return "break"

        widget.bind("<Button-3>", show_menu, add="+")
        widget.bind("<Shift-F10>", show_menu, add="+")
        widget.bind("<Control-a>", lambda _event: (select_all(), "break")[1], add="+")

    def labeled_entry(parent, row: int, label: str, variable: tk.Variable, browse_command=None) -> None:
        ttk.Label(parent, text=label, style="Muted.TLabel").grid(row=row, column=0, sticky="w", pady=6, padx=(0, 12))
        holder = ttk.Frame(parent, style="Row.TFrame")
        holder.grid(row=row, column=1, sticky="ew", pady=6)
        holder.columnconfigure(0, weight=1)
        entry = ttk.Entry(holder, textvariable=variable)
        entry.grid(row=0, column=0, sticky="ew")
        add_text_context_menu(entry)
        if browse_command:
            ttk.Button(holder, text="Browse", command=browse_command).grid(row=0, column=1, padx=(8, 0))

    shell = ttk.Frame(root, style="App.TFrame", padding=0)
    shell.pack(fill="both", expand=True)
    shell.columnconfigure(0, weight=1)
    shell.rowconfigure(2, weight=1)

    header = ttk.Frame(shell, style="Nav.TFrame", padding=(22, 18, 22, 16))
    header.grid(row=0, column=0, sticky="ew")
    text_column = 1 if header_logo is not None else 0
    header.columnconfigure(text_column, weight=1)
    if header_logo is not None:
        tk.Label(header, image=header_logo, bg=palette["hero"], bd=0).grid(
            row=0, column=0, rowspan=2, sticky="w", padx=(0, 16)
        )
    ttk.Label(header, text="Series Trailer Downloader", style="Header.TLabel").grid(row=0, column=text_column, sticky="w")
    ttk.Label(
        header,
        text="Search public trailer sources, preserve existing trailers, and keep one clean file per series.",
        style="Subheader.TLabel",
    ).grid(row=1, column=text_column, sticky="w", pady=(4, 0))

    status_badge = tk.Label(
        header,
        textvariable=status_var,
        bg=palette["good"],
        fg="#ecfeff",
        padx=14,
        pady=7,
        font=("Segoe UI", 10, "bold"),
    )
    status_badge.grid(row=0, column=text_column + 1, rowspan=2, sticky="e")

    summary = ttk.Frame(shell, style="App.TFrame")
    summary.grid(row=1, column=0, sticky="ew", padx=16, pady=(16, 14))
    summary.columnconfigure((0, 1, 2), weight=1)

    def summary_tile(column: int, label: str, value_var: tk.StringVar, accent: str) -> None:
        tile = ttk.Frame(summary, style="Panel.TFrame", padding=12)
        tile.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 8, 0 if column == 2 else 8))
        tile.columnconfigure(0, weight=1)
        marker = tk.Frame(tile, width=44, height=4, bg=accent, highlightthickness=0)
        marker.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(tile, textvariable=value_var, style="Value.TLabel").grid(row=1, column=0, sticky="w")
        ttk.Label(tile, text=label.upper(), style="Tiny.TLabel").grid(row=2, column=0, sticky="w", pady=(2, 0))

    summary_tile(0, "Library", root_summary_var, palette["accent"])
    summary_tile(1, "Cookies", cookie_summary_var, palette["good"])
    summary_tile(2, "Mode", mode_summary_var, palette["warn"])

    notebook = ttk.Notebook(shell)
    notebook.grid(row=2, column=0, sticky="nsew", padx=16, pady=(0, 16))

    run_tab = ttk.Frame(notebook, padding=12)
    settings_tab = ttk.Frame(notebook, padding=12)
    notebook.add(run_tab, text="Run")
    notebook.add(settings_tab, text="Settings")

    run_tab.columnconfigure(0, weight=1)
    run_tab.rowconfigure(2, weight=1)

    settings_tab.columnconfigure(0, weight=1)
    settings_tab.rowconfigure(0, weight=1)
    settings_canvas = tk.Canvas(
        settings_tab,
        bg=palette["bg"],
        highlightthickness=0,
        borderwidth=0,
        yscrollincrement=24,
    )
    settings_scrollbar = ttk.Scrollbar(settings_tab, orient="vertical", command=settings_canvas.yview)
    settings_canvas.configure(yscrollcommand=settings_scrollbar.set)
    settings_canvas.grid(row=0, column=0, sticky="nsew")
    settings_scrollbar.grid(row=0, column=1, sticky="ns")

    settings_body = ttk.Frame(settings_canvas, style="App.TFrame")
    settings_window = settings_canvas.create_window((0, 0), window=settings_body, anchor="nw")
    settings_body.columnconfigure((0, 1), weight=1, uniform="settings")
    settings_body.rowconfigure(1, weight=1)

    def refresh_settings_scroll_region(_event=None) -> None:
        settings_canvas.configure(scrollregion=settings_canvas.bbox("all"))

    def resize_settings_body(event) -> None:
        settings_canvas.itemconfigure(settings_window, width=event.width)

    def scroll_settings(event) -> str:
        if event.delta:
            settings_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        return "break"

    def bind_settings_wheel(_event) -> None:
        settings_canvas.bind_all("<MouseWheel>", scroll_settings)

    def unbind_settings_wheel(_event) -> None:
        settings_canvas.unbind_all("<MouseWheel>")

    settings_body.bind("<Configure>", refresh_settings_scroll_region)
    settings_canvas.bind("<Configure>", resize_settings_body)
    settings_canvas.bind("<Enter>", bind_settings_wheel)
    settings_canvas.bind("<Leave>", unbind_settings_wheel)

    def browse_root() -> None:
        selected = filedialog.askdirectory(initialdir=root_var.get() or r"C:\TV Shows")
        if selected:
            root_var.set(selected)
            root_summary_var.set(selected)

    library_panel = panel(run_tab, row=0, column=0, sticky="ew")
    library_panel.columnconfigure(1, weight=1)
    card_title(library_panel, "Library target", "Folder names should look like Series (2026).")
    labeled_entry(library_panel, 2, "Series folder", root_var, browse_root)
    ttk.Checkbutton(
        library_panel,
        text="Recursive scan for nested series folders",
        variable=recursive_var,
    ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))

    actions = ttk.Frame(run_tab, style="Panel.TFrame", padding=14)
    actions.grid(row=1, column=0, sticky="ew", pady=(12, 8))
    actions.columnconfigure(5, weight=1)
    ttk.Label(actions, text="Run controls", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=6, sticky="w", pady=(0, 10))

    log_panel = ttk.LabelFrame(run_tab, text="Download Log", style="Card.TLabelframe", padding=10)
    log_panel.grid(row=2, column=0, sticky="nsew")
    log_panel.columnconfigure(0, weight=1)
    log_panel.rowconfigure(0, weight=1)
    log_text = tk.Text(
        log_panel,
        wrap="word",
        height=12,
        state="disabled",
        bg="#111827",
        fg="#e5e7eb",
        insertbackground="#e5e7eb",
        selectbackground="#475569",
        selectforeground="#ffffff",
        relief="flat",
        bd=0,
        padx=10,
        pady=8,
        font=("Consolas", 9),
    )
    log_text.grid(row=0, column=0, sticky="nsew")
    add_text_context_menu(log_text, editable=False)
    scrollbar = ttk.Scrollbar(log_panel, orient="vertical", command=log_text.yview)
    scrollbar.grid(row=0, column=1, sticky="ns")
    log_text.configure(yscrollcommand=scrollbar.set)

    status = ttk.Label(run_tab, textvariable=status_var, style="Muted.TLabel", anchor="w")
    status.grid(row=3, column=0, sticky="ew", pady=(8, 0))

    def append_log(text: str) -> None:
        text = strip_ansi(text).replace("\r", "").strip()
        if not text:
            return
        log_text.configure(state="normal")
        for line in text.splitlines():
            log_text.insert(tk.END, f"{line}\n")
        log_text.configure(state="disabled")
        log_text.see(tk.END)

    def update_summary() -> None:
        root_summary_var.set(root_var.get().strip() or r"C:\TV Shows")
        cookie_path = Path(cookies_file_var.get().strip() or str(DEFAULT_COOKIES_PATH))
        if cookie_path.exists():
            cookie_summary_var.set(f"file: {cookie_path.name}")
        elif cookies_var.get().strip():
            cookie_summary_var.set(f"browser: {cookies_var.get().strip()}")
        else:
            cookie_summary_var.set("public search")
        mode_summary_var.set("total re-download" if redownload_existing_var.get() else "skip existing trailers")

    def set_status(text: str, tone: str = "accent") -> None:
        status_var.set(text)
        status_badge.configure(bg=palette.get(tone, palette["accent_dark"]))

    def parse_int_field(value: str, field_name: str, minimum: int, maximum: int) -> int:
        text = value.strip()
        if not text:
            raise ValueError(f"{field_name} cannot be blank.")
        try:
            number = int(text)
        except ValueError:
            raise ValueError(f"{field_name} must be a whole number.") from None
        if number < minimum or number > maximum:
            raise ValueError(f"{field_name} must be between {minimum} and {maximum}.")
        return number

    def parse_float_field(value: str, field_name: str, minimum: float, maximum: float) -> float:
        text = value.strip()
        if not text:
            raise ValueError(f"{field_name} cannot be blank.")
        try:
            number = float(text)
        except ValueError:
            raise ValueError(f"{field_name} must be a number.") from None
        if number < minimum or number > maximum:
            raise ValueError(f"{field_name} must be between {minimum:g} and {maximum:g}.")
        return number

    def read_settings_from_gui() -> dict:
        limit_text = limit_var.get().strip()
        limit = parse_int_field(limit_text, "Process first N folders", 1, 1_000_000) if limit_text else None
        download_sleep_min = parse_float_field(download_sleep_min_var.get(), "Download sleep minimum", 0, 120)
        download_sleep_max = parse_float_field(download_sleep_max_var.get(), "Download sleep maximum", 0, 300)
        if download_sleep_max < download_sleep_min:
            raise ValueError("Download sleep maximum must be greater than or equal to the minimum.")
        ffmpeg_preset = ffmpeg_preset_var.get().strip() or DEFAULT_FFMPEG_PRESET
        if ffmpeg_preset not in FFMPEG_PRESETS:
            raise ValueError(f"FFmpeg preset must be one of: {', '.join(FFMPEG_PRESETS)}.")
        ffmpeg_encoder = ffmpeg_encoder_var.get().strip().lower() or DEFAULT_FFMPEG_ENCODER
        if ffmpeg_encoder not in FFMPEG_ENCODERS:
            raise ValueError(f"FFmpeg encoder must be one of: {', '.join(FFMPEG_ENCODERS)}.")
        return {
            "root": root_var.get().strip() or r"C:\TV Shows",
            "recursive": bool(recursive_var.get()),
            "max_per_series": 1,
            "source_order": source_order_var.get().strip() or DEFAULT_SOURCE_ORDER,
            "tmdb_token": tmdb_token_var.get().strip(),
            "kinocheck_token": kinocheck_token_var.get().strip(),
            "youtube_api_key": youtube_api_key_var.get().strip(),
            "search_results": parse_int_field(search_results_var.get(), "Search results per query", 1, 50),
            "max_duration": parse_int_field(max_duration_var.get(), "Maximum trailer length", 30, 3600),
            "max_height": parse_int_field(max_height_var.get(), "Maximum video quality", 0, 4320),
            "candidate_attempts": parse_int_field(candidate_attempts_var.get(), "Candidate attempts", 1, 25),
            "search_delay": parse_float_field(search_delay_var.get(), "Search delay", 0, 120),
            "series_delay": parse_float_field(series_delay_var.get(), "Series delay", 0, 300),
            "download_sleep_min": download_sleep_min,
            "download_sleep_max": download_sleep_max,
            "ffmpeg_threads": parse_int_field(ffmpeg_threads_var.get(), "FFmpeg threads", 1, 16),
            "ffmpeg_preset": ffmpeg_preset,
            "ffmpeg_crf": parse_int_field(ffmpeg_crf_var.get(), "FFmpeg CRF", 16, 30),
            "ffmpeg_encoder": ffmpeg_encoder,
            "js_runtime": js_runtime_var.get().strip(),
            "remote_components": remote_components_var.get().strip(),
            "include_network_queries": bool(include_network_queries_var.get()),
            "cookies_file": cookies_file_var.get().strip() or str(DEFAULT_COOKIES_PATH),
            "results_file": results_file_var.get().strip() or str(DEFAULT_RESULTS_PATH),
            "log_file": log_file_var.get().strip(),
            "cookies_from_browser": cookies_var.get().strip() or None,
            "limit": limit,
            "redownload_existing": bool(redownload_existing_var.get()),
            "ignore_success_history": not bool(skip_success_history_var.get()),
            "keep_temp": bool(keep_temp_var.get()),
        }

    def persist_current_settings() -> None:
        current = read_settings_from_gui()
        save_gui_settings(
            {
                "root": current["root"],
                "recursive": current["recursive"],
                "include_network_queries": current["include_network_queries"],
                "cookies_file": current["cookies_file"],
                "results_file": current["results_file"],
                "log_file": current["log_file"],
                "extract_cookies_from_browser": extract_browser_var.get().strip() or "edge",
                "cookies_from_browser": current["cookies_from_browser"] or "",
                "source_order": current["source_order"],
                "tmdb_token": current["tmdb_token"],
                "kinocheck_token": current["kinocheck_token"],
                "youtube_api_key": current["youtube_api_key"],
                "search_results": current["search_results"],
                "max_duration": current["max_duration"],
                "max_height": current["max_height"],
                "candidate_attempts": current["candidate_attempts"],
                "search_delay": current["search_delay"],
                "series_delay": current["series_delay"],
                "download_sleep_min": current["download_sleep_min"],
                "download_sleep_max": current["download_sleep_max"],
                "ffmpeg_threads": current["ffmpeg_threads"],
                "ffmpeg_preset": current["ffmpeg_preset"],
                "ffmpeg_crf": current["ffmpeg_crf"],
                "ffmpeg_encoder": current["ffmpeg_encoder"],
                "js_runtime": current["js_runtime"],
                "remote_components": current["remote_components"],
                "limit": "" if current["limit"] is None else str(current["limit"]),
                "redownload_existing": current["redownload_existing"],
                "skip_success_history": not current["ignore_success_history"],
                "keep_temp": current["keep_temp"],
            }
        )
        update_summary()

    def drain_log_queue() -> None:
        while True:
            try:
                item = log_queue.get_nowait()
            except queue.Empty:
                break
            done_state = None
            if item == "__DONE__":
                done_state = "finished"
            elif isinstance(item, tuple) and len(item) == 2 and item[0] == "__DONE__":
                done_state = str(item[1])

            if done_state:
                running_var.set(False)
                for button in busy_buttons:
                    button.configure(state="normal")
                if done_state == "cancelled":
                    progress_text_var.set("Cancelled")
                    set_status("Cancelled", "warn")
                else:
                    progress_var.set(100)
                    set_status("Finished", "good")
                update_summary()
            elif isinstance(item, tuple) and len(item) == 4 and item[0] == "__PROGRESS__":
                _kind, current, total, label = item
                percent = 0 if not total else (float(current) / float(total)) * 100
                progress_var.set(percent)
                progress_text_var.set(f"{current} / {total} - {label}")
            else:
                append_log(item)
        root.after(100, drain_log_queue)

    def run_worker(dry_run_override: bool | None = None) -> None:
        if running_var.get():
            return
        try:
            current = read_settings_from_gui()
            if dry_run_override is not None:
                current["dry_run"] = dry_run_override
            else:
                current["dry_run"] = False
            persist_current_settings()
            current["progress_callback"] = lambda current_count, total_count, label: log_queue.put(
                ("__PROGRESS__", current_count, total_count, label)
            )
        except Exception as exc:
            messagebox.showerror("Invalid settings", str(exc))
            return

        log_text.configure(state="normal")
        log_text.delete("1.0", "end")
        log_text.configure(state="disabled")
        progress_var.set(0)
        progress_text_var.set("Preparing")
        set_status("Running", "accent_dark")
        cancel_event.clear()
        current["cancel_event"] = cancel_event
        running_var.set(True)
        for button in busy_buttons:
            button.configure(state="disabled")

        def target() -> None:
            old_stdout, old_stderr = sys.stdout, sys.stderr
            writer = QueueWriter(log_queue)
            sys.stdout = writer
            sys.stderr = writer
            done_state = "finished"
            try:
                args = argparse.Namespace(**current)
                exit_code = run_cli(args)
                if exit_code == 130:
                    done_state = "cancelled"
            except SystemExit as exc:
                print(f"\nStopped with exit code {exc.code}")
                if exc.code == 130:
                    done_state = "cancelled"
            except CancelledByUser:
                print("\nRun cancelled by user")
                done_state = "cancelled"
            except Exception as exc:
                print(f"\nError: {exc}")
            finally:
                sys.stdout = old_stdout
                sys.stderr = old_stderr
                log_queue.put(("__DONE__", done_state))

        threading.Thread(target=target, daemon=True).start()

    def extract_worker() -> None:
        if running_var.get():
            return
        browser = extract_browser_var.get().strip()
        if not browser:
            messagebox.showerror("Missing browser", "Choose a browser to extract cookies from.")
            return
        try:
            persist_current_settings()
            cookies_file = Path(cookies_file_var.get().strip() or str(DEFAULT_COOKIES_PATH))
        except Exception as exc:
            messagebox.showerror("Invalid settings", str(exc))
            return

        notebook.select(run_tab)
        log_text.configure(state="normal")
        log_text.delete("1.0", "end")
        log_text.configure(state="disabled")
        progress_var.set(0)
        progress_text_var.set("Extracting cookies")
        set_status("Extracting cookies", "warn")
        running_var.set(True)
        for button in busy_buttons:
            button.configure(state="disabled")

        def target() -> None:
            old_stdout, old_stderr = sys.stdout, sys.stderr
            writer = QueueWriter(log_queue)
            sys.stdout = writer
            sys.stderr = writer
            try:
                extract_cookies_file(browser, cookies_file)
            except Exception as exc:
                print(f"\nCookie extraction failed: {exc}")
                print("Try closing the browser first, or choose a different browser/profile.")
            finally:
                sys.stdout = old_stdout
                sys.stderr = old_stderr
                log_queue.put("__DONE__")

        threading.Thread(target=target, daemon=True).start()


    def dependency_worker() -> None:
        if running_var.get():
            return
        missing = missing_dependency_names()
        status_lines = [f"{'OK' if ok else 'MISSING'} - {name}: {detail}" for name, ok, detail in dependency_status()]
        if not missing:
            messagebox.showinfo("Dependencies", "All required dependencies look ready.\n\n" + "\n".join(status_lines))
            return
        if not INSTALLER_PATH.exists():
            messagebox.showerror("Installer missing", f"Could not find {INSTALLER_PATH}")
            return
        answer = messagebox.askyesno(
            "Install dependencies",
            "Missing dependencies were found:\n\n"
            + "\n".join(status_lines)
            + "\n\nRun install.ps1 now? Windows may ask for administrator approval.",
        )
        if not answer:
            return

        notebook.select(run_tab)
        log_text.configure(state="normal")
        log_text.delete("1.0", "end")
        log_text.configure(state="disabled")
        progress_var.set(0)
        progress_text_var.set("Installing dependencies")
        set_status("Installing dependencies", "warn")
        running_var.set(True)
        for button in busy_buttons:
            button.configure(state="disabled")

        def target() -> None:
            old_stdout, old_stderr = sys.stdout, sys.stderr
            writer = QueueWriter(log_queue)
            sys.stdout = writer
            sys.stderr = writer
            try:
                print_dependency_status()
                print("\nStarting dependency installer...")
                exit_code = install_dependencies()
                print(f"\nInstaller finished with exit code {exit_code}")
                print("\nUpdated dependency status:")
                print_dependency_status()
            except Exception as exc:
                print(f"\nDependency install failed: {exc}")
            finally:
                sys.stdout = old_stdout
                sys.stderr = old_stderr
                log_queue.put("__DONE__")

        threading.Thread(target=target, daemon=True).start()

    def cancel_task() -> None:
        if running_var.get():
            cancel_event.set()
            set_status("Cancelling", "warn")
            progress_text_var.set("Cancelling current download")
            log_queue.put("\nCancel requested; stopping active download/conversion...\n")
        else:
            progress_text_var.set("No active task to cancel")

    def exit_app() -> None:
        if running_var.get():
            answer = messagebox.askyesno(
                "Task running",
                "A task is currently running. Cancel it first and keep the app open?",
            )
            if answer:
                cancel_task()
            return
        root.destroy()

    preview_button = ttk.Button(actions, text="Preview", command=lambda: run_worker(True))
    preview_button.grid(row=1, column=0, sticky="w")
    start_button = ttk.Button(actions, text="Download", style="Accent.TButton", command=lambda: run_worker(False))
    start_button.grid(row=1, column=1, sticky="w", padx=(8, 0))
    deps_button = ttk.Button(actions, text="Install / Repair Dependencies", command=dependency_worker)
    deps_button.grid(row=1, column=2, sticky="w", padx=(8, 0))
    cancel_button = ttk.Button(actions, text="Cancel Task", style="Danger.TButton", command=cancel_task)
    cancel_button.grid(row=1, column=3, sticky="w", padx=(8, 0))
    exit_button = ttk.Button(actions, text="Exit", command=exit_app)
    exit_button.grid(row=1, column=4, sticky="w", padx=(8, 0))
    busy_buttons.extend([preview_button, start_button, deps_button])
    ttk.Label(actions, text="Default skips existing trailers; total re-download is in Settings.", style="Muted.TLabel").grid(
        row=1, column=5, sticky="e"
    )
    progress_bar = ttk.Progressbar(actions, variable=progress_var, mode="determinate", maximum=100)
    progress_bar.grid(row=2, column=0, columnspan=6, sticky="ew", pady=(14, 4))
    ttk.Label(actions, textvariable=progress_text_var, style="Muted.TLabel").grid(
        row=3, column=0, columnspan=6, sticky="ew"
    )

    search_panel = panel(settings_body, row=0, column=0, sticky="nsew", padx=(0, 8), pady=(0, 12))
    search_panel.columnconfigure(1, weight=1)
    card_title(search_panel, "Search profile", "Tune source breadth and candidate filtering.")
    ttk.Checkbutton(search_panel, text="Include network/platform searches", variable=include_network_queries_var, command=update_summary).grid(
        row=2, column=0, columnspan=2, sticky="w", pady=(2, 8)
    )
    ttk.Label(search_panel, text="Search results per query", style="Muted.TLabel").grid(row=3, column=0, sticky="w", pady=6)
    search_results_entry = ttk.Entry(search_panel, textvariable=search_results_var)
    search_results_entry.grid(row=3, column=1, sticky="ew", pady=6)
    add_text_context_menu(search_results_entry)
    ttk.Label(search_panel, text="Maximum trailer length", style="Muted.TLabel").grid(row=4, column=0, sticky="w", pady=6)
    max_duration_entry = ttk.Entry(search_panel, textvariable=max_duration_var)
    max_duration_entry.grid(row=4, column=1, sticky="ew", pady=6)
    add_text_context_menu(max_duration_entry)
    ttk.Label(search_panel, text="Maximum video quality", style="Muted.TLabel").grid(row=5, column=0, sticky="w", pady=6)
    max_height_combo = ttk.Combobox(
        search_panel,
        textvariable=max_height_var,
        values=("0", "2160", "1440", "1080", "720", "480"),
        state="readonly",
    )
    max_height_combo.grid(row=5, column=1, sticky="ew", pady=6)
    add_text_context_menu(max_height_combo)
    ttk.Label(search_panel, text="Candidate attempts", style="Muted.TLabel").grid(row=6, column=0, sticky="w", pady=6)
    candidate_attempts_entry = ttk.Entry(search_panel, textvariable=candidate_attempts_var)
    candidate_attempts_entry.grid(row=6, column=1, sticky="ew", pady=6)
    add_text_context_menu(candidate_attempts_entry)
    ttk.Label(search_panel, text="Delay between searches (seconds)", style="Muted.TLabel").grid(row=7, column=0, sticky="w", pady=6)
    search_delay_entry = ttk.Entry(search_panel, textvariable=search_delay_var)
    search_delay_entry.grid(row=7, column=1, sticky="ew", pady=6)
    add_text_context_menu(search_delay_entry)
    ttk.Label(search_panel, text="Download sleep min (seconds)", style="Muted.TLabel").grid(row=8, column=0, sticky="w", pady=6)
    download_sleep_min_entry = ttk.Entry(search_panel, textvariable=download_sleep_min_var)
    download_sleep_min_entry.grid(row=8, column=1, sticky="ew", pady=6)
    add_text_context_menu(download_sleep_min_entry)
    ttk.Label(search_panel, text="Download sleep max (seconds)", style="Muted.TLabel").grid(row=9, column=0, sticky="w", pady=6)
    download_sleep_max_entry = ttk.Entry(search_panel, textvariable=download_sleep_max_var)
    download_sleep_max_entry.grid(row=9, column=1, sticky="ew", pady=6)
    add_text_context_menu(download_sleep_max_entry)
    ttk.Label(search_panel, text="Source order", style="Muted.TLabel").grid(row=10, column=0, sticky="w", pady=6)
    source_order_entry = ttk.Entry(search_panel, textvariable=source_order_var)
    source_order_entry.grid(row=10, column=1, sticky="ew", pady=6)
    add_text_context_menu(source_order_entry)
    ttk.Label(search_panel, text="TMDb token / key", style="Muted.TLabel").grid(row=11, column=0, sticky="w", pady=6)
    tmdb_token_entry = ttk.Entry(search_panel, textvariable=tmdb_token_var, show="*")
    tmdb_token_entry.grid(row=11, column=1, sticky="ew", pady=6)
    add_text_context_menu(tmdb_token_entry)
    ttk.Label(search_panel, text="KinoCheck API key", style="Muted.TLabel").grid(row=12, column=0, sticky="w", pady=6)
    kinocheck_token_entry = ttk.Entry(search_panel, textvariable=kinocheck_token_var, show="*")
    kinocheck_token_entry.grid(row=12, column=1, sticky="ew", pady=6)
    add_text_context_menu(kinocheck_token_entry)
    ttk.Label(search_panel, text="YouTube API key", style="Muted.TLabel").grid(row=13, column=0, sticky="w", pady=6)
    youtube_api_key_entry = ttk.Entry(search_panel, textvariable=youtube_api_key_var, show="*")
    youtube_api_key_entry.grid(row=13, column=1, sticky="ew", pady=6)
    add_text_context_menu(youtube_api_key_entry)

    runtime_panel = panel(settings_body, row=1, column=0, sticky="nsew", padx=(0, 8))
    runtime_panel.columnconfigure(1, weight=1)
    card_title(runtime_panel, "Runtime", "Keep test runs short while tuning searches.")
    ttk.Checkbutton(runtime_panel, text="Keep temporary folders", variable=keep_temp_var).grid(
        row=2, column=0, columnspan=2, sticky="w", pady=(2, 8)
    )
    ttk.Checkbutton(
        runtime_panel,
        text="Use saved success history",
        variable=skip_success_history_var,
        command=update_summary,
    ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(2, 8))
    ttk.Checkbutton(
        runtime_panel,
        text="Total re-download existing trailers",
        variable=redownload_existing_var,
        command=update_summary,
    ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(2, 8))
    ttk.Label(runtime_panel, text="Process first N folders", style="Muted.TLabel").grid(row=5, column=0, sticky="w", pady=6)
    limit_entry = ttk.Entry(runtime_panel, textvariable=limit_var)
    limit_entry.grid(row=5, column=1, sticky="ew", pady=6)
    add_text_context_menu(limit_entry)
    ttk.Label(runtime_panel, text="Delay between series (seconds)", style="Muted.TLabel").grid(row=6, column=0, sticky="w", pady=6)
    series_delay_entry = ttk.Entry(runtime_panel, textvariable=series_delay_var)
    series_delay_entry.grid(row=6, column=1, sticky="ew", pady=6)
    add_text_context_menu(series_delay_entry)
    ttk.Label(runtime_panel, text="FFmpeg threads", style="Muted.TLabel").grid(row=7, column=0, sticky="w", pady=6)
    ffmpeg_threads_entry = ttk.Entry(runtime_panel, textvariable=ffmpeg_threads_var)
    ffmpeg_threads_entry.grid(row=7, column=1, sticky="ew", pady=6)
    add_text_context_menu(ffmpeg_threads_entry)
    ttk.Label(runtime_panel, text="FFmpeg preset", style="Muted.TLabel").grid(row=8, column=0, sticky="w", pady=6)
    ffmpeg_preset_combo = ttk.Combobox(runtime_panel, textvariable=ffmpeg_preset_var, values=FFMPEG_PRESETS)
    ffmpeg_preset_combo.grid(row=8, column=1, sticky="ew", pady=6)
    add_text_context_menu(ffmpeg_preset_combo)
    ttk.Label(runtime_panel, text="FFmpeg encoder", style="Muted.TLabel").grid(row=9, column=0, sticky="w", pady=6)
    ffmpeg_encoder_combo = ttk.Combobox(runtime_panel, textvariable=ffmpeg_encoder_var, values=FFMPEG_ENCODERS, state="readonly")
    ffmpeg_encoder_combo.grid(row=9, column=1, sticky="ew", pady=6)
    add_text_context_menu(ffmpeg_encoder_combo)
    ttk.Label(runtime_panel, text="FFmpeg quality", style="Muted.TLabel").grid(row=10, column=0, sticky="w", pady=6)
    ffmpeg_crf_entry = ttk.Entry(runtime_panel, textvariable=ffmpeg_crf_var)
    ffmpeg_crf_entry.grid(row=10, column=1, sticky="ew", pady=6)
    add_text_context_menu(ffmpeg_crf_entry)

    def browse_results_file() -> None:
        selected = filedialog.asksaveasfilename(
            initialfile=Path(results_file_var.get() or str(DEFAULT_RESULTS_PATH)).name,
            initialdir=str(Path(results_file_var.get() or str(DEFAULT_RESULTS_PATH)).parent),
            defaultextension=".json",
            filetypes=(("JSON files", "*.json"), ("All files", "*.*")),
        )
        if selected:
            results_file_var.set(selected)

    labeled_entry(runtime_panel, 11, "Results file", results_file_var, browse_results_file)

    def browse_log_file() -> None:
        selected = filedialog.asksaveasfilename(
            initialfile=Path(log_file_var.get() or str(DEFAULT_LOG_PATH)).name,
            initialdir=str(Path(log_file_var.get() or str(DEFAULT_LOG_PATH)).parent),
            defaultextension=".log",
            filetypes=(("Log files", "*.log"), ("All files", "*.*")),
        )
        if selected:
            log_file_var.set(selected)

    labeled_entry(runtime_panel, 12, "Progress log", log_file_var, browse_log_file)

    cookies_panel = panel(settings_body, row=0, column=1, rowspan=2, sticky="nsew", padx=(8, 0))
    cookies_panel.columnconfigure(1, weight=1)
    card_title(cookies_panel, "Cookies", "Prefer a generated cookies.txt file over live browser extraction.")

    def browse_cookies_file() -> None:
        selected = filedialog.asksaveasfilename(
            initialfile=Path(cookies_file_var.get() or str(DEFAULT_COOKIES_PATH)).name,
            initialdir=str(Path(cookies_file_var.get() or str(DEFAULT_COOKIES_PATH)).parent),
            defaultextension=".txt",
            filetypes=(("Cookies files", "*.txt"), ("All files", "*.*")),
        )
        if selected:
            cookies_file_var.set(selected)
            update_summary()

    labeled_entry(cookies_panel, 2, "Cookies file", cookies_file_var, browse_cookies_file)

    ttk.Label(cookies_panel, text="Extract from", style="Muted.TLabel").grid(row=3, column=0, sticky="w", pady=6, padx=(0, 12))
    extract_row = ttk.Frame(cookies_panel, style="Row.TFrame")
    extract_row.grid(row=3, column=1, sticky="ew", pady=6)
    extract_row.columnconfigure(0, weight=1)
    extract_combo = ttk.Combobox(
        extract_row,
        textvariable=extract_browser_var,
        values=("edge", "chrome", "chromium", "firefox", "brave", "vivaldi", "opera"),
    )
    extract_combo.grid(row=0, column=0, sticky="ew", padx=(0, 8))
    add_text_context_menu(extract_combo)
    extract_button = ttk.Button(extract_row, text="Extract Cookies", command=extract_worker)
    extract_button.grid(row=0, column=1)
    busy_buttons.append(extract_button)

    ttk.Label(cookies_panel, text="Direct fallback", style="Muted.TLabel").grid(row=4, column=0, sticky="w", pady=6)
    cookies_combo = ttk.Combobox(cookies_panel, textvariable=cookies_var, values=("", "edge", "chrome", "firefox"))
    cookies_combo.grid(row=4, column=1, sticky="ew", pady=6)
    add_text_context_menu(cookies_combo)
    cookies_combo.bind("<<ComboboxSelected>>", lambda _event: update_summary())

    ttk.Label(cookies_panel, text="JS runtime", style="Muted.TLabel").grid(row=5, column=0, sticky="w", pady=6)
    js_runtime_combo = ttk.Combobox(cookies_panel, textvariable=js_runtime_var, values=("", "node", "deno", "quickjs"))
    js_runtime_combo.grid(row=5, column=1, sticky="ew", pady=6)
    add_text_context_menu(js_runtime_combo)

    ttk.Label(cookies_panel, text="Remote EJS components", style="Muted.TLabel").grid(row=6, column=0, sticky="w", pady=6)
    remote_components_entry = ttk.Entry(cookies_panel, textvariable=remote_components_var)
    remote_components_entry.grid(row=6, column=1, sticky="ew", pady=6)
    add_text_context_menu(remote_components_entry)

    ttk.Label(cookies_panel, text="Settings file", style="Muted.TLabel").grid(row=7, column=0, sticky="w", pady=(18, 4))
    ttk.Label(cookies_panel, text=str(SETTINGS_PATH), style="Tiny.TLabel", wraplength=430).grid(
        row=7, column=1, sticky="w", pady=(18, 4)
    )

    ttk.Button(cookies_panel, text="Save Settings", command=persist_current_settings).grid(
        row=8, column=1, sticky="e", pady=(14, 0)
    )

    update_summary()
    drain_log_queue()
    root.mainloop()
    return 0


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    supplied = supplied_cli_dests(parser, sys.argv[1:])
    if args.check_deps:
        print_dependency_status()
        return 1 if missing_dependency_names() else 0
    if args.install_deps:
        return install_dependencies()

    if args.gui or len(sys.argv) == 1:
        return launch_gui()
    if not apply_settings_file(args, parser, supplied):
        return 1
    return run_cli(args)


if __name__ == "__main__":
    raise SystemExit(main())
