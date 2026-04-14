#!/usr/bin/env python3
"""
Claude Code Remote Control via Telegram Bot
- Persistent per-user Claude sessions (conversation memory)
- Streaming output to Telegram
- Shell command execution
"""

import os
import sys
import json
import asyncio
import logging
import shutil
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from telegram import Update, BotCommand
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# Suppress noisy HTTP/polling logs from dependencies
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

# ── Config ────────────────────────────────────────────────────────────────────

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
_raw_ids = os.getenv("ALLOWED_USER_IDS", "")
ALLOWED_USERS: set[int] = {int(x) for x in _raw_ids.split(",") if x.strip().isdigit()}
DEFAULT_DIR = os.getenv("WORKING_DIR", str(Path.home()))
CLAUDE_BIN = os.getenv("CLAUDE_PATH") or shutil.which("claude") or "claude"

STREAM_UPDATE_INTERVAL = 2.0  # seconds between Telegram message edits
MAX_MSG_LEN = 4096
CMD_TIMEOUT = 300             # seconds

def _bar(pct: float, width: int = 16) -> str:
    filled = round(min(pct, 100) / 100 * width)
    return "[" + "█" * filled + "░" * (width - filled) + "]"

# ── Per-user state ─────────────────────────────────────────────────────────────

session_cwd: dict[int, str] = {}           # user_id → working directory
session_ids: dict[int, str] = {}           # user_id → claude session_id
active_procs: dict[int, asyncio.subprocess.Process] = {}

# ── Helpers ───────────────────────────────────────────────────────────────────

def authorized(user_id: int) -> bool:
    return not ALLOWED_USERS or user_id in ALLOWED_USERS


def get_cwd(user_id: int) -> str:
    return session_cwd.get(user_id, DEFAULT_DIR)


def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def send_chunks(update: Update, text: str, parse_mode: str | None = None):
    """Send text split into Telegram-safe chunks."""
    text = text.strip() or "(no output)"
    while text:
        chunk, text = text[:MAX_MSG_LEN], text[MAX_MSG_LEN:]
        await update.message.reply_text(chunk, parse_mode=parse_mode)
        if text:
            await asyncio.sleep(0.05)


# ── Claude session runner ─────────────────────────────────────────────────────

async def run_claude_session(
    prompt: str,
    session_id: str | None,
    cwd: str,
    user_id: int,
    status_msg,
) -> tuple[str, str | None]:
    """
    Run `claude -p <prompt> --output-format stream-json [--resume <id>]`.
    Streams partial output to status_msg every STREAM_UPDATE_INTERVAL seconds.
    Returns (full_text, new_session_id).
    """
    args = [CLAUDE_BIN, "-p", prompt, "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions"]
    if session_id:
        args += ["--resume", session_id]

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env={**os.environ, **({"TERM": "dumb"} if sys.platform != "win32" else {})},
        limit=4 * 1024 * 1024,  # 4 MB per line — Claude JSON can be large
    )
    active_procs[user_id] = proc

    text_buf = ""
    new_session_id = session_id
    last_edit = ""

    async def periodic_edit():
        nonlocal last_edit
        while True:
            await asyncio.sleep(STREAM_UPDATE_INTERVAL)
            snippet = text_buf[-MAX_MSG_LEN:].strip()
            if snippet and snippet != last_edit:
                try:
                    await status_msg.edit_text(
                        f"<pre>{escape_html(snippet)}</pre>",
                        parse_mode=ParseMode.HTML,
                    )
                    last_edit = snippet
                except BadRequest:
                    pass

    edit_task = asyncio.create_task(periodic_edit())

    try:
        async def read_stdout():
            nonlocal text_buf, new_session_id
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                try:
                    obj = json.loads(line.decode())
                except json.JSONDecodeError:
                    # Fallback: treat as plain text
                    text_buf += line.decode("utf-8", errors="replace")
                    continue

                msg_type = obj.get("type", "")

                if msg_type == "assistant":
                    for block in obj.get("message", {}).get("content", []):
                        if block.get("type") == "text":
                            text_buf += block["text"]

                elif msg_type == "result":
                    sid = obj.get("session_id")
                    if sid:
                        new_session_id = sid
                    # Use result field as fallback if no streamed text
                    if not text_buf.strip():
                        text_buf = obj.get("result", "")

                # session_id can also appear in system/init messages
                if not new_session_id and "session_id" in obj:
                    new_session_id = obj["session_id"]

        await asyncio.wait_for(read_stdout(), timeout=CMD_TIMEOUT)
        await proc.wait()

    except asyncio.TimeoutError:
        proc.kill()
        text_buf += "\n\n⏱ Timed out."
    finally:
        edit_task.cancel()
        active_procs.pop(user_id, None)

    return text_buf.strip(), new_session_id


