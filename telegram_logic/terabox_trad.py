import os
import time 
import threading
import asyncio
import logging
from telethon import Button
from telethon.errors import FloodWaitError

from .bot import bot, _find_cached_video, _safe_send, active_tasks, STORAGE_GROUP_ID, download_progress, terabox_queue
from .helpers import format_size, format_duration
from .progress_callbacks import make_download_progress_cb
from .download_store import store_download, notify_done

from terabox.public_api import prepare_terabox_link, download_terabox_file, TeraBoxError, CancelledError

log = logging.getLogger(__name__)

# — Heart Function —————————————————————————————————————————————————————————————

#! ONLY PUBLIC API
async def process_terabox(event, surl: str) -> None:
    """
    Entry point: process immediately, or queue if flood-gated.
    Users never need to re-send — queued requests auto-process.
    """
    # If currently in flood cooldown → queue immediately
    rem = terabox_queue.flood_remaining()
    if rem > 0:
        await terabox_queue.put(helper, event, surl)
        try:
            await event.respond(
                f"⏳ Bot overloaded! Your request for `{surl}` has been queued "
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
            await helper(event, surl)
        except FloodWaitError as e:
            # Pipeline hit flood → set cooldown, queue, notify user
            terabox_queue.update_flood_until(e.seconds)
            await terabox_queue.put(helper, event, surl)
            try:
                await event.respond(
                    f"⏳ Bot overloaded! Your request for `{surl}` has been queued "
                    f"and will be processed automatically in ~{e.seconds}s."
                )
            except Exception:
                pass


async def helper(event, surl: str) -> None:
    """Inner pipeline, runs under the concurrency semaphore."""
    chat_id = event.chat_id
    task_key = (chat_id, surl)
    total_start = time.time()

    cancel_event = threading.Event()
    active_tasks[task_key] = cancel_event

    cancel_btn = [[Button.inline("❌ Cancel", data=f"cancel:{surl}")]]

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
    status = await _safe_send(event.respond, f"🔍 Menyiapkan download `{surl}`…")

    # — Phase 2: Prepare metadata ——————————————————————————————————————————
    await _safe_send(status.edit, f"⏳ Fetching metadata…", buttons=cancel_btn)
    
    try:
        prepared = await asyncio.to_thread(prepare_terabox_link, surl)
    except CancelledError:
        await _safe_send(status.edit, "🚫 Cancelled.")
        active_tasks.pop(task_key, None)
        return
    except TeraBoxError as e:
        log.error(f"Prepare error for surl={surl}: {e}")
        await _safe_send(status.edit, f"❌ Error: {e}\n\nYou can try different *mode* to download.\nSwitch *mode* from /settings")
        active_tasks.pop(task_key, None)
        return
    except Exception as e:
        log.exception(f"Unexpected prepare error for surl={surl}")
        await _safe_send(status.edit, f"❌ Unexpected error: {e}\n\nYou can try different *mode* to download.\nSwitch *mode* from /settings")
        active_tasks.pop(task_key, None)
        return

    if cancel_event.is_set():
        await _safe_send(status.edit, "🚫 Cancelled.")
        active_tasks.pop(task_key, None)
        return

    filename = prepared["filename"]
    size_str = format_size(prepared["size"])

    await _safe_send(
        status.edit,
        f"📦 **{filename}**\n📐 Size: **{size_str}**\n\n⬇️ Downloading… **0%**",
        buttons=cancel_btn,
    )

    # — Phase 3: Download (download-only: no Telegram upload) ————————————————————
    loop = asyncio.get_running_loop()
    dl_start = time.time()
    _base_cb = make_download_progress_cb(status, filename, size_str, loop, cancel_btn)
    prog_key = (chat_id, surl)
    download_progress[prog_key] = {"filename": filename, "done": 0,
                                   "total": prepared["size"], "started": dl_start}

    def dl_progress_cb(done, total):
        download_progress[prog_key]["done"] = done
        if total:
            download_progress[prog_key]["total"] = total
        return _base_cb(done, total)

    try:
        filepath = await asyncio.to_thread(download_terabox_file, prepared, cancel_event, dl_progress_cb)
    except CancelledError:
        await _safe_send(status.edit, "🚫 Cancelled.")
        active_tasks.pop(task_key, None)
        download_progress.pop(prog_key, None)
        return
    except TeraBoxError as e:
        log.error(f"Download error for surl={surl}: {e}")
        await _safe_send(status.edit, f"❌ Download failed: {e}\n\nYou can try different *mode* to download.\nSwitch *mode* from /settings")
        active_tasks.pop(task_key, None)
        download_progress.pop(prog_key, None)
        return
    except Exception as e:
        log.exception(f"Unexpected download error for surl={surl}")
        await _safe_send(status.edit, f"❌ Download failed: {e}\n\nYou can try different *mode* to download.\nSwitch *mode* from /settings")
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

    # Use actual file size instead of original API size
    size_str = format_size(os.path.getsize(filepath))

    # — Phase 4: Store on disk + notify (NO Telegram upload) ———————————————————
    if cancel_event.is_set():
        _cleanup_files(filepath, os.path.splitext(filepath)[0] + ".ts")
        await _safe_send(status.edit, "🚫 Cancelled.")
        active_tasks.pop(task_key, None)
        return

    try:
        final_path = await asyncio.to_thread(store_download, filepath, filename, surl)
        _cleanup_files(os.path.splitext(filepath)[0] + ".ts")
    except Exception as e:
        log.exception(f"Store failed for surl={surl}")
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

