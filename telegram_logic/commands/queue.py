"""
telegram_logic/commands/queue.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
/queue — show active downloads with live progress.
"""
import logging
import time

from telethon import events

from telegram_logic.bot import bot, download_progress
from telegram_logic.helpers import format_size

log = logging.getLogger(__name__)


def _fmt_eta(done: int, total: int, started: float) -> str:
    elapsed = max(time.time() - started, 1)
    speed = done / elapsed  # bytes/sec
    if total and total > done and speed > 0:
        eta = (total - done) / speed
        if eta < 60:
            return f"{eta:.0f} dtk"
        if eta < 3600:
            return f"{eta/60:.0f} mnt"
        return f"{eta/3600:.1f} jam"
    return "—"


@bot.on(events.NewMessage(pattern=r"^/queue$"))
async def queue_cmd(event):
    if not download_progress:
        await event.respond("📭 Ga ada download yang jalan sekarang.")
        return
    lines = ["📥 **Download aktif:**"]
    for (chat_id, surl), p in download_progress.items():
        total = p.get("total") or 0
        done = p.get("done") or 0
        pct = (done / total * 100) if total else 0
        bar = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
        lines.append(
            f"\n📦 `{p.get('filename', '?')}`\n"
            f"`{bar}` {pct:.0f}% — {format_size(done)}"
            + (f" / {format_size(total)}" if total else "") + "\n"
            f"⏳ ETA: {_fmt_eta(done, total, p.get('started', time.time()))}"
        )
    await event.respond("\n".join(lines))
