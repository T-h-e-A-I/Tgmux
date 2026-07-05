# Tgmux

Control multiple interactive **Claude Code** agents from **Telegram**. Each agent lives in its own tmux session on your VM; Tgmux bridges the terminal to your phone — you send messages, the agent's output is cleaned and relayed back, blocking prompts ping you, and shipping (`git push` → Vercel preview → production) is gated behind explicit commands and confirm buttons.

```
Telegram ⇄ Tgmux daemon ⇄ tmux sessions (claude REPL, one per project)
                          └─ gh / vercel for the ship pipeline
```

## Features

- 🤖 Spawn / adopt / kill Claude Code agents per project, switch between them
- 🔄 Bidirectional bridge: plain text goes to the agent, its responses come back cleaned (no TUI noise), coalesced and throttled
- 🔶 Blocking prompts (permission dialogs, menus, y/n) ping you and mark the agent *waiting*
- 📎 Attach files/photos in Telegram → saved into the project, agent is told
- 🎛 Remote control of the Claude Code TUI: cycle modes (plan / accept-edits), Escape, raw keys, slash-command passthrough (`/compact`, `/model`, custom skills)
- 🚀 Gated shipping: `/push` commits to `dev` and returns a Vercel **preview** URL; `/deploy` needs a confirm button before merging `dev`→`prod` and going to **production**
- 🔐 Single-owner: the bot answers exactly one Telegram user ID, everyone else is silently ignored

## Prerequisites

On the VM (tested on Ubuntu 24.04, Python 3.12):

- `tmux`
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) — `claude` installed and logged in
- [GitHub CLI](https://cli.github.com/) — `gh auth login` done (only needed for `/push`)
- [Vercel CLI](https://vercel.com/docs/cli) — `vercel login` done (only needed for `/push`/`/deploy`)
- `python3-venv` (`apt install python3.12-venv`)

And from Telegram:

- A bot token: talk to **@BotFather** → `/newbot`
- Your numeric user ID: message **@userinfobot**

## Setup

```bash
git clone https://github.com/T-h-e-A-I/Tgmux.git
cd Tgmux

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env      # fill in TELEGRAM_BOT_TOKEN + TELEGRAM_OWNER_ID
chmod 600 .env
mkdir -p /root/projects   # or set PROJECTS_DIR in .env

# run in foreground to test
.venv/bin/python -m orchestrator

# or install as a service
cp tgmux.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now tgmux
```

Message your bot `/help` — if it answers, the bridge is up.

## Commands

| Command | Action |
|---|---|
| 📁 `/ls [path]` | list the projects dir |
| 🆕 `/mkdir <name>` | create a project folder |
| ✨ `/new <name> [api]` | spawn an interactive Claude Code agent (add `api` to bill via `ANTHROPIC_API_KEY` instead of the subscription) |
| 🔗 `/adopt <name> <path> [tmux-session]` | agent in an existing folder, or bridge an already-running tmux session |
| 🧟 `/revive <name> [path]` | respawn a killed agent with its previous conversation (`claude --continue`) |
| 📋 `/list` | agents + status (🛠 building · 🔶 waiting for you · 😴 idle · 💀 dead) with tap buttons: 🎯 switch · 👀 screen · 🗡 kill |
| 🎯 `/switch <name>` | route plain text to this agent |
| 👀 `/status [name]` | current cleaned pane tail |
| 💬 `/say <name> <msg>` | message a specific agent without switching |
| 🔇 `/pause` / 🔊 `/resume <name>` | mute/unmute progress relay (questions always ping) |
| 🔁 `/mode [name] [normal\|auto\|plan]` | set Claude Code mode directly — reads the status line and Shift+Tabs until it matches; bare `/mode` shows a button per mode with the current one marked |
| 🛑 `/esc [name]` | interrupt the agent (Escape) |
| ⌨️ `/key <name> <key>` | send a raw key (Enter, Up, Down, Tab, 1, C-c …) |
| 🌐 `/port <name>` | dev-server URL (`http://VM_IP:port`) |
| 📤 `/push <name>` | commit + push `dev` (creates the GitHub repo if needed) → Vercel **preview** URL |
| 🔀 `/merge <name> [src] [dst]` | git-only release: merge + push, no Vercel (default `dev` → `main`, confirm button) |
| 🟢 `/deploy <name>` | confirm button → merge `dev`→`prod` → Vercel **production** |
| 🗡 `/kill <name>` | tear down the tmux session (files kept) |
| 💀 `/killall` | tear down ALL agents (confirm button) |
| ♻️ `/restart` | restart the daemon (agents survive in tmux) |
| ⏱ `/uptime` | daemon uptime |

Routing: plain text → active agent · `@name text` → that agent · replying to an agent's message → that agent. Any unrecognized `/command` (e.g. `/compact`, `/model`, custom skills) is passed through to the active agent. Attach a file/photo and it's saved to the project's `incoming/` dir (auto-added to the project's `.gitignore` — attachments are for the agent, never published); the caption becomes the instruction.

## How the bridge works

- Each agent = one tmux session `proj-<slug>` running the interactive `claude` REPL, spawned with `PORT=<allocated>` (pool 3001–3099) and a house-rules `CLAUDE.md` in the project root.
- Your Telegram text becomes `tmux send-keys`; the daemon polls `capture-pane` every 2 s, cleans the rendered pane (ANSI, TUI chrome, spinners, input-box echoes), diffs it against the last relayed snapshot, and relays only new content — coalesced and throttled so you aren't spammed.
- The snapshot baseline is persisted per agent, so a daemon restart never swallows or repeats output.
- Question detection watches the stable pane tail for blocking prompts (`Do you want…`, `(y/n)`, numbered menus). On a hit you get a 🔶 ping with quick-answer buttons (1️⃣ 2️⃣ 3️⃣ · ✅ Enter · 🛑 Esc · 👀 Screen) and the agent is marked `WAITING_FOR_ME`; replying to that message also routes straight back.
- Sessions live in tmux, not the daemon: restart the daemon and it reattaches.

Design note: raw `pipe-pane` streaming isn't relayed because Claude Code is a full-screen TUI — its output stream is redraw noise. The rendered `capture-pane` diff is what you see; `pipe-pane` still runs, feeding raw per-agent audit logs in `logs/`.

## Safety

- Owner-ID allowlist on every handler; non-owners are ignored silently.
- Prod deploys require `/deploy` **and** a confirm button — agents can't self-deploy (the injected `CLAUDE.md` also tells them not to).
- All commands, pushes, and deploys are appended to `logs/audit.log` (JSONL).
- Secrets live in `.env` (`chmod 600`), never in git; `state.db` and `logs/` are gitignored.
- Headless `claude -p` is used only for commit messages, with an empty tool allowlist.

## Not yet built (Plan §11 later slices)

Budget caps / cost tracking from JSONL transcripts, dead-man's-switch health ping, Caddy reverse proxy, Agent-SDK structured approvals. The plan (§9.5) recommends a dedicated non-root user — uncomment `User=` in the unit once created.
