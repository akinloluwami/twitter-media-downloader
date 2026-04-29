#!/usr/bin/env python3
"""Download media from a Twitter/X profile at the highest available quality."""

from __future__ import annotations

import argparse
import json
import tempfile
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Iterable
from urllib.parse import urlparse
from urllib.request import Request, urlopen


HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
DEFAULT_OUTPUT_ROOT = Path("downloads")
DEFAULT_VENV_DIR = Path(".venv")
RESERVED_PATHS = {
    "account",
    "compose",
    "download",
    "explore",
    "hashtag",
    "home",
    "i",
    "intent",
    "jobs",
    "login",
    "messages",
    "notifications",
    "privacy",
    "search",
    "settings",
    "share",
    "signup",
    "tos",
}
SUPPORTED_HOSTS = {
    "mobile.twitter.com",
    "mobile.x.com",
    "twitter.com",
    "www.twitter.com",
    "www.x.com",
    "x.com",
}
AUTO_COOKIE_BROWSERS = (
    "firefox",
    "chrome",
    "brave",
    "edge",
    "chromium",
    "safari",
    "vivaldi",
    "opera",
)
AUTH_REQUIRED_MARKERS = (
    "AuthRequired",
    "authenticated cookies needed to access this timeline",
)
COOKIE_DB_MISSING_MARKERS = (
    "unable to find",
    "cookies database",
)
SUMMARY_FILENAME = "download-summary.json"
IMAGES_SUBDIR = "images"
VIDEOS_SUBDIR = "videos"
DOWNLOAD_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)
IMAGE_EXTENSIONS = {
    ".avif",
    ".bmp",
    ".gif",
    ".heic",
    ".heif",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}
VIDEO_EXTENSIONS = {
    ".avi",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".ts",
    ".webm",
}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS


def is_windows() -> bool:
    return os.name == "nt"


