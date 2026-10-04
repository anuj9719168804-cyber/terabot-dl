import os
import time
import threading
import asyncio
import logging
from telethon import Button
from telethon.errors import FloodWaitError

from .bot import (
    bot, terabox_queue, _safe_send, active_tasks, download_progress,
)
from .helpers import format_size, format_duration
from .progress_callbacks import make_download_progress_cb
from .download_store import store_download, notify_done

from terabox.public_api import TeraBoxError, CancelledError
from teraboxDL.public_api import download_terabox_file_experimental
from diskwalaDL.public_api import get_diskwala_info, extract_diskwala_id, DiskwalaError

log = logging.getLogger(__name__)

# Diskwala shares its own cache bucket / user mode.
DW_MODE = "dw"


# — Heart Function —————————————————————————————————————————————————————————————

#! ONLY PUBLIC API
async def process_diskwala(event, diskwala_url: str) -> None:
    # If currently in flood cooldown → queue immediately
    rem = terabox_queue.flood_remaining()
    if rem > 0:
        await terabox_queue.put(_dw_helper, event, diskwala_url)
        try:
            await event.respond(
                "⏳ Bot overloaded! Your request has been queued "
                f"and will be processed automatically in ~{rem}s."
            )
        except FloodWaitError as e:
            terabox_queue.update_flood_until(e.seconds)
        except Exception:
            pass
        return

    # Try processing normally under the semaphore
    async with terabox_queue.semaphore:
        try:
            await _dw_helper(event, diskwala_url)
        except FloodWaitError as e:
            # Pipeline hit flood → set cooldown, queue, notify user
            terabox_queue.update_flood_until(e.seconds)
            await terabox_queue.put(_dw_helper, event, diskwala_url)
            try:
                await event.respond(
                    f"⏳ Bot overloaded! Your request has been queued "
                    f"and will be processed automatically in ~{e.seconds}s."
                )
            except Exception:
                pass


async def _dw_helper(event, diskwala_url: str) -> None:
    """Inner pipeline, runs under the concurrency semaphore."""
    chat_id = event.chat_id
    link_id = extract_diskwala_id(diskwala_url) or diskwala_url
    user_mode = DW_MODE
    task_key = (chat_id, link_id)
    total_start = time.time()

    cancel_event = threading.Event()
    active_tasks[task_key] = cancel_event

    cancel_btn = [[Button.inline("❌ Cancel", data=f"cancel:{link_id}")]]

    def _cleanup_files(*paths):
        """Remove temp/downloaded files from disk."""
        for p in paths:
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                    log.info(f"Cleaned up file: {p}")
                except Exception as e:
                    log.warning(f"Could not clean up {p}: {e}")

    # — Phase 1: Cache lookup ——————————————————————————————————————————————
    # Download-only mode: Telegram media cache is disabled (no uploads happen),
    # so skip the lookup entirely.
    status = await _safe_send(event.respond, f"🔍 Menyiapkan download `{link_id}`…")

    # — Phase 2: Prepare metadata ——————————————————————————————————————————
    await _safe_send(status.edit, "⏳ Fetching metadata…", buttons=cancel_btn)

    #! GET FILE INFO
    try:
        info = await asyncio.to_thread(get_diskwala_info, diskwala_url)
    except DiskwalaError as e:
        log.error(f"Diskwala metadata fetch failed for {link_id}: {e}")
        await _safe_send(status.edit, f"❌ Failed to get video info: {e}")
        active_tasks.pop(task_key, None)
        return
    except Exception as e:
        log.exception(f"Unexpected Diskwala metadata error for {link_id}")
        await _safe_send(status.edit, f"❌ Failed to get video info: {e}")
        active_tasks.pop(task_key, None)
        return

    download_url = info["download_url"]
    filename = info["filename"]
    size_str = format_size(info["size"])

    await _safe_send(
        status.edit,
        f"📦 **{filename}**\n📐 Size: **{size_str}**\n\n⬇️ Downloading… **0%**",
        buttons=cancel_btn,
    )

    # — Phase 3: Download (download-only: no Telegram upload) ————————————————————
    loop = asyncio.get_running_loop()
    dl_start = time.time()
    _base_cb = make_download_progress_cb(status, filename, size_str, loop, cancel_btn)
    prog_key = (chat_id, link_id)
    download_progress[prog_key] = {"filename": filename, "done": 0,
                                   "total": info["size"], "started": dl_start}

    def dl_progress_cb(done, total):
        download_progress[prog_key]["done"] = done
        if total:
            download_progress[prog_key]["total"] = total
        return _base_cb(done, total)

    try:
        filepath = await asyncio.to_thread(
            download_terabox_file_experimental, download_url, filename, cancel_event, dl_progress_cb
        )
    except CancelledError:
        await _safe_send(status.edit, "🚫 Cancelled.")
        active_tasks.pop(task_key, None)
        download_progress.pop(prog_key, None)
        return
    except TeraBoxError as e:
        log.error(f"Download error for {link_id}: {e}")
        await _safe_send(status.edit, f"❌ Download failed: {e}")
        active_tasks.pop(task_key, None)
        download_progress.pop(prog_key, None)
        return
    except Exception as e:
        log.exception(f"Unexpected download error for {link_id}")
        await _safe_send(status.edit, f"❌ Download failed: {e}")
        active_tasks.pop(task_key, None)
        download_progress.pop(prog_key, None)
        return
    dl_time = time.time() - dl_start
    download_progress.pop(prog_key, None)

    if cancel_event.is_set():
        _cleanup_files(filepath, os.path.splitext(filepath)[0] + ".ts")
        await _safe_send(status.edit, "🚫 Cancelled.")
        active_tasks.pop(task_key, None)
        return

    # Use actual file size on disk instead of the API-reported size
    size_str = format_size(os.path.getsize(filepath))

    # — Phase 4: Store on disk + notify (NO Telegram upload) ———————————————————
    if cancel_event.is_set():
        _cleanup_files(filepath, os.path.splitext(filepath)[0] + ".ts")
        await _safe_send(status.edit, "🚫 Cancelled.")
        active_tasks.pop(task_key, None)
        return

    try:
        final_path = await asyncio.to_thread(store_download, filepath, filename, link_id)
        _cleanup_files(os.path.splitext(filepath)[0] + ".ts")
    except Exception as e:
        log.exception(f"Store failed for {link_id}")
        await _safe_send(status.edit, f"❌ Gagal menyimpan file: {e}")
        active_tasks.pop(task_key, None)
        return

    await _safe_send(status.delete)
    await notify_done(
        lambda m: _safe_send(event.respond, m),
        filename, final_path, os.path.getsize(final_path), dl_time,
    )
    log.info(f"Download-only complete: {final_path}")

    active_tasks.pop(task_key, None)
