"""Telegram bot — remote control for Claude Autopilot."""
import asyncio
import logging
import time
from pathlib import Path

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TASKS_DIR, LOGS_DIR
from orchestrator import orchestrator
from planner import plan_project, suggest_fix
from state import ProjectState

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("autopilot.bot")

# Track the orchestrator task so we can manage it
_orchestrator_task: asyncio.Task | None = None


# ─── Helpers ────────────────────────────────────────────────────────────────


def authorized(func):
    """Decorator to restrict commands to the configured chat ID."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if TELEGRAM_CHAT_ID and str(update.effective_chat.id) != str(TELEGRAM_CHAT_ID):
            await update.message.reply_text("⛔ Unauthorized.")
            return
        return await func(update, context)
    return wrapper


def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ─── Commands ───────────────────────────────────────────────────────────────


@authorized
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start executing from the loaded/resumed project."""
    global _orchestrator_task

    # Try to resume existing state, or check if a YAML was provided
    if context.args:
        yaml_path = " ".join(context.args)
        if not Path(yaml_path).exists():
            # Check tasks directory
            candidate = TASKS_DIR / yaml_path
            if not candidate.exists():
                candidate = TASKS_DIR / f"{yaml_path}.yaml"
            if candidate.exists():
                yaml_path = str(candidate)
            else:
                await update.message.reply_text(f"❌ File not found: {yaml_path}")
                return
        orchestrator.load_project(yaml_path)
    else:
        state = orchestrator.resume_project()
        if state is None:
            await update.message.reply_text(
                "No project loaded. Use:\n"
                "• /plan <description> — to create a task plan\n"
                "• /start <path-to-tasks.yaml> — to load a specific plan"
            )
            return

    state = orchestrator.state
    counts = state.count_by_status()
    await update.message.reply_text(
        f"🚀 <b>Starting: {escape_html(state.project_name)}</b>\n"
        f"{state.progress_bar()}\n"
        f"📋 {len(state.tasks)} tasks | "
        f"✅ {counts['completed']} done | ⏳ {counts['pending']} pending\n"
        f"📂 {escape_html(state.repo_path)}",
        parse_mode=ParseMode.HTML,
    )

    # Run orchestrator in background
    _orchestrator_task = asyncio.create_task(orchestrator.run())


@authorized
async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Use DeepSeek to decompose a project description into tasks."""
    if not context.args:
        await update.message.reply_text(
            "Usage: /plan <project description>\n\n"
            "Optionally add repo path:\n"
            "/plan Build a SaaS app | /Users/wilson/projects/my-app"
        )
        return

    text = " ".join(context.args)
    parts = text.split("|")
    description = parts[0].strip()
    repo_path = parts[1].strip() if len(parts) > 1 else f"/Users/wilson/projects/{description.lower().replace(' ', '-')[:30]}"

    await update.message.reply_text("🧠 Planning with DeepSeek...\nThis may take 30-60 seconds.")

    try:
        yaml_content, yaml_path, task_count = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: plan_project(description, repo_path),
        )
        await update.message.reply_text(
            f"✅ <b>Plan created!</b>\n\n"
            f"📋 {task_count} subtasks\n"
            f"📂 Repo: <code>{escape_html(repo_path)}</code>\n"
            f"📄 Saved: <code>{escape_html(yaml_path)}</code>\n\n"
            f"Use /start to begin execution.\n"
            f"Use /tasks to preview the task list.",
            parse_mode=ParseMode.HTML,
        )
        # Pre-load it
        orchestrator.load_project(yaml_path)
    except Exception as e:
        await update.message.reply_text(f"❌ Planning failed: {escape_html(str(e))}")


@authorized
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get current project status."""
    state = orchestrator.state
    if not state:
        await update.message.reply_text("No project loaded. Use /plan or /start.")
        return

    counts = state.count_by_status()
    current = state.get_current_task()

    elapsed = ""
    if state.started_at:
        hours = (time.time() - state.started_at) / 3600
        elapsed = f"\n⏱ Running for {hours:.1f} hours"

    msg = (
        f"📊 <b>{escape_html(state.project_name)}</b> — {state.status.upper()}\n"
        f"{state.progress_bar()}\n\n"
        f"✅ {counts['completed']} completed\n"
        f"🔄 {counts['running']} running\n"
        f"❌ {counts['failed']} failed\n"
        f"⏳ {counts['pending']} pending\n"
        f"⏭ {counts['skipped']} skipped\n"
        f"💰 ${state.total_cost:.2f} estimated cost"
        f"{elapsed}"
    )
    if current:
        msg += f"\n\n🔨 Current: <code>{escape_html(current.name)}</code> (attempt {current.attempts})"

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


