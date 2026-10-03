"""
telegram_logic/folder_select.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Folder-link support for /exp: when a link resolves to a folder, list all video
files inside as inline buttons; the picked file is downloaded immediately.
"""
import asyncio
import logging
import threading
import time
from urllib.parse import quote

from telethon import Button, events

from .bot import bot, _find_cached_video, _safe_send, active_tasks
from .helpers import format_size, format_duration
from .progress_callbacks import make_download_progress_cb, make_upload_progress_cb
from .terabox_exp import helper as exp_helper
from teraboxDL.terabox_dl import _get_video_metadata
from teraboxDL.public_api import TeraBoxError
from firebase_db.cache import add_to_cache

log = logging.getLogger(__name__)

VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v")
DL_EXTS = VIDEO_EXTS + (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")

def _is_downloadable(f):
    return str(f.get("server_filename", "")).lower().endswith(DL_EXTS)

# pending selections: {(chat_id, surl): {"options": [file dicts], "base_url": str, "ts": float}}
_PENDING = {}


def list_folder_videos(terabox_url: str, max_files: int = 20) -> tuple[str, list]:
    """Walk the folder tree and return (base_url, [video file dicts])."""
    base_url = terabox_url.split("?")[0]
    first = _get_video_metadata(base_url)
    queue = [f["path"] for f in first.get("list", []) if f.get("isdir")]
    found = [f for f in first.get("list", [])
             if not f.get("isdir") and _is_downloadable(f) and (f.get("stream_url") or f.get("direct_link"))]
    visited = 0
    while queue and len(found) < max_files and visited < 30:
        dirpath = queue.pop(0)
        visited += 1
        try:
            sub = _get_video_metadata(base_url + "?dir=" + quote(dirpath))
        except Exception as e:
            log.warning(f"list_folder_videos: failed to list {dirpath}: {e}")
            continue
        for f in sub.get("list", []):
            if f.get("isdir"):
                queue.append(f["path"])
            elif _is_downloadable(f) and (f.get("stream_url") or f.get("direct_link")):
                found.append(f)
    return base_url, found


def download_picked(base_url: str, file_info: dict, is_hd: bool = False) -> dict:
    """Build the info dict for a picked file and download via the exp pipeline."""
    if is_hd:
        return {
            "filename": file_info.get("server_filename", "unknown"),
            "size": int(file_info.get("size", 0)),
            "download_url": file_info.get("direct_link", ""),
        }
    return {
        "filename": file_info.get("server_filename", "unknown"),
        "size": int(file_info.get("size", 0)),
        "download_url": file_info.get("stream_url", "") or file_info.get("direct_link", ""),
    }


# — Inline button flow ————————————————————————————————————————————————————

PAGE_SIZE = 10

def store_pending(chat_id: int, surl: str, base_url: str, options: list) -> None:
    # keep only latest per chat
    for key in [k for k in _PENDING if k[0] == chat_id]:
        _PENDING.pop(key, None)
    _PENDING[(chat_id, surl)] = {"options": options, "base_url": base_url, "ts": time.time()}


async def show_folder_page(edit_fn, chat_id: int, surl: str, videos: list, page: int = 0,
                           reply_to_msg=None) -> None:
    """Show page of up to 10 thumbnail+buttons, with Next/Prev navigation."""
    import requests as _rq
    import tempfile as _tf
    import os as _os
    from telegram_logic.bot import bot as _b
    from telegram_logic.helpers import format_size as _fs

    total_pages = (len(videos) + PAGE_SIZE - 1) // PAGE_SIZE
    page = max(0, min(page, total_pages - 1))
    start = page * PAGE_SIZE
    chunk = videos[start:start + PAGE_SIZE]

    buttons = [[
        Button.inline(
            f"{start+j+1}. {v.get('server_filename','?')[:24]} ({_fs(int(v.get('size',0)))})",
            data=f"pickvideo:{surl}:{start+j}",
        )
    ] for j, v in enumerate(chunk)]

    nav = []
    if page > 0:
        nav.append(Button.inline("◀️ Prev", data=f"pickpage:{surl}:{page-1}"))
    if page < total_pages - 1:
        nav.append(Button.inline(f"Next ▶ ({page+2}/{total_pages})", data=f"pickpage:{surl}:{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([Button.inline(f"⬇️ Download Semua ({len(videos)} video)", data=f"pickall:{surl}")])
    buttons.append([Button.inline(f"🗜️ Download Semua sebagai ZIP ({len(videos)} video)", data=f"pickzip:{surl}")])

    # Thumbnails for this page
    _tmp_paths = []
    for j, v in enumerate(chunk):
        tu = v.get("thumbs", {}).get("url1") or v.get("thumbs", {}).get("url2")
        if not tu:
            continue
        tpath = None
        try:
            r = await asyncio.to_thread(_rq.get, tu, timeout=15)
            if r.status_code == 200 and len(r.content) > 500:
                fd, tpath = _tf.mkstemp(suffix=".jpg")
                with _os.fdopen(fd, "wb") as fh:
                    fh.write(r.content)
                _tmp_paths.append(tpath)
        except Exception as e:
            log.warning(f"thumb fetch failed for #{start+j+1}: {e}")
            if tpath and _os.path.exists(tpath):
                try: _os.remove(tpath)
                except Exception: pass

    if _tmp_paths:
        try:
            await _b.send_file(chat_id, _tmp_paths[:PAGE_SIZE],
                               caption=f"📄 Halaman {page+1}/{total_pages}",
                               reply_to=reply_to_msg)
        except Exception as e:
            log.warning(f"album send failed: {e}")
    for tp in _tmp_paths:
        if _os.path.exists(tp):
            try: _os.remove(tp)
            except Exception: pass

    await edit_fn(
        f"📂 Folder berisi **{len(videos)} video** — halaman {page+1}/{total_pages}. Pilih nomor 👇",
        buttons=buttons,
    )


@bot.on(events.CallbackQuery(pattern=rb"^pickpage:"))
async def handle_page(event):
    data = event.data.decode("utf-8", errors="ignore")  # pickpage:<surl>:<page>
    try:
        _, surl, page_s = data.split(":", 2)
        page = int(page_s)
    except ValueError:
        await event.answer("⚠️ Invalid page.")
        return
    chat_id = event.chat_id
    pending = _PENDING.get((chat_id, surl))
    if not pending:
        await event.answer("⚠️ Selection expired — send the link again.")
        return
    await event.answer()
    await show_folder_page(event.edit, chat_id, surl, pending["options"], page=page)


@bot.on(events.CallbackQuery(pattern=rb"^pickzip:"))
async def handle_pick_zip(event):
    """Download all videos, pack into 45MB multi-volume ZIP parts, send each part."""
    import subprocess as _sp
    import zipfile as _zf

    data = event.data.decode("utf-8", errors="ignore")  # pickzip:<surl>
    surl = data.split(":", 1)[1] if ":" in data else None
    chat_id = event.chat_id
    pending = _PENDING.get((chat_id, surl))
    if not pending:
        await event.answer("⚠️ Selection expired — send the link again.")
        return

    videos = pending["options"]
    base_url = pending["base_url"]
    total = len(videos)
    await event.answer(f"🗜️ Downloading {total} videos then packing ZIP...")

    async def _zip_all():
        from teraboxDL.public_api import download_terabox_file_experimental
        import os as _os
        workdir = f"storage/zip_{surl}_{int(time.time())}"
        _os.makedirs(workdir, exist_ok=True)
        ok, fail = 0, 0
        cancel_btn = [[Button.inline("❌ Cancel", data=f"cancel:{surl}")]]
        loop = asyncio.get_running_loop()

        for i, file_info in enumerate(videos):
            try:
                info = download_picked(base_url, file_info)
                fname = info["filename"]
                size_str = format_size(info["size"])
                status = await _safe_send(
                    event.respond,
                    f"🗜️ ZIP **[{i+1}/{total}]** {fname}\n📐 {size_str}\n⬇️ Downloading… **0%**",
                    buttons=cancel_btn)
                dl_progress_cb = make_download_progress_cb(status, fname, size_str, loop, cancel_btn)
                fp = await asyncio.to_thread(
                    download_terabox_file_experimental, info["download_url"], fname, None, dl_progress_cb)
                # move into workdir
                dest = _os.path.join(workdir, _os.path.basename(fp))
                _os.replace(fp, dest)
                ok += 1
            except Exception as e:
                fail += 1
                log.exception(f"zip-all download failed #{i+1}")
                try:
                    await event.respond(f"❌ [{i+1}/{total}] Gagal: {e}")
                except Exception:
                    pass

        if ok == 0:
            await event.respond("❌ Tidak ada video yang berhasil didownload, ZIP dibatalkan.")
            import shutil as _sh; _sh.rmtree(workdir, ignore_errors=True)
            _PENDING.pop((chat_id, surl), None)
            return

        # Pack into split zip volumes (45MB parts, safe under 50MB TG limit)
        await event.respond(f"🗜️ Packing {ok} video ke ZIP…")
        base_zip = _os.path.join(workdir, "videos")
        if not _os.path.isdir(workdir):
            _os.makedirs(workdir, exist_ok=True)
        base_zip_abs = _os.path.abspath(base_zip)
        r = await asyncio.to_thread(
            _sp.run, ["/usr/bin/zip", "-r", "-s", "45m", base_zip_abs + ".zip", "."],
            cwd=workdir, capture_output=True, text=True, timeout=1800)
        log.info(f"zip rc={r.returncode} stderr={r.stderr[:500]!r} stdout_tail={r.stdout[-300:]!r}")
        if r.returncode != 0:
            await event.respond(f"❌ Gagal bikin ZIP (rc={r.returncode}): {r.stderr[:300] or r.stdout[-300:]}")
            import shutil as _sh; _sh.rmtree(workdir, ignore_errors=True)
            _PENDING.pop((chat_id, surl), None)
            return

        # find parts: videos.z01 ... videos.zip (order matters: z01 first, .zip last)
        import glob as _glob, re as _re
        parts = sorted(
            _glob.glob(base_zip + ".z[0-9][0-9]"),
            key=lambda x: int(_re.search(r"\.z(\d+)$", x).group(1)))
        parts.append(base_zip + ".zip")

        await event.respond(f"📤 Mengirim {len(parts)} bagian ZIP… gabungkan semua file lalu ekstrak **videos.zip** terakhir.")
        for j, part in enumerate(parts):
            size_str = format_size(_os.path.getsize(part))
            await event.respond(f"📤 Bagian **{j+1}/{len(parts)}** ({size_str})…")
            try:
                from telegram_logic.bot import bot as _b
                await _b.send_file(chat_id, part,
                                   caption=f"🗜️ videos.zip — bagian {j+1}/{len(parts)}",
                                   force_document=True)
            except Exception as e:
                log.exception("zip part send failed")
                await event.respond(f"❌ Gagal kirim bagian {j+1}: {e}")

        import shutil as _sh
        _sh.rmtree(workdir, ignore_errors=True)
        _PENDING.pop((chat_id, surl), None)

    asyncio.create_task(_zip_all())


@bot.on(events.CallbackQuery(pattern=rb"^pickall:"))
async def handle_pick_all(event):
    data = event.data.decode("utf-8", errors="ignore")  # pickall:<surl>
    surl = data.split(":", 1)[1] if ":" in data else None
    chat_id = event.chat_id
    pending = _PENDING.get((chat_id, surl))
    if not pending:
        await event.answer("⚠️ Selection expired — send the link again.")
        return

    videos = pending["options"]
    base_url = pending["base_url"]
    await event.answer(f"⬇️ Downloading all {len(videos)} videos sequentially...")
    total = len(videos)

    # Background task so the callback returns fast
    async def _download_all():
        ok, fail = 0, 0
        for i, file_info in enumerate(videos):
            try:
                await _safe_send(
                    event.respond,
                    f"📦 **[{i+1}/{total}]** {file_info.get('server_filename','?')}\n⏳ Downloading…",
                )
                await _run_download(event, surl, base_url, file_info, seq_label=f"[{i+1}/{total}]")
                ok += 1
            except Exception as e:
                fail += 1
                log.exception(f"download-all failed for #{i+1}")
                try:
                    await event.respond(f"❌ [{i+1}/{total}] Gagal: {e}")
                except Exception:
                    pass
        try:
            await event.respond(f"✅ Selesai! Berhasil: **{ok}**, gagal: **{fail}**")
        except Exception:
            pass
        _PENDING.pop((chat_id, surl), None)

    asyncio.create_task(_download_all())


@bot.on(events.CallbackQuery(pattern=rb"^pickvideo:"))
async def handle_pick(event):
    data = event.data.decode("utf-8", errors="ignore")  # pickvideo:<surl>:<idx>
    try:
        _, surl, idx_s = data.split(":", 2)
        idx = int(idx_s)
    except ValueError:
        await event.answer("⚠️ Invalid selection.")
        return

    chat_id = event.chat_id
    pending = _PENDING.get((chat_id, surl))
    if not pending:
        await event.answer("⚠️ Selection expired — send the link again.")
        return

    options = pending["options"]
    if idx >= len(options):
        await event.answer("⚠️ Invalid option.")
        return

    file_info = options[idx]
    fname = file_info.get("server_filename", "video")
    await event.answer(f"⬇️ Downloading {fname}...")
    await _safe_send(event.edit if hasattr(event, "edit") else event.respond,
                     f"✅ Selected: **{fname}**\n⏳ Starting download…")

    # Reuse the full exp pipeline by faking a "file link" info via monkey-run:
    # simplest robust path: run exp_helper on a synthetic prepared info is complex,
    # so we run the download+upload inline (mirrors terabox_exp.helper phases).
    try:
        await _run_download(event, surl, pending["base_url"], file_info)
    except Exception as e:
        log.exception("folder pick download failed")
        try:
            await event.respond(f"❌ Download failed: {e}")
        except Exception:
            pass
    finally:
        _PENDING.pop((chat_id, surl), None)


async def _run_download(event, surl: str, base_url: str, file_info: dict, seq_label: str = "") -> None:
    from teraboxDL.public_api import download_terabox_file_experimental
    from terabox.internal_helpers import CancelledError

    chat_id = event.chat_id
    user_mode = "exp"
    task_key = (chat_id, surl)
    cancel_event = threading.Event()
    active_tasks[task_key] = cancel_event
    cancel_btn = [[Button.inline("❌ Cancel", data=f"cancel:{surl}")]]

    info = download_picked(base_url, file_info)
    filename = info["filename"]
    size_str = format_size(info["size"])

    status = await _safe_send(event.respond, f"📦 {seq_label} **{filename}**\n📐 Size: **{size_str}**\n\n⬇️ Downloading… **0%**", buttons=cancel_btn)

    loop = asyncio.get_running_loop()
    dl_start = time.time()
    dl_progress_cb = make_download_progress_cb(status, filename, size_str, loop, cancel_btn)
    try:
        filepath = await asyncio.to_thread(
            download_terabox_file_experimental, info["download_url"], filename, cancel_event, dl_progress_cb)
    except CancelledError:
        await _safe_send(status.edit, "🚫 Cancelled.")
        return
    except TeraBoxError as e:
        await _safe_send(status.edit, f"❌ Download failed: {e}")
        return

    import os as _os
    size_str = format_size(_os.path.getsize(filepath))

    dl_time = time.time() - dl_start

    # Deliver directly (no storage group configured)
    from telegram_logic.bot import bot as _bot
    caption = (f"📦 `{filename}`\n📐 Size: **{size_str}**\n"
               f"⬇️ Download: **{format_duration(dl_time)}**")
    await _safe_send(status.delete)
    await _safe_send(
        _bot.send_file,
        chat_id, filepath,
        caption=caption, supports_streaming=True,
        reply_to=event.message.id if hasattr(event, "message") else None,
    )
    await asyncio.to_thread(add_to_cache, surl, 0, user_mode)  # msg_id unknown; skip caching effect
