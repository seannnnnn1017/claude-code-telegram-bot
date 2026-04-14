# Claude Code Telegram Bot

Control [Claude Code](https://claude.ai/code) remotely via Telegram. Send prompts, run shell commands, and manage Claude sessions from your phone.

**Supports: Windows · macOS · Linux**

---

## Features

- **Persistent sessions** — Claude remembers the conversation across messages
- **Streaming output** — responses update in real-time as Claude types
- **Shell commands** — run any shell command via `/run`
- **Directory navigation** — `/cd` and `/pwd`
- **Session management** — `/new` to start fresh, `/session` to inspect
- **Rate limit display** — `/cost` shows your 5-hour and 7-day usage (requires setup below)
- **User allowlist** — restrict access to specific Telegram user IDs

---

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.11+ | `python --version` |
| Claude Code CLI | latest | `claude --version` |
| Telegram Bot Token | — | from [@BotFather](https://t.me/BotFather) |
| `jq` (optional) | any | only needed for `/cost` |

### Install Claude Code CLI

```bash
npm install -g @anthropic-ai/claude-code
```

> First-time setup: run `claude` once interactively and log in before using the bot.

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/seannnnnn1017/claude-code-telegram-bot.git
cd claude-code-telegram-bot
```

### 2. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 3. Create your `.env` file

```bash
cp .env.example .env
```

Edit `.env`:

```env
# Required: your bot token from @BotFather
TELEGRAM_BOT_TOKEN=1234567890:ABCDefGhIJKlmNoPQRsTUVwxyZ

# Optional: comma-separated Telegram user IDs allowed to use the bot
# Leave empty to allow ALL users (not recommended on a public server)
# Get your ID from @userinfobot
ALLOWED_USER_IDS=123456789

# Optional: default working directory for Claude Code
# Leave empty to use your home directory
# Windows: WORKING_DIR=C:/Users/yourname/projects
# macOS:   WORKING_DIR=/Users/yourname/projects
WORKING_DIR=

# Optional: full path to the claude binary (leave empty to auto-detect)
CLAUDE_PATH=
```

### 4. Run the bot

```bash
python bot.py
```

You should see:
```
[INFO] Starting Claude Code Telegram bot…
[INFO] Claude binary: /usr/local/bin/claude
[INFO] Default working dir: /Users/yourname
[INFO] Bot commands registered.
```

Open Telegram, find your bot, and send any message to start a Claude session.

---

## Platform Notes

### Windows

The bot uses `asyncio.WindowsProactorEventLoopPolicy` automatically — no extra setup needed.

The `/run` command executes via `cmd.exe`. Use Windows-style commands:
```
/run dir
/run type file.txt
```

**Claude Code path** is usually auto-detected. If not, set it in `.env`:
```env
CLAUDE_PATH=C:/Users/yourname/AppData/Local/Microsoft/WinGet/Packages/Anthropic.ClaudeCode_.../claude.EXE
```

### macOS / Linux

No platform-specific setup needed. The bot uses your login shell for `/run`.

---

## Commands

| Command | Description |
|---|---|
| `<any message>` | Send a prompt to Claude (continues current session) |
| `/new` | Start a fresh Claude session (clears context) |
| `/session` | Show the current session ID |
| `/run <cmd>` | Execute a shell command in the current directory |
| `/cd <path>` | Change working directory |
| `/pwd` | Show current working directory |
| `/cancel` | Kill the currently running command |
| `/cost` | Show Claude rate limit usage (see setup below) |
| `/exit` | Shut down the bot server |
| `/help` | Show help |

---

## `/cost` Setup (Rate Limit Display)

`/cost` shows your 5-hour and 7-day Claude usage bars. It reads from `~/.claude/bot_rate_limits.json`, which is populated by Claude Code's statusline hook.

This requires Claude Code to be run **interactively** at least once after setup — the bot reads the cached data on demand.

### macOS / Linux

Add this to your Claude Code statusline script (or create `~/.claude/statusline.sh`):

```bash
#!/bin/bash
input=$(cat)

# Save rate limits for the Telegram bot
echo "$input" | jq -c '{
  five_hour:  .rate_limits.five_hour,
  seven_day:  .rate_limits.seven_day,
  updated_at: (now | floor)
}' > ~/.claude/bot_rate_limits.json 2>/dev/null || true

# Continue with your existing statusline command here...
```

Then in `~/.claude/settings.json`:
```json
{
  "statusLine": {
    "type": "command",
    "command": "bash ~/.claude/statusline.sh"
  }
}
```

### Windows

A wrapper script is provided. Run once to set it up:

**Step 1** — The file `~/.claude/statusline_wrapper.sh` should already exist if you followed the setup above. If not, create it:

```bash
cat > ~/.claude/statusline_wrapper.sh << 'EOF'
#!/bin/bash
input=$(cat)

echo "$input" | jq -c '{
  five_hour:  .rate_limits.five_hour,
  seven_day:  .rate_limits.seven_day,
  updated_at: (now | floor)
}' > "${CLAUDE_CONFIG_DIR:-$HOME/.claude}/bot_rate_limits.json" 2>/dev/null || true

plugin_dir=$(ls -d "${CLAUDE_CONFIG_DIR:-$HOME/.claude}"/plugins/cache/claude-hud/claude-hud/*/ 2>/dev/null | awk -F/ '{ print $(NF-1) "\t" $0 }' | sort -t. -k1,1n -k2,2n -k3,3n -k4,4n | tail -1 | cut -f2-)
echo "$input" | "/c/Program Files/nodejs/node" "${plugin_dir}dist/index.js"
EOF
```

**Step 2** — In `~/.claude/settings.json`, set:
```json
{
  "statusLine": {
    "type": "command",
    "command": "bash ~/.claude/statusline_wrapper.sh"
  }
}
```

> **Note:** `jq` must be installed. On Windows, install via [winget](https://github.com/jqlang/jq): `winget install jqlang.jq`

After setup, open Claude Code interactively once, then `/cost` in Telegram will work.

---

## Security

- Set `ALLOWED_USER_IDS` in `.env` to restrict access. Without it, **anyone** who finds your bot can control your machine.
- The bot runs with `--dangerously-skip-permissions`, meaning Claude can read/write/execute without confirmation prompts.
- Never share your `.env` file or commit it to version control (it's in `.gitignore`).

---

## Troubleshooting

**`Conflict: terminated by other getUpdates request`**
Another bot instance is already running. Find and kill it:
```bash
# macOS/Linux
pkill -f "python bot.py"

# Windows
tasklist | findstr python
taskkill /PID <pid> /F
```

**`NotADirectoryError` / `WinError 267`**
Your `WORKING_DIR` in `.env` is set to an invalid path. Set it to a real directory or leave it empty.

**`/cost` says "No rate limit data yet"**
Open Claude Code interactively (`claude` in terminal) — the statusline hook needs to run at least once to create `~/.claude/bot_rate_limits.json`.

**Claude binary not found**
Set `CLAUDE_PATH` in `.env` to the full path of your `claude` executable.