@authorized
async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all tasks with status indicators."""
    state = orchestrator.state
    if not state:
        await update.message.reply_text("No project loaded.")
        return

    icons = {
        "pending": "⏳",
        "running": "🔄",
        "completed": "✅",
        "failed": "❌",
        "skipped": "⏭",
    }

    lines = [f"📋 <b>Tasks for {escape_html(state.project_name)}</b>\n"]
    for i, tid in enumerate(state.task_order, 1):
        ts = state.tasks[tid]
        icon = icons.get(ts.status, "❓")
        extra = ""
        if ts.status == "failed":
            extra = f" — {escape_html(ts.error[:60])}" if ts.error else ""
        elif ts.status == "completed" and ts.completed_at and ts.started_at:
            mins = (ts.completed_at - ts.started_at) / 60
            extra = f" ({mins:.1f}m)"
        lines.append(f"{icon} {i}. <code>{escape_html(ts.name)}</code>{extra}")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3997] + "..."
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


@authorized
async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pause after current subtask completes."""
    orchestrator.pause()
    await update.message.reply_text("⏸ Pausing after current task completes...")


@authorized
async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resume execution."""
    global _orchestrator_task
    state = orchestrator.resume_project()
    if not state:
        await update.message.reply_text("Nothing to resume.")
        return

    await update.message.reply_text(
        f"▶️ Resuming <b>{escape_html(state.project_name)}</b>\n{state.progress_bar()}",
        parse_mode=ParseMode.HTML,
    )
    _orchestrator_task = asyncio.create_task(orchestrator.run())


@authorized
async def cmd_abort(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Abort immediately."""
    orchestrator.abort_now()
    await update.message.reply_text("🛑 Aborting... Current Claude Code process will be killed.")


@authorized
async def cmd_inject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inject instructions into the next subtask."""
    if not context.args:
        await update.message.reply_text("Usage: /inject <instructions for next task>")
        return
    instructions = " ".join(context.args)
    orchestrator.inject(instructions)
    await update.message.reply_text(
        f"💉 Injected into next task:\n<pre>{escape_html(instructions[:500])}</pre>",
        parse_mode=ParseMode.HTML,
    )


@authorized
async def cmd_log(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Get recent Claude Code output."""
    state = orchestrator.state
    if not state:
        await update.message.reply_text("No project loaded.")
        return

    # Find the most recent log
    current = state.get_current_task()
    if not current:
        # Check last completed/failed task
        for tid in reversed(state.task_order):
            ts = state.tasks[tid]
            if ts.output_log:
                current = ts
                break

    if not current or not current.output_log:
        await update.message.reply_text("No logs available yet.")
        return

    log_path = Path(current.output_log)
    if not log_path.exists():
        await update.message.reply_text("Log file not found.")
        return

    content = log_path.read_text()
    full = context.args and context.args[0] == "full"

    if full:
        # Send as file
        await update.message.reply_document(
            document=open(log_path, "rb"),
            filename=log_path.name,
            caption=f"Full log for: {current.name}",
        )
    else:
        # Last 50 lines
        lines = content.split("\n")
        tail = "\n".join(lines[-50:])
        await update.message.reply_text(
            f"📜 <b>Last 50 lines</b> — <code>{escape_html(current.name)}</code>\n\n"
            f"<pre>{escape_html(tail[:3500])}</pre>\n\n"
            f"Use /log full for the complete output.",
            parse_mode=ParseMode.HTML,
        )


@authorized
async def cmd_retry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Retry the last failed task."""
    global _orchestrator_task
    state = orchestrator.state
    if not state:
        await update.message.reply_text("No project loaded.")
        return

    # Find last failed task
    for tid in state.task_order:
        ts = state.tasks[tid]
        if ts.status == "failed":
            ts.status = "pending"
            ts.attempts = 0  # reset retry counter
            state.save()
            await update.message.reply_text(
                f"🔄 Retrying: <code>{escape_html(ts.name)}</code>",
                parse_mode=ParseMode.HTML,
            )
            _orchestrator_task = asyncio.create_task(orchestrator.run())
            return

    await update.message.reply_text("No failed tasks to retry.")


@authorized
async def cmd_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Skip a failed task and continue."""
    global _orchestrator_task
    state = orchestrator.state
    if not state:
        await update.message.reply_text("No project loaded.")
        return

    for tid in state.task_order:
        ts = state.tasks[tid]
        if ts.status == "failed":
            ts.status = "skipped"
            state.save()
            await update.message.reply_text(
                f"⏭ Skipped: <code>{escape_html(ts.name)}</code>\nResuming...",
                parse_mode=ParseMode.HTML,
            )
            _orchestrator_task = asyncio.create_task(orchestrator.run())
            return

    await update.message.reply_text("No failed tasks to skip.")


