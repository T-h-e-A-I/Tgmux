# plan.md — Telegram-Controlled Claude Code Orchestrator

**Codename:** (working title — call it whatever, e.g. `tg-orchestrator`, `agentctl`, `remote-cc`)
**Owner:** Awesh
**Goal in one line:** Talk to multiple Claude Code agents living on my VM from Telegram, with minimal interruption — they build, ask, brainstorm, and (on my approval) push to GitHub and deploy to Vercel.

---

## 0. The core architectural decision (read this first)

There are two ways to run Claude Code on the VM, and this project needs **both**:

| Mode | What it is | Used for |
|---|---|---|
| **Interactive session in tmux** | Long-lived `claude` REPL running in a tmux window. Stays alive, asks clarifying questions, takes follow-up messages, brainstorms. | The **main build loop** — the "living agent I chat with via Telegram." This is the heart of the product. |
| **Headless (`claude -p`)** | Batch: one prompt in, one result out, then exits. Non-interactive. | The **deterministic automation** — commit summaries, changelog, "should this deploy" checks, one-shot scripted tasks. NOT the chat loop. |

**Why this matters:** My requirement — agents that keep running, ask questions, and take my replies — is fundamentally an *interactive session*, not a headless batch job. Headless `-p` has no follow-up turn. So the design is: **persistent interactive Claude Code sessions in tmux, bridged to Telegram**, with headless `-p` used only for the scripted git/deploy steps.

**Cost note:** A Max subscription comfortably covers ~1–3 steady interactive agents. For 5+ parallel agents or overnight bursts, switch that agent's session to an `ANTHROPIC_API_KEY` (pay-as-you-go) instead of subscription auth. Design the config so each agent can independently use subscription auth OR an API key.

---

## 1. Objectives & non-goals

### Objectives (v-by-v)
1. From Telegram, run `ls` in a base directory.
2. From Telegram, `mkdir` a new project folder.
3. From Telegram, pick a folder → spawn an interactive Claude Code session in tmux rooted at that folder.
4. That session builds like normal CC: asks questions, brainstorms, sends updates — all relayed through Telegram, and I reply through Telegram.
5. The session detects occupied ports and serves the WIP app on a free port; it tells me the URL/IP:port so I can monitor.
6. Run **multiple agents in parallel**, toggle/switch between them from Telegram.
7. On my explicit approval, the agent pushes to a `dev` branch (creating the GitHub repo if needed) and a **Vercel preview** deploy happens automatically.
8. On my explicit **`deploy`** command, merge `dev` → `prod` and trigger the **Vercel production** deploy.

### Non-goals (for now)
- No web dashboard/UI (Telegram is the only control plane).
- No fully autonomous production deploys (prod is always gated behind my `deploy` command).
- No multi-user support (only *my* Telegram user ID is authorized).
- No fancy voice/media — text-first.

---

## 2. High-level architecture

```
                 ┌─────────────────────────────────────────────┐
                 │                  My phone                     │
                 │                Telegram app                   │
                 └───────────────────┬───────────────────────────┘
                                     │  (Bot API: long-poll or webhook)
                                     ▼
┌──────────────────────────────────────────────────────────────────────┐
│                              THE VM                                     │
│                                                                        │
│   ┌────────────────────────────────────────────────────────────────┐ │
│   │                    Orchestrator daemon (Python)                  │ │
│   │  - Telegram bot handler (auth: only my user ID)                  │ │
│   │  - Command router  (/ls /mkdir /new /list /switch /deploy ...)   │ │
│   │  - Session manager (spawn/kill/list tmux sessions)               │ │
│   │  - I/O bridge      (tmux <-> Telegram, both directions)          │ │
│   │  - Question detector (spot when an agent is waiting on me)       │ │
│   │  - Port allocator  (find free port, track per project)           │ │
│   │  - Git/Vercel ops  (via headless `claude -p` or direct CLI)      │ │
│   │  - State store     (JSON/SQLite: sessions, active agent, ports)  │ │
│   └───────────────┬──────────────────────────┬─────────────────────┘ │
│                   │                          │                        │
│         tmux (session per agent)     GitHub CLI (gh) + Vercel CLI     │
│   ┌───────────────┴───────┐  ┌───────┴───────┐        (creds on VM)   │
│   │ tmux: proj-offerkoi    │  │ tmux: proj-x  │  ...                   │
│   │  └ claude (interactive)│  │  └ claude ... │                        │
│   │  └ dev server :3001    │  │  └ dev :3002  │                        │
│   └────────────────────────┘  └───────────────┘                        │
└──────────────────────────────────────────────────────────────────────┘
```

**Key idea:** the orchestrator daemon *owns* the tmux sessions. It never makes me SSH in. Telegram messages become tmux keystrokes; tmux pane output becomes Telegram messages.

