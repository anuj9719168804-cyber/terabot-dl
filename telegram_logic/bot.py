import os
import time 
import threading
import asyncio
import logging
from telethon import TelegramClient, Button
from telethon.errors import FloodWaitError

from firebase_db.cache import search_in_cache

from dotenv import load_dotenv
load_dotenv()

log = logging.getLogger(__name__)

from .queue import MessageQueue

# — Concurrency & Flood-Wait Queue ————————————————————————————————————————————
# We still need a semaphore because:
# 1. Unbounded concurrency (e.g. 50 links) will instantly trigger FloodWait before any work gets done.
# 2. Downloading/Uploading 50 videos concurrently will crash a low-spec VPS (OOM or CPU exhaustion).
# 10 is a good high-capacity limit that balances speed with server stability.
terabox_queue = MessageQueue(concurrency_limit=20)

async def _safe_send(*args, **kwargs):
    return await terabox_queue.safe_send(*args, **kwargs)

# — Configuration —————————————————————————————————————————————————————————————
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
APP_ID = int(os.environ.get("APP_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
STORAGE_GROUP_ID = int(os.environ.get("STORAGE_GROUP_ID") or 0)

# — Active-task tracking (for cancel) ————————————————————————————————————————————
active_tasks: dict[tuple[int, str], threading.Event] = {}

# — Download progress registry (for /queue) ————————————————————————————————————
# {(chat_id, surl): {"filename": str, "done": int, "total": int, "started": float}}
download_progress: dict[tuple[int, str], dict] = {}

# — Optional MTProto proxy (needed inside sandboxed egress) ——————————————————
# Reads standard http://user:pass@host:port from MTPROTO_PROXY, else HTTPS_PROXY.
def _mtproto_proxy():
    from urllib.parse import urlparse
    raw = os.environ.get("MTPROTO_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if not raw:
        return None
    u = urlparse(raw)
    if not u.hostname:
        return None
    return ("http", u.hostname, u.port or 3128, True, u.username, u.password)

# — Bot Setup —————————————————————————————————————————————————————————————

bot = TelegramClient(
    "terabox_bot",
    APP_ID,
    API_HASH,
    # infinite retries: ride out egress-proxy blips instead of dying
    # (keeper kept reviving the bot every ~3h after 5 failed attempts)
    connection_retries=None,
    retry_delay=2,
    auto_reconnect=True,
    flood_sleep_threshold=0,
    proxy=_mtproto_proxy(),
)

# — Cache helpers ——————————————————————————————————————————————————————————————

async def _find_cached_video(surl: str, user_mode: str):
    """
    Look up surl in the cache buckets using the priority order for user_mode,
    then fetch the message directly by ID.
    Returns the Telethon Message object if found, otherwise None.
    """
    if not STORAGE_GROUP_ID:
        return None
    msg_id = await asyncio.to_thread(search_in_cache, surl, user_mode)
    if msg_id == -1:
        return None
    try:
        msg = await _safe_send(bot.get_messages, STORAGE_GROUP_ID, ids=msg_id)
        if msg and (msg.video or (
            msg.document
            and msg.document.mime_type
            and "video" in msg.document.mime_type
        )):
            return msg
        return None
    except Exception as e:
        log.warning(f"Cache fetch failed for surl={surl} msg_id={msg_id}: {e}")
        return None
    
async def _pre_upload_file(filepath: str, progress_cb=None):
    """
    Upload a file to Telegram's servers and return a reusable InputFile handle.
    This avoids reading from disk multiple times when sending to both
    storage group and user. The handle is valid for ~24h.
    """
    return await _safe_send(
        bot.upload_file,
        filepath,
        progress_callback=progress_cb,
    )

async def _upload_to_storage(file, filename: str, progress_cb=None):
    """
    Upload a file to the storage group.
    `file` can be a filepath (str) or a pre-uploaded InputFile handle.
    Caption is set to the video filename.
    Returns the sent Message.
    """
    # If it's a raw filepath, upload normally (with progress).
    # If it's an InputFile handle, progress_callback is ignored (already uploaded).
    kwargs = {}
    if isinstance(file, str) and progress_cb:
        kwargs["progress_callback"] = progress_cb

    return await _safe_send(
        bot.send_file,
        STORAGE_GROUP_ID,
        file,
        caption=filename,
        supports_streaming=True,
        **kwargs,
    )


async def _cancellable(coro, cancel_event: threading.Event, poll_interval: float = 0.5):
    """
    Run `coro` as a task while polling `cancel_event` (threading.Event).
    If the event is set, cancel the task immediately.
    Raises asyncio.CancelledError on cancellation.
    """
    task = asyncio.ensure_future(coro)
    while not task.done():
        if cancel_event.is_set():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            raise asyncio.CancelledError("Upload cancelled by user")
        await asyncio.sleep(poll_interval)
    return task.result()

