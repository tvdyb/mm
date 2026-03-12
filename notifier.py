"""Telegram notification helpers for async updates."""
import asyncio
import logging
from typing import Optional

from telegram import Bot
from telegram.constants import ParseMode

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger("autopilot.notifier")

_bot: Optional[Bot] = None


def get_bot() -> Bot:
    global _bot
    if _bot is None:
        _bot = Bot(token=TELEGRAM_BOT_TOKEN)
    return _bot


async def send_message(text: str, parse_mode: str = ParseMode.HTML):
    """Send a message to the configured Telegram chat."""
    if not TELEGRAM_CHAT_ID:
        logger.warning("No TELEGRAM_CHAT_ID set, skipping notification")
        return
    try:
        bot = get_bot()
        # Telegram has a 4096 char limit
        if len(text) > 4000:
            text = text[:3997] + "..."
        await bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=text,
            parse_mode=parse_mode,
        )
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")


async def notify_task_started(task_name: str, task_num: int, total: int):
    await send_message(
        f"🚀 <b>Starting task {task_num}/{total}</b>\n"
        f"<code>{task_name}</code>"
    )


async def notify_task_completed(task_name: str, task_num: int, total: int, duration_s: float):
    mins = duration_s / 60
    await send_message(
        f"✅ <b>Completed {task_num}/{total}</b>\n"
        f"<code>{task_name}</code>\n"
        f"⏱ {mins:.1f} min"
    )


async def notify_task_failed(task_name: str, error: str, attempts: int, max_retries: int):
    await send_message(
        f"❌ <b>Task failed</b> (attempt {attempts}/{max_retries})\n"
        f"<code>{task_name}</code>\n\n"
        f"<pre>{error[:1500]}</pre>"
    )


async def notify_paused(reason: str, progress_bar: str):
    await send_message(
        f"⏸ <b>Autopilot paused</b>\n"
        f"{progress_bar}\n\n"
        f"Reason: {reason}\n\n"
        f"Use /resume to continue or /status for details."
    )


async def notify_completed(project_name: str, total_tasks: int, total_cost: float, total_time_s: float):
    hours = total_time_s / 3600
    await send_message(
        f"🎉 <b>Project complete!</b>\n"
        f"<code>{project_name}</code>\n\n"
        f"📋 {total_tasks} tasks completed\n"
        f"💰 ${total_cost:.2f} estimated cost\n"
        f"⏱ {hours:.1f} hours"
    )


async def notify_progress(progress_bar: str, current_task: str, stats: dict):
    await send_message(
        f"📊 <b>Progress update</b>\n"
        f"{progress_bar}\n\n"
        f"Current: <code>{current_task}</code>\n"
        f"✅ {stats.get('completed', 0)} done | "
        f"❌ {stats.get('failed', 0)} failed | "
        f"⏳ {stats.get('pending', 0)} remaining\n"
        f"💰 ${stats.get('cost', 0):.2f} so far"
    )


def send_sync(text: str):
    """Synchronous wrapper for sending messages (for use in non-async contexts)."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(send_message(text))
        else:
            loop.run_until_complete(send_message(text))
    except RuntimeError:
        asyncio.run(send_message(text))