---

## 3. Tech stack (recommended)

| Concern | Choice | Why |
|---|---|---|
| Language | **Python 3.11+** | Fast to write, great Telegram + subprocess libs, you already use it. |
| Telegram lib | **`python-telegram-bot` v21+** (async) | Mature, async, inline keyboards/buttons for approvals. |
| Session multiplexing | **tmux** | Persistent, scriptable (`send-keys`, `capture-pane`), survives daemon restarts. |
| Agent | **Claude Code CLI** (`claude`), interactive in tmux for build loop; `claude -p` for scripted ops | See §0. |
| Git | **GitHub CLI (`gh`)** + git | `gh repo create`, `gh auth` already set on VM. |
| Deploy | **Vercel CLI (`vercel`)** + Vercel git integration | Preview on `dev` push (native), prod on explicit command. |
| State | **SQLite** (or a single JSON file for v1) | Track sessions, active agent, port map, repo/branch per project. |
| Process mgmt | **systemd** unit for the daemon | Auto-restart if it crashes; starts on boot. |
| Secrets | **`.env` + env vars**, `chmod 600` | Bot token, API keys, never in git. |

**Alternative considered:** there's a class of ready-made "control planes" for parallel headless Claude Code agents (e.g. amux-style tools) that run each agent in tmux with a dashboard + cost tracking + a task board. Worth a look as prior art — but they're built around *headless* fleets doing autonomous task-claiming, not the *interactive, chat-with-me-via-Telegram* loop I want. Build my own thin orchestrator; borrow ideas (per-agent tmux, cost tracking from JSONL transcripts) where useful.

---

## 4. The tmux ↔ Telegram bridge (the hard part)

This is the trickiest engineering piece, so it gets its own section.

### 4.1 Spawning a session
```bash
tmux new-session -d -s "proj-offerkoi" -c "/home/awesh/projects/offerkoi"
tmux send-keys -t "proj-offerkoi" "claude" Enter
```
- `-d` detached, `-s` name, `-c` working dir.
- Each project = one tmux session (or window). Name convention: `proj-<slug>`.

### 4.2 Sending my Telegram message INTO the agent
```bash
tmux send-keys -t "proj-offerkoi" -l "add a filter for Dhanmondi restaurants"
tmux send-keys -t "proj-offerkoi" Enter
```
- `-l` sends the text literally (no key-name interpretation).
- Router maps: my Telegram text (for the active/tagged agent) → `send-keys` into that session.

### 4.3 Getting the agent's output OUT to Telegram
Two viable approaches — **pick B, keep A as fallback:**

**A) Poll `capture-pane`** (simplest, works today):
```bash
tmux capture-pane -t "proj-offerkoi" -p -S -50   # last 50 lines
```
- Daemon polls every N seconds, diffs against last-sent buffer, sends only new lines to Telegram.
- Pro: dead simple. Con: polling lag; parsing a TUI's redraws is messy.

**B) `pipe-pane` to a file/FIFO, tail it** (cleaner streaming):
```bash
tmux pipe-pane -t "proj-offerkoi" -o "cat >> /var/log/agents/offerkoi.log"
```
- Daemon tails the log, forwards new content to Telegram in near-real-time.
- Pro: streaming, no polling loop hammering tmux. Con: still need to clean ANSI escape codes.

**In both cases:** strip ANSI/TUI control sequences before sending to Telegram (use a regex or a lib like `sttransform`/`strip-ansi` equivalent). Consider running Claude Code with the simplest possible output (minimal color/animation) to make parsing sane.

### 4.4 Detecting "the agent is asking me a question"
This is what enables *minimal interruption* — I only get pinged when needed.
- Heuristic v1: watch the outbound stream for question markers — a trailing `?`, prompt patterns like `(y/n)`, `Do you want`, `Which`, `Should I`, an idle-with-cursor state, or Claude Code's own approval prompts.
- When detected → push a Telegram message tagged with the project name + the question, and mark that session `WAITING_FOR_ME` in state.
- My reply routes back into that session automatically (see §5.2 routing).
- Refinement later: if I run Claude Code via the Agent SDK for some flows, tool-approval callbacks give structured "agent wants permission" events instead of scraping text. Keep that as a v3 upgrade path.

### 4.5 Reconnecting after a daemon restart
- On startup, `tmux ls` to rediscover live sessions, rehydrate state from SQLite, re-attach `pipe-pane` logging. Sessions survive because tmux (not the daemon) hosts them.

---

## 5. Telegram command surface

