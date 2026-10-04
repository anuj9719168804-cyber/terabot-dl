"""
telegram_logic/download_store.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Download-only delivery: finished files are kept on disk (no Telegram upload).
The user is notified with the on-disk path only (no FileBrowser link).

Layout:
    DOWNLOAD_DIR/<link-id>_<YYYY-MM-DD>/<file>
where <link-id> is the TeraBox surl (or Diskwala link id), sanitized so it
is always a safe single directory name, and the date is the download date
in WIB (Asia/Jakarta) so re-downloads of the same link on another day land
in a fresh folder instead of mixing.

Env knobs:
    DOWNLOAD_DIR   base dir for finished files (default: <repo>/downloads)
    CLEANUP_DAYS   auto-delete files older than N days (default: 7, 0 = off)
"""
import logging
import os
import re
import shutil
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR") or os.path.join(REPO_ROOT, "downloads")
CLEANUP_DAYS = float(os.environ.get("CLEANUP_DAYS", "7") or 7)


def ensure_download_dir() -> str:
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    return DOWNLOAD_DIR


def sanitize_folder_name(name: str | None) -> str:
    """Turn an arbitrary link id into a safe single directory name."""
    name = (name or "").strip()
    # keep only filesystem/URL-safe chars
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    name = re.sub(r"_+", "_", name).strip("._-")
    if not name:
        name = "misc"
    return name[:80]


# User-facing date for folder names (user is in WIB / Asia/Jakarta).
_WIB = timezone(timedelta(hours=7))


def dated_folder_name(folder: str | None) -> str:
    """<sanitized link-id>_<YYYY-MM-DD> using the WIB download date."""
    return f"{sanitize_folder_name(folder)}_{datetime.now(_WIB).strftime('%Y-%m-%d')}"


def _dedup_path(path: str) -> str:
    """If path exists, append (1), (2), ... before the extension."""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while True:
        cand = f"{base} ({i}){ext}"
        if not os.path.exists(cand):
            return cand
        i += 1


def store_download(tmp_path: str, filename: str | None = None,
                   folder: str | None = None) -> str:
    """
    Move a finished download into DOWNLOAD_DIR/<link-id>_<YYYY-MM-DD>/.
    `folder` is typically the link's surl/id; the WIB download date is
    appended so the same link downloaded on different days never mixes.
    The folder name is sanitized to always be a single directory level.
    When omitted the file lands directly in DOWNLOAD_DIR
    (backwards compatible).
    Returns the final absolute path. The file is KEPT (not deleted).
    """
    ensure_download_dir()
    target_dir = DOWNLOAD_DIR
    if folder:
        target_dir = os.path.join(DOWNLOAD_DIR, dated_folder_name(folder))
        os.makedirs(target_dir, exist_ok=True)
    name = filename or os.path.basename(tmp_path)
    # sanitize: strip path separators just in case
    name = os.path.basename(name)
    dest = _dedup_path(os.path.join(target_dir, name))
    shutil.move(tmp_path, dest)
    log.info(f"Stored download: {dest}")
    return dest


def format_size(num: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024.0:
            return f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} PB"


def build_done_message(filename: str, path: str, size: int,
                       dl_time: float, resumed: bool = False) -> str:
    lines = [
        "✅ **Download selesai!**",
        f"📦 `{filename}`",
        f"📐 Size: **{format_size(size)}**",
        f"⬇️ Waktu download: **{dl_time:.0f} dtk**" + (" (resume)" if resumed else ""),
        "",
        f"📁 Tersimpan di:\n`{path}`",
    ]
    return "\n".join(lines)


async def notify_done(send_fn, filename: str, path: str, size: int,
                      dl_time: float, resumed: bool = False) -> None:
    """send_fn is an async callable like event.respond / status.edit."""
    msg = build_done_message(filename, path, size, dl_time, resumed)
    try:
        await send_fn(msg)
    except Exception as e:
        log.warning(f"notify_done failed: {e}")


def cleanup_old_downloads(max_age_days: float | None = None) -> tuple[int, int]:
    """
    Delete files under DOWNLOAD_DIR (including per-link subfolders) older
    than max_age_days. Empty subfolders are pruned afterwards.
    Returns (deleted_count, freed_bytes).
    """
    days = CLEANUP_DAYS if max_age_days is None else max_age_days
    if not days or days <= 0:
        return 0, 0
    cutoff = time.time() - days * 86400
    deleted, freed = 0, 0
    if not os.path.isdir(DOWNLOAD_DIR):
        return 0, 0
    for root, _dirs, files in os.walk(DOWNLOAD_DIR):
        for name in files:
            p = os.path.join(root, name)
            try:
                if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                    freed += os.path.getsize(p)
                    os.remove(p)
                    deleted += 1
                    log.info(f"Auto-cleanup deleted: {p}")
            except Exception as e:
                log.warning(f"Auto-cleanup failed for {p}: {e}")
    # prune empty subfolders (deepest first)
    for root, dirs, files in os.walk(DOWNLOAD_DIR, topdown=False):
        if root == DOWNLOAD_DIR:
            continue
        if not dirs and not files:
            try:
                os.rmdir(root)
            except Exception:
                pass
    if deleted:
        log.info(f"Auto-cleanup: deleted {deleted} files, freed {freed/1024/1024:.1f} MB")
    return deleted, freed