@authorized
async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Edit a task's prompt. Usage: /edit <task_id> <new prompt>"""
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Usage: /edit <task_id> <new prompt>")
        return

    task_id = context.args[0]
    new_prompt = " ".join(context.args[1:])

    if task_id in orchestrator._task_prompts:
        orchestrator._task_prompts[task_id]["prompt"] = new_prompt
        if orchestrator.state and task_id in orchestrator.state.tasks:
            ts = orchestrator.state.tasks[task_id]
            if ts.status == "failed":
                ts.status = "pending"
                ts.attempts = 0
                orchestrator.state.save()
        await update.message.reply_text(
            f"✏️ Updated prompt for <code>{escape_html(task_id)}</code>",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(f"❌ Task '{task_id}' not found.")


@authorized
async def cmd_cost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show cost tracking and budget status."""
    state = orchestrator.state
    if not state:
        await update.message.reply_text("No project loaded.")
        return

    budget = orchestrator.budget
    budget_check = budget.check_budget()

    elapsed = ""
    if state.started_at:
        hours = (time.time() - state.started_at) / 3600
        if hours > 0:
            rate = budget.total_cost / hours
            elapsed = f"\n⏱ {hours:.1f} hours | 🔥 ${rate:.2f}/hr burn rate"

    await update.message.reply_text(
        f"💰 <b>Cost &amp; Budget</b>\n\n"
        f"Spent: ${budget.total_cost:.2f}\n"
        f"Task budget: ${budget.task_budget:.2f}/task\n"
        f"Project budget: ${budget.project_budget:.2f}\n"
        f"Hourly cap: ${budget.hourly_budget:.2f}/hr\n"
        f"{budget_check.format_telegram()}"
        f"{elapsed}",
        parse_mode=ParseMode.HTML,
    )


@authorized
async def cmd_fix(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Use DeepSeek to suggest a fix for the last failed task."""
    state = orchestrator.state
    if not state:
        await update.message.reply_text("No project loaded.")
        return

    for tid in state.task_order:
        ts = state.tasks[tid]
        if ts.status == "failed":
            await update.message.reply_text("🧠 Asking DeepSeek for a fix suggestion...")
            try:
                error_text = ts.error or "Unknown error"
                if ts.output_log and Path(ts.output_log).exists():
                    error_text = Path(ts.output_log).read_text()[-2000:]
                suggestion = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: suggest_fix(ts.name, error_text),
                )
                await update.message.reply_text(
                    f"💡 <b>Fix suggestion for</b> <code>{escape_html(ts.name)}</code>:\n\n"
                    f"{escape_html(suggestion)}\n\n"
                    f"Use /edit {escape_html(tid)} &lt;new prompt&gt; to update, then /retry.",
                    parse_mode=ParseMode.HTML,
                )
            except Exception as e:
                await update.message.reply_text(f"❌ DeepSeek error: {escape_html(str(e))}")
            return

    await update.message.reply_text("No failed tasks to analyze.")


@authorized
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show available commands."""
    await update.message.reply_text(
        "🤖 <b>Claude Autopilot v2 Commands</b>\n\n"
        "<b>Planning:</b>\n"
        "/plan &lt;description&gt; — DeepSeek task decomposition\n"
        "/plan &lt;desc&gt; | &lt;repo_path&gt; — With custom repo path\n\n"
        "<b>Execution:</b>\n"
        "/start — Begin from first incomplete task\n"
        "/start &lt;tasks.yaml&gt; — Load and start a specific plan\n"
        "/pause — Pause after current task\n"
        "/resume — Resume execution\n"
        "/abort — Stop immediately\n\n"
        "<b>Monitoring:</b>\n"
        "/status — Progress and current state\n"
        "/tasks — List all tasks with status\n"
        "/log — Last 50 lines of output\n"
        "/log full — Full output as file\n"
        "/cost — Budget tracking and burn rate\n\n"
        "<b>Recovery:</b>\n"
        "/retry — Retry last failed task\n"
        "/skip — Skip failed task, continue\n"
        "/fix — DeepSeek fix suggestion\n"
        "/edit &lt;id&gt; &lt;prompt&gt; — Edit a task prompt\n"
        "/inject &lt;msg&gt; — Add instructions to next task\n\n"
        "<b>v2 Features:</b>\n"
        "• CLAUDE.md auto-maintained for context continuity\n"
        "• Parallel execution via git worktrees\n"
        "• Adaptive replanning every N tasks\n"
        "• Cost guardrails with auto-pause\n"
        "• Semantic review (DeepSeek checks output vs spec)\n"
        "• Robust fix loops (piped context, no --resume)",
        parse_mode=ParseMode.HTML,
    )


# ─── Main ───────────────────────────────────────────────────────────────────


def main():
    """Start the Telegram bot."""
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set in .env")
        return

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("abort", cmd_abort))
    app.add_handler(CommandHandler("inject", cmd_inject))
    app.add_handler(CommandHandler("log", cmd_log))
    app.add_handler(CommandHandler("retry", cmd_retry))
    app.add_handler(CommandHandler("skip", cmd_skip))
    app.add_handler(CommandHandler("edit", cmd_edit))
    app.add_handler(CommandHandler("cost", cmd_cost))
    app.add_handler(CommandHandler("fix", cmd_fix))
    app.add_handler(CommandHandler("help", cmd_help))

    print("🤖 Claude Autopilot bot starting...")
    print(f"   Chat ID filter: {TELEGRAM_CHAT_ID or 'NONE (open to all!)'}")
    print(f"   Tasks dir: {TASKS_DIR}")
    print(f"   Logs dir: {LOGS_DIR}")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