### 5.1 Commands
| Command | Action |
|---|---|
| `/ls [path]` | List the base projects dir (or given subpath). |
| `/mkdir <name>` | Create a new project folder under the base dir. |
| `/new <name>` | Spawn an interactive CC session in `projects/<name>` (mkdir if missing). Becomes the active agent. |
| `/list` | Show all active agents + status (`BUILDING`, `WAITING_FOR_ME`, `IDLE`), their port, branch. |
| `/switch <name>` | Set the active agent (free-text messages route here). |
| `/status [name]` | `capture-pane` summary of what that agent is doing right now. |
| `/say <name> <msg>` | Send a message to a *specific* agent without switching active. |
| `/port <name>` | Show the URL/IP:port where that project's dev server is served. |
| `/push <name>` | Approve: commit + push to `dev` (repo created if needed) → triggers Vercel preview. |
| `/deploy <name>` | **Prod gate:** merge `dev`→`prod` → Vercel production deploy. Requires confirm button. |
| `/kill <name>` | Tear down that agent's tmux session. |
| `/pause <name>` / `/resume <name>` | Stop/allow output relay for a noisy agent (mute). |
| `/help` | List commands. |

### 5.2 Free-text routing rules
- Plain text → goes to the **active agent** (last `/new` or `/switch`).
- `@<name> <text>` → goes to that agent regardless of active (explicit tag).
- If an agent is `WAITING_FOR_ME` and I reply to *its* Telegram message (Telegram reply-to), route to that agent even if not active.

### 5.3 Inline buttons (approvals)
- Use inline keyboards for the scary actions:
  - Agent proposes a deploy → Telegram shows **[Preview URL] [Deploy to prod] [Not yet]**.
  - `/deploy` → **[Confirm prod deploy] [Cancel]**.
- Never deploy to prod without a button press.

---

## 6. Git + Vercel flow

### Branch model
- `dev` — agents push here freely (on my `/push` approval). Vercel auto-creates a **preview** deployment per push (native Vercel git integration → gives me a URL to eyeball).
- `prod` — only updated by `/deploy` (merge `dev`→`prod`). Vercel treats this as the **production** branch.

### The flow, step by step
1. Agent builds in tmux; dev server runs on an allocated port; I monitor via `IP:port`.
2. I'm happy → `/push offerkoi`.
   - Daemon (or a `claude -p` scripted step) does: `git add -A`, generate a commit message, `git commit`, `git push origin dev`.
   - If no remote: `gh repo create <name> --private --source=. --remote=origin --push`.
   - Vercel detects `dev` push → builds **preview** → daemon fetches the preview URL (Vercel CLI/API) → posts it to Telegram with buttons.
3. I review the preview URL.
4. I'm satisfied → `/deploy offerkoi` → **[Confirm]**.
   - Daemon merges `dev`→`prod` (`git checkout prod && git merge dev && git push`), or triggers `vercel --prod`.
   - Vercel builds production → daemon confirms "🚀 live at <prod URL>" in Telegram.

### First-time project setup (per project)
- Ensure Vercel project is linked (`vercel link` or created via CLI) with `prod` set as the production branch and preview deploys enabled for `dev`.
- Store per-project mapping in state: `{ slug, local_path, github_repo, vercel_project, dev_port }`.

---

## 7. Port management

- Maintain a port pool (e.g. 3001–3099) in state.
- On session spawn (or when the agent starts a dev server), allocate the lowest free port.
- Detect occupied ports before assigning:
  ```bash
  ss -ltn | awk '{print $4}' | grep -oE '[0-9]+$' | sort -u
  ```
  (or `lsof -i -P -n | grep LISTEN`).
- Tell the agent (via its CLAUDE.md or an injected instruction) to bind its dev server to the assigned `PORT` env var, so it doesn't fight other agents.
- `/port <name>` returns `http://<VM_IP>:<port>` for monitoring.
- (Later: reverse-proxy via Caddy/nginx to give each project a subpath/subdomain over HTTPS instead of raw IP:port.)

---

## 8. Per-agent context (so agents behave)

For each project folder, drop a `CLAUDE.md` with house rules, e.g.:
- "Bind the dev server to `process.env.PORT` (assigned by orchestrator). Don't hardcode a port."
- "Work on the `dev` branch. Never touch `prod`."
- "When you need a decision from me, ask a single clear question and wait."
- "Prefer small, reviewable commits."
- Project-specific stack/conventions.

This keeps interactive agents predictable and reduces how often they interrupt me.

---

## 9. Security & safety (non-negotiable)