def venv_bin_dir(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts" if is_windows() else "bin")


def venv_executable_name(name: str) -> str:
    return f"{name}.exe" if is_windows() else name


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download all media from a Twitter/X profile using gallery-dl and yt-dlp. "
            "The script auto-installs the tools into a local .venv when needed."
        )
    )
    parser.add_argument(
        "profile",
        help="Twitter/X profile URL, @handle, or plain handle",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help="Base directory for downloads (default: %(default)s)",
    )
    parser.add_argument(
        "--archive-file",
        type=Path,
        help="Path to gallery-dl's download archive file",
    )
    cookie_group = parser.add_mutually_exclusive_group()
    cookie_group.add_argument(
        "--cookies-file",
        type=Path,
        help="Netscape-format cookies file to pass to gallery-dl",
    )
    cookie_group.add_argument(
        "--cookies-browser",
        help=(
            "Pass-through for gallery-dl --cookies-from-browser, "
            'for example: "firefox" or "chrome:Default"'
        ),
    )
    parser.add_argument(
        "--venv-dir",
        type=Path,
        default=DEFAULT_VENV_DIR,
        help="Local virtualenv used for gallery-dl/yt-dlp if bootstrapping is needed",
    )
    parser.add_argument(
        "--organize-media",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Place media into images/ and videos/ subfolders (default: %(default)s)",
    )
    parser.add_argument(
        "--skip-bootstrap",
        action="store_true",
        help="Fail instead of auto-installing gallery-dl and yt-dlp",
    )
    parser.add_argument(
        "--include-retweets",
        action="store_true",
        help="Also fetch media from retweets",
    )
    parser.add_argument(
        "--include-quoted",
        action="store_true",
        help="Also fetch media from quoted tweets",
    )
    parser.add_argument(
        "--exclude-pinned",
        action="store_true",
        help="Skip media from pinned tweets",
    )
    parser.add_argument(
        "--write-info-json",
        action="store_true",
        help="Write gallery-dl metadata sidecars next to downloaded media",
    )
    parser.add_argument(
        "--verify-auth",
        action="store_true",
        help=(
            "Preflight-check cookies before the real download. "
            "Useful for debugging cookie issues, but slower."
        ),
    )
    parser.add_argument(
        "--concurrency",
        type=positive_int,
        default=3,
        help="Number of media files to download at once (default: %(default)s)",
    )
    parser.add_argument(
        "--retries",
        type=positive_int,
        default=3,
        help="Attempts per media file before marking it failed (default: %(default)s)",
    )
    parser.add_argument(
        "--timeout",
        type=positive_int,
        default=60,
        help="Network timeout in seconds for media downloads (default: %(default)s)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the gallery-dl command that would run and exit",
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def normalize_profile(value: str) -> tuple[str, str]:
    candidate = value.strip()
    if not candidate:
        raise ValueError("Profile URL or handle cannot be empty.")

    if candidate.startswith("@"):
        handle = candidate[1:]
    elif HANDLE_RE.fullmatch(candidate):
        handle = candidate
    else:
        if "://" not in candidate:
            candidate = f"https://{candidate}"
        parsed = urlparse(candidate)
        host = parsed.netloc.lower()
        if host not in SUPPORTED_HOSTS:
            raise ValueError(
                f"Unsupported host '{parsed.netloc}'. Use a twitter.com or x.com profile URL."
            )
        segments = [segment for segment in parsed.path.split("/") if segment]
        if not segments:
            raise ValueError("The supplied URL does not look like a profile URL.")
        handle = segments[0].lstrip("@")

    if handle.lower() in RESERVED_PATHS or not HANDLE_RE.fullmatch(handle):
        raise ValueError(
            "Could not extract a valid Twitter/X handle. "
            "Expected a profile URL or handle like @example_user."
        )

    return handle, f"https://x.com/{handle}/media"


def ensure_toolchain(venv_dir: Path, skip_bootstrap: bool) -> tuple[Path, Path]:
    global_gallery = shutil.which("gallery-dl")
    global_ytdlp = shutil.which("yt-dlp")
    if global_gallery and global_ytdlp:
        return Path(global_gallery), Path(global_ytdlp)

    venv_dir = venv_dir.expanduser().resolve()
    venv_bin = venv_bin_dir(venv_dir)
    gallery_bin = venv_bin / venv_executable_name("gallery-dl")
    ytdlp_bin = venv_bin / venv_executable_name("yt-dlp")

    if gallery_bin.exists() and ytdlp_bin.exists():
        return gallery_bin, ytdlp_bin

    if skip_bootstrap:
        raise RuntimeError(
            "gallery-dl and yt-dlp were not both found. "
            "Install them first or rerun without --skip-bootstrap."
        )

    bootstrap_toolchain(venv_dir)
    if not gallery_bin.exists() or not ytdlp_bin.exists():
        raise RuntimeError("Bootstrapping finished, but gallery-dl or yt-dlp is still missing.")

    return gallery_bin, ytdlp_bin


def candidate_python_executables() -> list[Path]:
    candidates = [
        os.environ.get("PYTHON_FOR_VENV"),
        getattr(sys, "_base_executable", None),
        sys.executable,
        shutil.which("python3"),
        shutil.which("python"),
    ]
    if not is_windows():
        candidates.insert(1, "/usr/bin/python3")
    resolved: list[Path] = []
    seen: set[str] = set()

    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        resolved.append(path)

    return resolved


def can_create_virtualenv(python_executable: Path) -> bool:
    try:
        with tempfile.TemporaryDirectory(prefix="xmd-venv-probe-") as temp_dir:
            probe_dir = Path(temp_dir) / "probe"
            result = subprocess.run(
                [str(python_executable), "-m", "venv", str(probe_dir)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
    except OSError:
        return False

    return result.returncode == 0


def resolve_bootstrap_python() -> Path:
    for candidate in candidate_python_executables():
        if can_create_virtualenv(candidate):
            return candidate

    raise RuntimeError(
        "Could not find a usable Python interpreter for virtualenv bootstrap. "
        "Install python3 or set PYTHON_FOR_VENV to a Python 3 executable."
    )


def bootstrap_toolchain(venv_dir: Path) -> None:
    print(f"[bootstrap] Preparing local virtualenv at {venv_dir}", file=sys.stderr)
    bootstrap_python = resolve_bootstrap_python()
    if venv_dir.exists():
        print(f"[bootstrap] Rebuilding virtualenv with {bootstrap_python}", file=sys.stderr)
        shutil.rmtree(venv_dir)
    run_checked([str(bootstrap_python), "-m", "venv", str(venv_dir)])

    venv_python = venv_bin_dir(venv_dir) / venv_executable_name("python")
    if not venv_python.exists():
        raise RuntimeError(f"Virtualenv python not found at {venv_python}")

    run_checked([str(venv_python), "-m", "pip", "install", "--upgrade", "pip"])
    run_checked([str(venv_python), "-m", "pip", "install", "--upgrade", "gallery-dl", "yt-dlp"])


def build_gallery_dl_command(
    gallery_bin: Path,
    target_url: str,
    target_dir: Path,
    archive_file: Path,
    args: argparse.Namespace,
    browser_override: str | None = None,
) -> list[str]:
    command = [
        str(gallery_bin),
        "-D",
        str(target_dir),
        "--download-archive",
        str(archive_file),
        "-o",
        "extractor.twitter.timeline.strategy=media",
        "-o",
        f"extractor.twitter.pinned={str(not args.exclude_pinned).lower()}",
        "-o",
        f"extractor.twitter.quoted={str(args.include_quoted).lower()}",
        "-o",
        f"extractor.twitter.retweets={'original' if args.include_retweets else 'false'}",
        "-o",
        "extractor.twitter.videos=ytdl",
        "-o",
        "extractor.twitter.text-tweets=false",
        "-o",
        "extractor.twitter.ratelimit=wait",
    ]

    if args.cookies_file:
        command.extend(["-C", str(args.cookies_file.expanduser().resolve())])
    elif browser_override or args.cookies_browser:
        command.extend(["--cookies-from-browser", browser_override or args.cookies_browser])

    if args.write_info_json:
        command.append("--write-info-json")

    command.append(target_url)
    return command


def quoted_command(parts: Iterable[str]) -> str:
    return " ".join(shlex.quote(part) for part in parts)


def format_bytes(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{num_bytes} B"


def format_duration(seconds: float) -> str:
    total_seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def archive_key_from_metadata(metadata: dict[str, object]) -> str:
    return (
        f"twitter{metadata['tweet_id']}_"
        f"{metadata.get('retweet_id', 0)}_"
        f"{metadata['num']}"
    )


def ensure_unique_path(path: Path) -> Path:
    if not path.exists():
        return path

    counter = 2
    while True:
        candidate = path.with_name(f"{path.stem}-{counter}{path.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def media_subdir_for_extension(extension: str) -> str | None:
    if extension in IMAGE_EXTENSIONS:
        return IMAGES_SUBDIR
    if extension in VIDEO_EXTENSIONS:
        return VIDEOS_SUBDIR
    return None


def reconcile_media_layout(root: Path, organize_media: bool) -> list[str]:
    if not root.exists():
        return []

    moved_paths: list[str] = []

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        extension = path.suffix.lower()
        target_subdir = media_subdir_for_extension(extension)
        if target_subdir is None:
            continue

        destination_dir = root
        if organize_media:
            destination_dir = root / target_subdir
            if destination_dir in path.parents:
                continue
        elif path.parent == root:
            continue

        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = ensure_unique_path(destination_dir / path.name)
        shutil.move(str(path), str(destination))
        moved_paths.append(str(destination.relative_to(root)))

    for directory_name in (IMAGES_SUBDIR, VIDEOS_SUBDIR):
        directory = root / directory_name
        if directory.exists():
            try:
                directory.rmdir()
            except OSError:
                pass

    return moved_paths


def target_path_for_media(root: Path, metadata: dict[str, object], organize_media: bool) -> Path:
    extension = str(metadata["extension"]).lower()
    filename = f"{metadata['tweet_id']}_{metadata['num']}.{extension}"
    if organize_media:
        target_subdir = media_subdir_for_extension(f".{extension}")
        if target_subdir:
            return root / target_subdir / filename
    return root / filename


def collect_media_stats(root: Path) -> dict[str, object]:
    paths: set[str] = set()
    sizes: dict[str, int] = {}
    extension_breakdown: dict[str, int] = {}
    image_count = 0
    video_count = 0
    total_bytes = 0

    if root.exists():
        for path in root.rglob("*"):
            if not path.is_file() or path.name.startswith("."):
                continue
            if path.name == SUMMARY_FILENAME:
                continue

            extension = path.suffix.lower()
            if extension not in MEDIA_EXTENSIONS:
                continue

            relative_path = str(path.relative_to(root))
            size = path.stat().st_size
            paths.add(relative_path)
            sizes[relative_path] = size
            extension_breakdown[extension] = extension_breakdown.get(extension, 0) + 1
            total_bytes += size

            if extension in IMAGE_EXTENSIONS:
                image_count += 1
            elif extension in VIDEO_EXTENSIONS:
                video_count += 1

    return {
        "paths": paths,
        "sizes": sizes,
        "count": len(paths),
        "total_bytes": total_bytes,
        "image_count": image_count,
        "video_count": video_count,
        "extension_breakdown": dict(sorted(extension_breakdown.items())),
    }


def count_archive_entries(archive_file: Path) -> int | None:
    if not archive_file.exists():
        return None

    try:
        with sqlite3.connect(f"file:{archive_file}?mode=ro", uri=True) as connection:
            row = connection.execute("SELECT COUNT(*) FROM archive").fetchone()
            return int(row[0]) if row else 0
    except sqlite3.DatabaseError:
        try:
            with archive_file.open("r", encoding="utf-8") as handle:
                return sum(1 for _ in handle)
        except OSError:
            return None


def load_archive_keys(archive_file: Path) -> set[str]:
    if not archive_file.exists():
        return set()

    try:
        with sqlite3.connect(f"file:{archive_file}?mode=ro", uri=True) as connection:
            rows = connection.execute("SELECT entry FROM archive").fetchall()
            return {str(row[0]) for row in rows}
    except sqlite3.DatabaseError:
        try:
            with archive_file.open("r", encoding="utf-8") as handle:
                return {line.strip() for line in handle if line.strip()}
        except OSError:
            return set()


def insert_archive_entry(archive_file: Path, key: str) -> None:
    archive_file.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(archive_file, timeout=60) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS archive (entry TEXT PRIMARY KEY) WITHOUT ROWID"
        )
        connection.execute("INSERT OR IGNORE INTO archive (entry) VALUES (?)", (key,))
        connection.commit()


def derive_run_status(exit_code: int, new_media_files: int, failed_media_files: int) -> str:
    if exit_code == 0:
        return "success"
    if new_media_files > 0 and failed_media_files > 0:
        return "partial"
    return "error"


def resolve_cookie_source(args: argparse.Namespace, browser_override: str | None) -> str:
    if args.cookies_file:
        return str(args.cookies_file.expanduser().resolve())
    if browser_override or args.cookies_browser:
        return f"browser:{browser_override or args.cookies_browser}"
    return "auto"


def build_run_summary(
    *,
    handle: str,
    profile_url: str,
    target_url: str,
    target_dir: Path,
    archive_file: Path,
    args: argparse.Namespace,
    browser_override: str | None,
    started_at: datetime,
    finished_at: datetime,
    duration_seconds: float,
    exit_code: int,
    before_stats: dict[str, object],
    after_stats: dict[str, object],
    discovered_media_files: int,
    skipped_media_files: int,
    failed_downloads: list[dict[str, str]],
) -> tuple[dict[str, object], Path]:
    before_paths = before_stats["paths"]
    after_paths = after_stats["paths"]
    after_sizes = after_stats["sizes"]
    new_media_paths = sorted(after_paths - before_paths)
    new_media_bytes = sum(after_sizes[path] for path in new_media_paths)
    summary_path = target_dir / SUMMARY_FILENAME
    failed_media_files = len(failed_downloads)

    summary = {
        "status": derive_run_status(exit_code, len(new_media_paths), failed_media_files),
        "exit_code": exit_code,
        "profile_handle": handle,
        "profile_input": args.profile,
        "profile_url": profile_url,
        "target_url": target_url,
        "output_dir": str(target_dir),
        "archive_file": str(archive_file),
        "summary_file": str(summary_path),
        "cookie_source": resolve_cookie_source(args, browser_override),
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_seconds": round(duration_seconds, 3),
        "duration_human": format_duration(duration_seconds),
        "discovered_media_files": discovered_media_files,
        "skipped_media_files": skipped_media_files,
        "new_media_files": len(new_media_paths),
        "new_media_bytes": new_media_bytes,
        "new_media_bytes_human": format_bytes(new_media_bytes),
        "new_media_paths": new_media_paths,
        "failed_media_files": failed_media_files,
        "failed_downloads": failed_downloads,
        "total_media_files": after_stats["count"],
        "total_media_bytes": after_stats["total_bytes"],
        "total_media_bytes_human": format_bytes(after_stats["total_bytes"]),
        "image_files": after_stats["image_count"],
        "video_files": after_stats["video_count"],
        "pre_existing_media_files": before_stats["count"],
        "archive_entries": count_archive_entries(archive_file),
        "extension_breakdown": after_stats["extension_breakdown"],
        "options": {
            "include_retweets": args.include_retweets,
            "include_quoted": args.include_quoted,
            "exclude_pinned": args.exclude_pinned,
            "write_info_json": args.write_info_json,
            "verify_auth": args.verify_auth,
            "concurrency": args.concurrency,
            "retries": args.retries,
            "timeout": args.timeout,
        },
    }
    return summary, summary_path


def write_summary_file(path: Path, summary: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2)
        handle.write("\n")


def run_checked(command: list[str], env: dict[str, str] | None = None) -> None:
    subprocess.run(command, check=True, env=env)


def extract_media_items(command: list[str], env: dict[str, str]) -> list[dict[str, object]]:
    discovery_command = [command[0], "-q", "-s", "-j", *command[1:]]
    result = subprocess.run(
        discovery_command,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise RuntimeError(stderr or f"gallery-dl exited with status {result.returncode}")

    payload = result.stdout.strip()
    if not payload:
        return []

    try:
        messages = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"failed to parse gallery-dl JSON output: {exc}") from exc

    items: list[dict[str, object]] = []
    for entry in messages:
        if (
            isinstance(entry, list)
            and len(entry) >= 3
            and entry[0] == 3
            and isinstance(entry[1], str)
            and isinstance(entry[2], dict)
        ):
            items.append({"url": entry[1], "metadata": entry[2]})
    return items


def write_media_info_file(path: Path, source_url: str, metadata: dict[str, object]) -> None:
    info_path = path.with_suffix(path.suffix + ".info.json")
    info = dict(metadata)
    info["download_url"] = source_url
    with info_path.open("w", encoding="utf-8") as handle:
        json.dump(info, handle, indent=2)
        handle.write("\n")


def cleanup_partial_downloads(destination: Path) -> None:
    for candidate in (
        destination,
        destination.with_name(destination.name + ".part"),
        destination.with_name(destination.name + ".ytdl"),
    ):
        candidate.unlink(missing_ok=True)


def download_direct_file(url: str, destination: Path, timeout: int) -> None:
    temp_path = destination.with_name(destination.name + ".part")
    request = Request(url, headers={"User-Agent": DOWNLOAD_USER_AGENT})

    try:
        with urlopen(request, timeout=timeout) as response, temp_path.open("wb") as handle:
            shutil.copyfileobj(response, handle, length=1024 * 1024)
        temp_path.replace(destination)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def build_ytdlp_command(
    ytdlp_bin: Path,
    source_url: str,
    destination: Path,
    args: argparse.Namespace,
    browser_override: str | None,
) -> list[str]:
    command = [
        str(ytdlp_bin),
        "--quiet",
        "--no-warnings",
        "--no-progress",
        "--socket-timeout",
        str(args.timeout),
        "--retries",
        str(args.retries),
        "--fragment-retries",
        str(args.retries),
        "--file-access-retries",
        str(args.retries),
        "-o",
        str(destination),
    ]

    if args.cookies_file:
        command.extend(["--cookies", str(args.cookies_file.expanduser().resolve())])
    elif browser_override or args.cookies_browser:
        command.extend(["--cookies-from-browser", browser_override or args.cookies_browser])

    command.append(source_url)
    return command


def download_media_item(
    item: dict[str, object],
    target_dir: Path,
    ytdlp_bin: Path,
    args: argparse.Namespace,
    browser_override: str | None,
    archive_file: Path,
    archive_lock: Lock,
) -> str:
    metadata = item["metadata"]
    url = str(item["url"])
    destination = target_path_for_media(target_dir, metadata, args.organize_media)
    destination.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None

    for attempt in range(1, args.retries + 1):
        try:
            cleanup_partial_downloads(destination)
            if url.startswith("ytdl:"):
                command = build_ytdlp_command(
                    ytdlp_bin,
                    url[5:],
                    destination,
                    args,
                    browser_override,
                )
                result = subprocess.run(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                if result.returncode != 0:
                    message = result.stderr.strip() or result.stdout.strip() or "yt-dlp failed"
                    raise RuntimeError(message)
            else:
                download_direct_file(url, destination, args.timeout)
            break
        except Exception as exc:
            last_error = exc
            cleanup_partial_downloads(destination)
            if attempt < args.retries:
                print(
                    f"[retry] {destination.name} attempt {attempt + 1}/{args.retries}",
                    file=sys.stderr,
                )
                time.sleep(min(2 ** (attempt - 1), 5))
            else:
                raise RuntimeError(str(last_error)) from last_error

    if args.write_info_json:
        write_media_info_file(destination, url, metadata)

    archive_key = archive_key_from_metadata(metadata)
    with archive_lock:
        insert_archive_entry(archive_file, archive_key)

    return str(destination.relative_to(target_dir))


def download_media_items(
    items: list[dict[str, object]],
    target_dir: Path,
    archive_file: Path,
    ytdlp_bin: Path,
    args: argparse.Namespace,
    browser_override: str | None,
) -> dict[str, object]:
    archive_keys = load_archive_keys(archive_file)
    pending: list[dict[str, object]] = []
    seen_keys: set[str] = set()
    skipped = 0

    for item in items:
        metadata = item["metadata"]
        archive_key = archive_key_from_metadata(metadata)
        if archive_key in seen_keys:
            continue
        seen_keys.add(archive_key)

        destination = target_path_for_media(target_dir, metadata, args.organize_media)
        if archive_key in archive_keys or destination.exists():
            skipped += 1
            continue
        pending.append(item)

    total = len(pending)
    print(
        f"[queue] discovered={len(items)} pending={total} skipped={skipped} concurrency={args.concurrency}",
        file=sys.stderr,
    )
    if not pending:
        return {
            "exit_code": 0,
            "discovered": len(items),
            "pending": total,
            "skipped": skipped,
            "failures": [],
        }

    archive_lock = Lock()
    progress_lock = Lock()
    completed = 0
    failures: list[dict[str, str]] = []

    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = {
            executor.submit(
                download_media_item,
                item,
                target_dir,
                ytdlp_bin,
                args,
                browser_override,
                archive_file,
                archive_lock,
            ): item
            for item in pending
        }

        for future in as_completed(futures):
            item = futures[future]
            metadata = item["metadata"]
            relative_path = str(
                target_path_for_media(target_dir, metadata, args.organize_media).relative_to(target_dir)
            )
            with progress_lock:
                completed += 1
                progress = f"{completed}/{total}"

            try:
                saved_path = future.result()
            except Exception as exc:
                failures.append({"path": relative_path, "error": str(exc)})
                print(f"[failed] {progress} {relative_path}: {exc}", file=sys.stderr)
            else:
                print(f"[saved] {progress} {saved_path}", file=sys.stderr)

    if failures:
        print(f"[summary] {len(failures)} file(s) failed to download.", file=sys.stderr)
        for failure in failures[:10]:
            print(f"[failure] {failure['path']}: {failure['error']}", file=sys.stderr)

    return {
        "exit_code": 1 if failures else 0,
        "discovered": len(items),
        "pending": total,
        "skipped": skipped,
        "failures": failures,
    }


def run_preflight(command: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    preview_command = [command[0], "-s", "--post-range", "1", *command[1:]]
    return subprocess.run(
        preview_command,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def is_auth_required(output: str, returncode: int) -> bool:
    lowered = output.lower()
    return returncode == 16 or any(marker.lower() in lowered for marker in AUTH_REQUIRED_MARKERS)


def is_cookie_db_missing(output: str) -> bool:
    lowered = output.lower()
    return all(marker in lowered for marker in COOKIE_DB_MISSING_MARKERS)


def pick_browser_cookies(
    gallery_bin: Path,
    target_url: str,
    target_dir: Path,
    archive_file: Path,
    args: argparse.Namespace,
    env: dict[str, str],
    candidate_browsers: Iterable[str] | None = None,
) -> tuple[str | None, str | None]:
    base_command = build_gallery_dl_command(gallery_bin, target_url, target_dir, archive_file, args)

    print("[check] Testing whether the timeline is accessible without cookies...", file=sys.stderr)
    result = run_preflight(base_command, env)
    if result.returncode == 0:
        return None, None

    if not is_auth_required(result.stdout, result.returncode):
        return None, result.stdout

    print("[auth] X requires authenticated cookies for this profile. Trying browser sessions...", file=sys.stderr)

    for browser in candidate_browsers or AUTO_COOKIE_BROWSERS:
        print(f"[auth] Trying cookies from {browser}...", file=sys.stderr)
        browser_command = build_gallery_dl_command(
            gallery_bin,
            target_url,
            target_dir,
            archive_file,
            args,
            browser_override=browser,
        )
        browser_result = run_preflight(browser_command, env)
        if browser_result.returncode == 0:
            print(f"[auth] Using cookies from {browser}.", file=sys.stderr)
            return browser, None

    return None, result.stdout


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        handle, target_url = normalize_profile(args.profile)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    target_dir = args.output_dir.expanduser().resolve() / handle
    target_dir.mkdir(parents=True, exist_ok=True)
    initial_moves = reconcile_media_layout(target_dir, args.organize_media)
    if initial_moves:
        destination_label = f"{IMAGES_SUBDIR}/ and {VIDEOS_SUBDIR}/" if args.organize_media else "the root folder"
        print(
            f"[organize] moved {len(initial_moves)} existing media file(s) into {destination_label}",
            file=sys.stderr,
        )
    before_stats = collect_media_stats(target_dir)

    archive_file = (
        args.archive_file.expanduser().resolve()
        if args.archive_file
        else target_dir / ".download-archive.txt"
    )

    try:
        gallery_bin, ytdlp_bin = ensure_toolchain(args.venv_dir, args.skip_bootstrap)
    except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    env = os.environ.copy()
    venv_bin_dir = ytdlp_bin.parent
    env["PATH"] = f"{venv_bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["PYTHONWARNINGS"] = (
        f"{env['PYTHONWARNINGS']},ignore" if env.get("PYTHONWARNINGS") else "ignore"
    )
    browser_override = None

    command = build_gallery_dl_command(
        gallery_bin,
        target_url,
        target_dir,
        archive_file,
        args,
        browser_override=browser_override,
    )

    if args.dry_run:
        if not args.cookies_file and not args.cookies_browser:
            print(
                "[note] Real runs will auto-try common browser cookies if X blocks anonymous access.",
                file=sys.stderr,
            )
        print(quoted_command(command))
        return 0

    browser_override = None
    preflight_error = None
    if args.cookies_browser and args.verify_auth:
        preflight_result = run_preflight(command, env)
        if preflight_result.returncode == 0:
            pass
        elif is_cookie_db_missing(preflight_result.stdout):
            print(
                f'[auth] Requested cookies from {args.cookies_browser}, but no readable cookie database was found.',
                file=sys.stderr,
            )
            print("[auth] Trying other browser sessions instead...", file=sys.stderr)
            candidates = tuple(
                browser for browser in AUTO_COOKIE_BROWSERS if browser != args.cookies_browser
            )
            browser_override, preflight_error = pick_browser_cookies(
                gallery_bin,
                target_url,
                target_dir,
                archive_file,
                args,
                env,
                candidate_browsers=candidates,
            )
            if preflight_error:
                print(
                    preflight_error,
                    end="" if preflight_error.endswith("\n") else "\n",
                    file=sys.stderr,
                )
                print(
                    "error: no working browser cookie session was found. "
                    "Log into X in one of Edge, Chrome, Brave, Safari, or Firefox, then rerun.",
                    file=sys.stderr,
                )
                return 16
            command = build_gallery_dl_command(
                gallery_bin,
                target_url,
                target_dir,
                archive_file,
                args,
                browser_override=browser_override,
            )
        else:
            print(
                preflight_result.stdout,
                end="" if preflight_result.stdout.endswith("\n") else "\n",
                file=sys.stderr,
            )
            print(
                f"error: cookies from {args.cookies_browser} were loaded, but X still rejected the request.",
                file=sys.stderr,
            )
            return preflight_result.returncode or 16
    elif args.cookies_browser:
        print(
            "[note] Skipping auth preflight because you provided --cookies-browser. "
            "Use --verify-auth if you want the extra check.",
            file=sys.stderr,
        )
    elif args.cookies_file and not args.verify_auth:
        print(
            "[note] Skipping auth preflight because you provided --cookies-file. "
            "Use --verify-auth if you want the extra check.",
            file=sys.stderr,
        )
    elif args.cookies_file and args.verify_auth:
        preflight_result = run_preflight(command, env)
        if preflight_result.returncode != 0:
            print(
                preflight_result.stdout,
                end="" if preflight_result.stdout.endswith("\n") else "\n",
                file=sys.stderr,
            )
            print("error: the provided cookies file did not unlock the timeline.", file=sys.stderr)
            return preflight_result.returncode or 16
    elif not args.cookies_file:
        print(
            "[note] X/Twitter often requires authenticated cookies for profile timeline scraping.",
            file=sys.stderr,
        )
        browser_override, preflight_error = pick_browser_cookies(
            gallery_bin,
            target_url,
            target_dir,
            archive_file,
            args,
            env,
        )
        if preflight_error:
            print(preflight_error, end="" if preflight_error.endswith("\n") else "\n", file=sys.stderr)
            print(
                'error: automatic cookie detection could not unlock the timeline. '
                'Rerun with --cookies-browser "firefox" or --cookies-file "/path/to/cookies.txt" '
                "from a browser where you're logged into X.",
                file=sys.stderr,
            )
            return 16

        command = build_gallery_dl_command(
            gallery_bin,
            target_url,
            target_dir,
            archive_file,
            args,
            browser_override=browser_override,
        )

    print(f"[download] {handle} -> {target_dir}", file=sys.stderr)
    print(f"[command] {quoted_command(command)}", file=sys.stderr)
    started_at = datetime.now().astimezone()
    started_timer = time.monotonic()

    try:
        items = extract_media_items(command, env)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    download_result = download_media_items(
        items,
        target_dir,
        archive_file,
        ytdlp_bin,
        args,
        browser_override,
    )
    download_exit_code = int(download_result["exit_code"])
    finished_at = datetime.now().astimezone()
    duration_seconds = time.monotonic() - started_timer
    moved_media = reconcile_media_layout(target_dir, args.organize_media)
    if moved_media:
        destination_label = f"{IMAGES_SUBDIR}/ and {VIDEOS_SUBDIR}/" if args.organize_media else "the root folder"
        print(
            f"[organize] moved {len(moved_media)} media file(s) into {destination_label}",
            file=sys.stderr,
        )
    after_stats = collect_media_stats(target_dir)
    summary, summary_path = build_run_summary(
        handle=handle,
        profile_url=f"https://x.com/{handle}",
        target_url=target_url,
        target_dir=target_dir,
        archive_file=archive_file,
        args=args,
        browser_override=browser_override,
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=duration_seconds,
        exit_code=download_exit_code,
        before_stats=before_stats,
        after_stats=after_stats,
        discovered_media_files=int(download_result["discovered"]),
        skipped_media_files=int(download_result["skipped"]),
        failed_downloads=list(download_result["failures"]),
    )

    write_summary_file(summary_path, summary)
    print(
        "[summary] "
        f"new={summary['new_media_files']} "
        f"total={summary['total_media_files']} "
        f"images={summary['image_files']} "
        f"videos={summary['video_files']} "
        f"new_size={summary['new_media_bytes_human']} "
        f"total_size={summary['total_media_bytes_human']} "
        f"duration={summary['duration_human']}",
        file=sys.stderr,
    )
    print(f"[summary-file] {summary_path}", file=sys.stderr)

    if download_exit_code != 0:
        print(f"error: downloader exited with status {download_exit_code}", file=sys.stderr)
        return download_exit_code

    print("[done] Download completed.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