# ── Handlers ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    text = (
        "<b>Claude Code Remote Control</b>\n\n"
        "Send any message → Claude Code (persistent session)\n\n"
        "<b>Commands:</b>\n"
        "/new — start a fresh Claude session\n"
        "/session — show current session ID\n"
        "/run &lt;cmd&gt; — run shell command\n"
        "/cd &lt;path&gt; — change directory\n"
        "/pwd — show current directory\n"
        "/cancel — kill running command\n"
        "/exit — shut down bot server\n"
        "/help — show this message"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)


async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not authorized(uid):
        return
    old = session_ids.pop(uid, None)
    await update.message.reply_text(
        "🆕 Started a new Claude session. Previous context cleared."
        + (f"\n<code>old: {old}</code>" if old else ""),
        parse_mode=ParseMode.HTML,
    )


async def cmd_session(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not authorized(uid):
        return
    sid = session_ids.get(uid)
    if sid:
        await update.message.reply_text(
            f"🔖 Session ID:\n<code>{escape_html(sid)}</code>",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text("No active session yet. Send a message to start one.")


async def cmd_pwd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not authorized(uid):
        return
    await update.message.reply_text(
        f"📁 <code>{escape_html(get_cwd(uid))}</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_cd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not authorized(uid):
        return
    path_arg = " ".join(ctx.args) if ctx.args else ""
    if not path_arg:
        await update.message.reply_text("Usage: /cd &lt;path&gt;", parse_mode=ParseMode.HTML)
        return
    base = get_cwd(uid)
    new_path = (Path(path_arg) if Path(path_arg).is_absolute() else Path(base) / path_arg).resolve()
    if not new_path.is_dir():
        await update.message.reply_text(
            f"❌ Not a directory: <code>{escape_html(str(new_path))}</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    session_cwd[uid] = str(new_path)
    await update.message.reply_text(
        f"✅ Now in <code>{escape_html(str(new_path))}</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_exit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not authorized(uid):
        return
    await update.message.reply_text("🛑 Shutting down bot...")
    ctx.application.stop_running()


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not authorized(uid):
        return
    proc = active_procs.get(uid)
    if proc:
        proc.kill()
        await update.message.reply_text("🛑 Cancelled.")
    else:
        await update.message.reply_text("Nothing running.")


_RATE_LIMITS_PATH = Path.home() / ".claude" / "bot_rate_limits.json"


async def cmd_cost(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not authorized(uid):
        return

    if not _RATE_LIMITS_PATH.exists():
        await update.message.reply_text(
            "No rate limit data yet. Send a message first to populate the data."
        )
        return

    try:
        data = json.loads(_RATE_LIMITS_PATH.read_text())
    except Exception:
        await update.message.reply_text("Failed to read rate limit data.")
        return

    lines = ["<b>Claude Code Usage Limits</b>", ""]

    for key, label in (("five_hour", "5-hour"), ("seven_day", "7-day")):
        entry = data.get(key)
        if not entry:
            continue
        pct = entry.get("used_percentage", 0) or 0
        resets_at = entry.get("resets_at")

        countdown = ""
        if resets_at:
            diff = resets_at - int(datetime.now().timestamp())
            if diff > 0:
                h, m = divmod(diff // 60, 60)
                countdown = f"  resets in {h}h {m}m"

        lines += [
            f"<b>{label}</b>",
            f"{_bar(pct)} {pct:.0f}%{countdown}",
            "",
        ]

    updated_at = data.get("updated_at")
    if updated_at:
        age = int(datetime.now().timestamp()) - updated_at
        lines.append(f"<i>updated {age}s ago</i>")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_run(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not authorized(uid):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if uid in active_procs:
        await update.message.reply_text("⚠️ Already running. Use /cancel first.")
        return
    shell_cmd = " ".join(ctx.args) if ctx.args else ""
    if not shell_cmd:
        await update.message.reply_text("Usage: /run &lt;command&gt;", parse_mode=ParseMode.HTML)
        return

    cwd = get_cwd(uid)
    status = await update.message.reply_text(
        f"⚙️ Running: <code>{escape_html(shell_cmd)}</code>",
        parse_mode=ParseMode.HTML,
    )

    proc = await asyncio.create_subprocess_shell(
        shell_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=cwd,
    )
    active_procs[uid] = proc
    buf = ""
    last_edit = ""

    async def periodic_edit():
        nonlocal last_edit
        while True:
            await asyncio.sleep(STREAM_UPDATE_INTERVAL)
            snippet = buf[-MAX_MSG_LEN:].strip()
            if snippet and snippet != last_edit:
                try:
                    await status.edit_text(
                        f"<pre>{escape_html(snippet)}</pre>",
                        parse_mode=ParseMode.HTML,
                    )
                    last_edit = snippet
                except BadRequest:
                    pass

    edit_task = asyncio.create_task(periodic_edit())
    try:
        async def read_all():
            nonlocal buf
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                buf += line.decode("utf-8", errors="replace")
        await asyncio.wait_for(read_all(), timeout=CMD_TIMEOUT)
        await proc.wait()
    except asyncio.TimeoutError:
        proc.kill()
        buf += "\n\n⏱ Timed out."
    finally:
        edit_task.cancel()
        active_procs.pop(uid, None)

    output = buf.strip() or "(no output)"
    full = f"$ {shell_cmd}  [exit {proc.returncode}]\n\n{output}"
    try:
        await status.edit_text(
            f"<pre>{escape_html(full[-MAX_MSG_LEN:])}</pre>",
            parse_mode=ParseMode.HTML,
        )
    except BadRequest:
        pass
    if len(full) > MAX_MSG_LEN:
        await send_chunks(update, output[-(MAX_MSG_LEN - 200):])


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not authorized(uid):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if uid in active_procs:
        await update.message.reply_text("⚠️ Already running. Use /cancel first.")
        return

    prompt = update.message.text.strip()
    if not prompt:
        return

    cwd = get_cwd(uid)
    sid = session_ids.get(uid)
    label = "💬 Continuing session…" if sid else "🤔 Thinking…"
    status = await update.message.reply_text(label)

    async def keep_typing():
        while True:
            try:
                await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
            except Exception:
                pass
            await asyncio.sleep(4)

    typing_task = asyncio.create_task(keep_typing())
    try:
        output, new_sid = await run_claude_session(prompt, sid, cwd, uid, status)
    finally:
        typing_task.cancel()

    if new_sid and new_sid != sid:
        session_ids[uid] = new_sid
        logger.info("User %s session: %s", uid, new_sid)

    try:
        await status.delete()
    except BadRequest:
        pass

    await send_chunks(update, output or "(no output)")


# ── Main ─────────────────────────────────────────────────────────────────────

async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("new", "Start a fresh Claude session"),
        BotCommand("session", "Show current session ID"),
        BotCommand("run", "Run a shell command"),
        BotCommand("cd", "Change working directory"),
        BotCommand("pwd", "Show current directory"),
        BotCommand("cancel", "Cancel running command"),
        BotCommand("cost", "Show API cost usage vs limits"),
        BotCommand("exit", "Shut down bot server"),
        BotCommand("help", "Show help"),
    ])
    logger.info("Bot commands registered.")


def main():
    if not BOT_TOKEN:
        sys.exit("❌ TELEGRAM_BOT_TOKEN is not set. Copy .env.example → .env and fill it in.")

    # Windows requires ProactorEventLoop for subprocess support
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    logger.info("Starting Claude Code Telegram bot…")
    logger.info("Claude binary: %s", CLAUDE_BIN)
    logger.info("Default working dir: %s", DEFAULT_DIR)
    if ALLOWED_USERS:
        logger.info("Allowed user IDs: %s", ALLOWED_USERS)
    else:
        logger.warning("No ALLOWED_USER_IDS set — all users can control this bot!")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("session", cmd_session))
    app.add_handler(CommandHandler("pwd", cmd_pwd))
    app.add_handler(CommandHandler("cd", cmd_cd))
    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("cost", cmd_cost))
    app.add_handler(CommandHandler("exit", cmd_exit))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