1. **Auth allowlist:** the bot responds ONLY to my Telegram numeric user ID. Every handler checks it first; silently drop everything else.
2. **Secrets:** bot token, `ANTHROPIC_API_KEY`, GitHub PAT / `gh` creds, Vercel token → in `.env`, `chmod 600`, never committed. `.gitignore` the env file and the orchestrator's own state DB.
3. **Prod gate:** production deploy always requires an explicit command **and** a button confirm. No agent can self-deploy to prod.
4. **Kill switch / budget caps:** per-agent max runtime and (if using API key) a token/cost ceiling. If exceeded → auto-pause + Telegram alert. (Cost can be read from Claude Code's JSONL transcripts.)
5. **Blast radius:** run agents under a dedicated non-root user; consider one Linux user or container per agent so they can't stomp each other's files. Never run the daemon as root.
6. **Permission scope for headless steps:** when using `claude -p` for git/deploy, pass explicit `--allowedTools` (e.g. `Bash,Read`) and a non-interactive permission mode rather than blanket-skipping permissions. Only use `--dangerously-skip-permissions` inside a locked-down container.
7. **Dead-man's-switch:** a lightweight external health ping (separate from Telegram) so if the VM/daemon dies I still find out. Telegram can't alert me if the thing that sends Telegram alerts is down.
8. **Audit log:** log every command, push, and deploy with timestamp. "If you didn't capture it, it didn't happen."

---

## 10. State model (SQLite tables or JSON)

```
agents:
  slug            TEXT PRIMARY KEY   -- "offerkoi"
  tmux_session    TEXT               -- "proj-offerkoi"
  local_path      TEXT
  github_repo     TEXT               -- "awesh/offerkoi" (nullable until created)
  vercel_project  TEXT               -- (nullable until linked)
  dev_port        INTEGER
  status          TEXT               -- BUILDING | WAITING_FOR_ME | IDLE | PAUSED
  auth_mode       TEXT               -- "subscription" | "api_key"
  created_at      TIMESTAMP
  last_activity   TIMESTAMP

meta:
  active_agent    TEXT               -- slug of currently active agent
```

---

## 11. Build phases (ship in slices — don't build it all at once)

### Phase 1 — Prove the bridge (single agent, no git)
- systemd-managed Python daemon, Telegram bot, my-user-ID auth.
- `/ls`, `/mkdir`.
- `/new <name>` → spawn ONE interactive CC session in tmux.
- Bidirectional bridge: my text → `send-keys`; pane output → Telegram (start with `capture-pane` polling).
- **Success = I can start a build and chat with one Claude Code agent entirely from Telegram.**

### Phase 2 — Make it usable
- Switch relay to `pipe-pane` streaming + ANSI stripping.
- Question-detection → `WAITING_FOR_ME` pings (minimal interruption).
- Port allocation + `/port` + inject `PORT` via CLAUDE.md.
- `/status`, `/kill`.

### Phase 3 — Multi-agent
- `/list`, `/switch`, `/say`, `@tag` routing, reply-to routing.
- Per-agent state in SQLite; reconnect-on-restart via `tmux ls`.
- Mute/`/pause` for noisy agents.
- Per-agent auth mode (subscription vs API key) + basic cost tracking from JSONL.

### Phase 4 — Git + deploy
- `/push` → commit + push `dev` (+ `gh repo create` if needed) → fetch & post Vercel preview URL.
- `/deploy` → confirm button → merge `dev`→`prod` → Vercel prod → confirm live.
- Audit log + budget caps + dead-man's-switch.

### Phase 5 — Polish (optional)
- Reverse proxy (Caddy) for HTTPS per-project URLs instead of IP:port.
- Agent SDK path for structured tool-approval events (replace text-scraping question detection).
- Slash-command autocomplete, richer status cards, cost dashboards.

---

## 12. Open questions to resolve before Phase 1

1. **Auth per agent:** start all agents on the Max subscription, or wire API-key mode from day one? (Recommend: subscription for 1–3, add API-key toggle in Phase 3.)
2. **One tmux session per project vs one session, many windows?** (Recommend: session per project — cleaner isolation, easier kill.)
3. **Same VM user for all agents vs isolated users/containers?** (Recommend: at least a dedicated non-root user now; containers later if agents start clashing.)
4. **How chatty do I want default updates?** (Recommend: quiet by default — only ping on questions, milestones, and my explicit `/status`.)
5. **Preview URL retrieval:** Vercel CLI output parse vs Vercel API token? (API is more robust — get a token.)

---

## 13. First concrete step

Stand up **Phase 1** end-to-end with a single hardcoded project before generalizing:
1. `pip install python-telegram-bot`
2. Create the bot with @BotFather, grab the token, put it in `.env`, lock down to my user ID.
3. Write the daemon: `/ls`, `/mkdir`, `/new` (spawn one tmux CC session), and the capture-pane→Telegram + Telegram→send-keys bridge.
4. Run it under systemd, test from my phone, iterate on output cleanliness.

Everything else (multi-agent, git, deploy) layers on top once the bridge feels good.