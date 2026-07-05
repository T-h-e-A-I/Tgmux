"""Telegram command surface, routing, and approvals (plan §5).

Auth: every handler is filtered to the owner's numeric user ID; everything
else is silently dropped (plan §9.1).
"""

import asyncio
import html
import logging
import os
import re
import sys
import time
from pathlib import Path

log = logging.getLogger(__name__)

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import bridge, config, gitops, ports, state, tmuxctl

STARTED_AT = time.time()

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
MAX_CHUNK = 3800

CLAUDE_MD_TEMPLATE = """\
# House rules (injected by orchestrator)

- Bind any dev server to port **{port}** (also set as the `PORT` env var).
  Never hardcode another port; other agents share this machine.
- Work on the `dev` branch. Never touch `prod` — deploys are gated by the owner.
- When you need a decision, ask ONE clear question and wait for the reply.
- Prefer small, reviewable commits. Do not push or deploy yourself; the
  orchestrator handles /push and /deploy on the owner's approval.
- You are controlled remotely via Telegram: keep questions and status updates
  short and self-contained (they are read on a phone).
"""

HELP_TEXT = """\
<b>📂 Projects</b>
📁 /ls [path] — list projects dir
🆕 /mkdir &lt;name&gt; — create project folder

<b>🤖 Agents</b>
✨ /new &lt;name&gt; [api] — spawn Claude Code agent (api = use API key auth)
🔗 /adopt &lt;name&gt; &lt;path&gt; [tmux-session] — agent in an existing folder, or bridge a running tmux session
🧟 /revive &lt;name&gt; [path] — respawn a killed agent with its previous conversation (claude --continue)
📋 /list — agents + status
🎯 /switch &lt;name&gt; — set active agent
👀 /status [name] — what is it doing right now
💬 /say &lt;name&gt; &lt;msg&gt; — message a specific agent
🔇 /pause &lt;name&gt; / 🔊 /resume &lt;name&gt; — mute/unmute progress relay
🗡 /kill &lt;name&gt; — tear down agent
💀 /killall — tear down ALL agents (confirm button)

<b>🎛 Control</b>
🔁 /mode [name] — cycle Claude Code mode: normal → accept-edits → plan (Shift+Tab)
🛑 /esc [name] — interrupt the agent (Escape)
⌨️ /key &lt;name&gt; &lt;key&gt; — send a raw key (Enter, Up, Down, Tab, 1, C-c …)

<b>🚀 Ship</b>
🌐 /port &lt;name&gt; — dev server URL
📤 /push &lt;name&gt; — commit+push dev → Vercel preview
🟢 /deploy &lt;name&gt; — merge dev→prod + production deploy (confirm button)

<b>⚙️ Daemon</b>
♻️ /restart — restart the orchestrator daemon (agents survive)
⏱ /uptime — how long the daemon has been running

<b>🧭 Routing</b>
plain text → active agent · @name text → that agent · replying to an agent's message → that agent
Attach a file/photo → saved to the project's <code>incoming/</code> dir and the agent is told (caption becomes the instruction; @name in caption routes it)
Any other /command (e.g. /compact, /model, custom skills) is passed through to the active agent\
"""


# ---------- helpers ----------

def _relays(app: Application) -> dict:
    return app.bot_data.setdefault("relays", {})


def _routes(app: Application) -> dict:
    return app.bot_data.setdefault("routes", {})


def _chunk_for_html(body: str, limit: int = MAX_CHUNK) -> list[str]:
    """Split on line boundaries so each chunk's *escaped* size fits Telegram."""
    chunks: list[str] = []
    cur: list[str] = []
    cur_len = 0
    for line in body.split("\n"):
        # hard-split pathological single lines
        while len(html.escape(line)) > limit:
            line, rest = line[: limit // 4], line[limit // 4:]
            if cur_len + len(html.escape(line)) + 1 > limit and cur:
                chunks.append("\n".join(cur))
                cur, cur_len = [], 0
            cur.append(line)
            cur_len += len(html.escape(line)) + 1
            line = rest
        esc_len = len(html.escape(line)) + 1
        if cur_len + esc_len > limit and cur:
            chunks.append("\n".join(cur))
            cur, cur_len = [], 0
        cur.append(line)
        cur_len += esc_len
    if cur:
        chunks.append("\n".join(cur))
    return chunks


async def send_pre(app: Application, header: str, body: str,
                   reply_markup=None, route_slug: str | None = None) -> None:
    """Send header + monospace body, chunked; remember message->agent routing."""
    body = body.strip() or "(no output)"
    chunks = _chunk_for_html(body)
    for i, chunk in enumerate(chunks):
        markup = reply_markup if i == len(chunks) - 1 else None
        try:
            msg = await app.bot.send_message(
                chat_id=config.OWNER_ID,
                text=f"{header}\n<pre>{html.escape(chunk)}</pre>",
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
        except Exception:
            log.exception("HTML send failed (%d chars), retrying as plain text", len(chunk))
            msg = await app.bot.send_message(
                chat_id=config.OWNER_ID,
                text=chunk[:4000],
                reply_markup=markup,
            )
        if route_slug:
            _routes(app)[msg.message_id] = route_slug


async def notify_from_agent(app: Application, slug: str, text: str,
                            is_question: bool) -> None:
    """Bridge callback: agent output / question -> Telegram."""
    header = f"🔶 <b>{html.escape(slug)}</b> needs you:" if is_question \
        else f"🛠 <b>{html.escape(slug)}</b>"
    await send_pre(app, header, text, route_slug=slug)


def start_relay(app: Application, slug: str) -> None:
    async def notify(s: str, text: str, q: bool) -> None:
        try:
            await notify_from_agent(app, s, text, q)
        except Exception:
            # never let a Telegram hiccup kill the relay — but never hide it either
            log.exception("relay notify failed for agent %s", s)

    relays = _relays(app)
    old = relays.get(slug)
    if old and not old.done():
        old.cancel()
    relays[slug] = app.create_task(bridge.relay_loop(slug, notify))


def _safe_path(sub: str) -> Path | None:
    p = (config.PROJECTS_DIR / sub).resolve() if sub else config.PROJECTS_DIR
    try:
        p.relative_to(config.PROJECTS_DIR.resolve())
    except ValueError:
        return None
    return p


def _arg_slug(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    if not context.args:
        return None
    slug = context.args[0].lower()
    return slug if SLUG_RE.match(slug) else None


async def _reply(update: Update, text: str, **kw) -> None:
    await update.effective_message.reply_text(text, **kw)


# ---------- commands ----------

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, HELP_TEXT, parse_mode=ParseMode.HTML)


async def cmd_ls(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sub = context.args[0] if context.args else ""
    path = _safe_path(sub)
    if path is None or not path.exists():
        await _reply(update, f"❌ no such path under {config.PROJECTS_DIR}")
        return
    entries = sorted(path.iterdir(), key=lambda e: (e.is_file(), e.name))
    lines = [f"{'📁' if e.is_dir() else '📄'} {e.name}" for e in entries] or ["(empty)"]
    await _reply(update, f"{path}\n" + "\n".join(lines[:100]))


async def cmd_mkdir(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    slug = _arg_slug(context)
    if not slug:
        await _reply(update, "usage: /mkdir <name>   (lowercase, a-z 0-9 - _)")
        return
    path = config.PROJECTS_DIR / slug
    if path.exists():
        await _reply(update, f"already exists: {path}")
        return
    path.mkdir(parents=True)
    state.audit("mkdir", str(path))
    await _reply(update, f"✅ created {path}")


async def _spawn(app: Application, slug: str, path: Path, auth_mode: str,
                 resume: bool = False, port: int | None = None) -> str:
    """Shared by /new, /adopt and /revive: tmux session + claude + state + relay.
    resume=True relaunches with `claude --continue` (previous conversation);
    port, if given, is reused instead of allocating a fresh one."""
    sess = tmuxctl.session_name(slug)
    if await tmuxctl.has_session(sess):
        state.set_active(slug)
        return f"already running — switched active agent to {slug}"

    if not port:
        try:
            port = ports.allocate()
        except RuntimeError as e:
            return f"❌ {e}"

    claude_md = path / "CLAUDE.md"
    if not claude_md.exists():
        claude_md.write_text(CLAUDE_MD_TEMPLATE.format(port=port))

    if not await tmuxctl.new_session(sess, str(path), {"PORT": str(port)}):
        return "❌ tmux failed to create the session"
    await tmuxctl.pipe_to_log(sess, slug)

    extra = {"ANTHROPIC_API_KEY": config.ANTHROPIC_API_KEY} if auth_mode == "api_key" else None
    await tmuxctl.start_claude(sess, extra, resume=resume)

    state.add_agent(slug, sess, str(path), port, auth_mode)
    state.set_active(slug)
    if resume:
        # --continue repaints the old conversation tail; wait for it and
        # baseline that screen so the relay doesn't resend history.
        bridge.drop_baseline(slug)
        await asyncio.sleep(6)
        bridge.save_baseline(slug, bridge.clean_pane(await tmuxctl.capture(sess)))
    start_relay(app, slug)
    state.audit("revive_agent" if resume else "new_agent",
                f"path={path} port={port} auth={auth_mode}", slug)
    if resume:
        return (
            f"🧟 agent <b>{slug}</b> revived in {html.escape(str(path))} — "
            f"previous conversation restored.\n"
            f"port {port} · auth {auth_mode} · now the active agent."
        )
    return (
        f"🤖 agent <b>{slug}</b> spawned in {html.escape(str(path))}\n"
        f"port {port} · auth {auth_mode} · now the active agent.\n"
        f"Just type to talk to it."
    )


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    slug = _arg_slug(context)
    if not slug:
        await _reply(update, "usage: /new <name> [api]")
        return
    auth_mode = "api_key" if (len(context.args) > 1 and context.args[1] == "api") else "subscription"
    if auth_mode == "api_key" and not config.ANTHROPIC_API_KEY:
        await _reply(update, "❌ api mode requested but ANTHROPIC_API_KEY is not set in .env")
        return
    path = config.PROJECTS_DIR / slug
    path.mkdir(parents=True, exist_ok=True)
    msg = await _spawn(context.application, slug, path, auth_mode)
    await _reply(update, msg, parse_mode=ParseMode.HTML)


async def cmd_adopt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Attach an agent to an existing folder — or bridge an EXISTING tmux
    session (e.g. a claude you started by hand): /adopt <name> <path> [tmux-session]."""
    if len(context.args) < 2:
        await _reply(update, "usage: /adopt <name> <absolute-path> [existing-tmux-session]")
        return
    slug = _arg_slug(context)
    if not slug:
        await _reply(update, "bad name (lowercase, a-z 0-9 - _)")
        return
    path = Path(context.args[1]).resolve()
    if not path.is_dir():
        await _reply(update, f"❌ not a directory: {path}")
        return
    if state.get_agent(slug):
        await _reply(update, f"❌ agent name {slug} is taken — /kill it first or pick another")
        return

    sess_arg = context.args[2] if len(context.args) > 2 else None
    if sess_arg:
        if not await tmuxctl.has_session(sess_arg):
            await _reply(update, f"❌ no tmux session named {sess_arg}")
            return
        try:
            port = ports.allocate()
        except RuntimeError:
            port = 0
        await tmuxctl.pipe_to_log(sess_arg, slug)
        state.add_agent(slug, sess_arg, str(path), port)
        state.set_active(slug)
        start_relay(context.application, slug)
        state.audit("adopt_session", f"tmux={sess_arg} path={path}", slug)
        await _reply(update, f"🔗 bridged existing tmux session '{sess_arg}' as agent "
                             f"<b>{slug}</b> — now active. Just type.",
                     parse_mode=ParseMode.HTML)
        return

    msg = await _spawn(context.application, slug, path, "subscription")
    await _reply(update, msg, parse_mode=ParseMode.HTML)


async def cmd_revive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Respawn a killed/dead agent with its conversation restored: Claude Code
    keeps transcripts per project dir, so `claude --continue` in the same
    folder picks up where it left off. /revive <name> [path] — path is only
    needed if the agent was fully /kill'ed AND doesn't live in PROJECTS_DIR."""
    slug = _arg_slug(context)
    if not slug:
        await _reply(update, "usage: /revive <name> [path]")
        return
    agent = state.get_agent(slug)
    if agent:  # row survived (DEAD status) — reuse everything we know
        if await tmuxctl.has_session(agent["tmux_session"]):
            await _reply(update, f"{slug} is already running — /switch {slug}")
            return
        path = Path(agent["local_path"])
        port, auth_mode = agent["dev_port"], agent["auth_mode"]
    else:      # /kill removed the row — recover from the path
        path = Path(context.args[1]).resolve() if len(context.args) > 1 \
            else config.PROJECTS_DIR / slug
        port, auth_mode = None, "subscription"
    if not path.is_dir():
        await _reply(update, f"❌ no such directory: {path}\n"
                             f"usage: /revive <name> <path>")
        return
    await _reply(update, f"🧟 reviving {slug} — restoring its last conversation…")
    msg = await _spawn(context.application, slug, path, auth_mode,
                       resume=True, port=port)
    await _reply(update, msg, parse_mode=ParseMode.HTML)


async def cmd_uptime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    secs = int(time.time() - STARTED_AT)
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    parts = ([f"{d}d"] if d else []) + ([f"{h}h"] if h or d else []) \
        + ([f"{m}m"] if m or h or d else []) + [f"{s}s"]
    started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(STARTED_AT))
    agents = state.all_agents()
    live = sum(1 for a in agents if a["status"] != "DEAD")
    await _reply(update, f"⏱ daemon up { ' '.join(parts) } (since {started})\n"
                         f"agents: {live} live / {len(agents)} total")


async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-exec the daemon in place. tmux agents survive; state rehydrates."""
    await _reply(update, "♻️ restarting orchestrator… agents survive, back in a few seconds")
    state.audit("restart", "via /restart")
    os.chdir(config.ROOT)
    os.execv(sys.executable, [sys.executable, "-m", "orchestrator"])


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    agents = state.all_agents()
    if not agents:
        await _reply(update, "no agents. /new <name> to spawn one.")
        return
    active = state.get_active()
    icons = {"BUILDING": "🛠", "WAITING_FOR_ME": "🔶", "IDLE": "😴", "DEAD": "💀"}
    lines = []
    for a in agents:
        mark = "👉 " if a["slug"] == active else "   "
        mute = " 🔇" if a["paused"] else ""
        lines.append(
            f"{mark}{icons.get(a['status'], '·')} {a['slug']} — {a['status']}{mute}"
            f" · :{a['dev_port']}"
            + (f" · {a['github_repo']}" if a["github_repo"] else "")
        )
    await _reply(update, "\n".join(lines))


async def cmd_switch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    slug = _arg_slug(context)
    if not slug or not state.get_agent(slug):
        await _reply(update, "usage: /switch <name> — see /list")
        return
    state.set_active(slug)
    await _reply(update, f"👉 active agent: {slug}")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    slug = _arg_slug(context) or state.get_active()
    agent = state.get_agent(slug) if slug else None
    if not agent:
        await _reply(update, "no agent — /status <name> or /switch first")
        return
    raw = await tmuxctl.capture(agent["tmux_session"], lines=80)
    tail = "\n".join(bridge.clean_pane(raw)[-30:])
    await send_pre(context.application,
                   f"📟 <b>{html.escape(slug)}</b> ({agent['status']})",
                   tail, route_slug=slug)


async def cmd_say(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 2:
        await _reply(update, "usage: /say <name> <message>")
        return
    slug = context.args[0].lower()
    text = " ".join(context.args[1:])
    await _route_to_agent(update, context, slug, text)


async def cmd_port(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    slug = _arg_slug(context) or state.get_active()
    agent = state.get_agent(slug) if slug else None
    if not agent:
        await _reply(update, "usage: /port <name>")
        return
    await _reply(update, f"🌐 {slug} → {ports.url_for(agent['dev_port'])}")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_paused(update, context, 1, "🔇 muted (questions still ping)")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _set_paused(update, context, 0, "🔊 unmuted")


async def _set_paused(update, context, value: int, label: str) -> None:
    slug = _arg_slug(context)
    if not slug or not state.get_agent(slug):
        await _reply(update, "usage: /pause|/resume <name>")
        return
    state.set_field(slug, "paused", value)
    await _reply(update, f"{label}: {slug}")


async def _kill_agent(app: Application, slug: str) -> str | None:
    """Tear down one agent; returns its local_path or None if unknown."""
    agent = state.get_agent(slug)
    if not agent:
        return None
    task = _relays(app).pop(slug, None)
    if task:
        task.cancel()
    await tmuxctl.kill_session(agent["tmux_session"])
    state.delete_agent(slug)
    bridge.drop_baseline(slug)
    state.audit("kill_agent", "", slug)
    return agent["local_path"]


async def _agent_for_key(update, context, need_key: bool = False):
    """Resolve (agent, key) for /mode, /esc, /key."""
    args = list(context.args or [])
    key = args.pop() if need_key and args else None
    slug = (args[0].lower() if args else None) or state.get_active()
    agent = state.get_agent(slug) if slug else None
    if not agent or (need_key and not key):
        await _reply(update, "no agent (or missing key) — see /help")
        return None, None
    if not await tmuxctl.has_session(agent["tmux_session"]):
        state.set_status(agent["slug"], "DEAD")
        await _reply(update, f"💀 {agent['slug']}'s session is gone")
        return None, None
    return agent, key


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    agent, _ = await _agent_for_key(update, context)
    if not agent:
        return
    await tmuxctl.send_key(agent["tmux_session"], "BTab")  # Shift+Tab
    await _reply(update, f"⇥ {agent['slug']}: cycled mode (normal → accept-edits → plan). "
                         f"/status to see which is active; /mode again to keep cycling.")


async def cmd_esc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    agent, _ = await _agent_for_key(update, context)
    if not agent:
        return
    await tmuxctl.send_key(agent["tmux_session"], "Escape")
    await _reply(update, f"⛔ sent Escape to {agent['slug']}")


async def cmd_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    agent, key = await _agent_for_key(update, context, need_key=True)
    if not agent:
        return
    await tmuxctl.send_key(agent["tmux_session"], key)
    await _reply(update, f"⌨️ sent {key} to {agent['slug']}", disable_notification=True)


async def cmd_kill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    slug = _arg_slug(context)
    if not slug or not state.get_agent(slug):
        await _reply(update, "usage: /kill <name>")
        return
    path = await _kill_agent(context.application, slug)
    await _reply(update, f"💀 {slug} torn down (files kept in {path} — "
                         f"/revive {slug} brings it back with its memory)")


async def cmd_killall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    agents = state.all_agents()
    if not agents:
        await _reply(update, "no agents to kill")
        return
    names = ", ".join(a["slug"] for a in agents)
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"☠️ Kill ALL {len(agents)}", callback_data="do_killall"),
        InlineKeyboardButton("Cancel", callback_data="dismiss"),
    ]])
    await _reply(update, f"⚠️ This tears down: {names}\nProject files are kept.",
                 reply_markup=kb)


async def cmd_push(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    slug = _arg_slug(context) or state.get_active()
    agent = state.get_agent(slug) if slug else None
    if not agent:
        await _reply(update, "usage: /push <name>")
        return
    await _reply(update, f"⏳ pushing {slug} to dev…")
    ok, out = await gitops.push_dev(slug, agent["local_path"])
    if not ok:
        await send_pre(context.application, f"❌ <b>{slug}</b> push failed", out)
        return
    await send_pre(context.application, f"✅ <b>{slug}</b> pushed", out)
    await context.application.bot.send_message(
        config.OWNER_ID, f"⏳ building Vercel preview for {slug}…")
    ok, url = await gitops.vercel_preview(slug, agent["local_path"])
    if not ok:
        await send_pre(context.application, f"❌ <b>{slug}</b> preview failed", url)
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🚀 Deploy to prod", callback_data=f"confirm_deploy:{slug}"),
        InlineKeyboardButton("Not yet", callback_data="dismiss"),
    ]])
    await context.application.bot.send_message(
        config.OWNER_ID,
        f"🔍 <b>{slug}</b> preview ready:\n{url}",
        parse_mode=ParseMode.HTML, reply_markup=kb,
    )


async def cmd_deploy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    slug = _arg_slug(context)
    if not slug or not state.get_agent(slug):
        await _reply(update, "usage: /deploy <name>")
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm prod deploy", callback_data=f"do_deploy:{slug}"),
        InlineKeyboardButton("Cancel", callback_data="dismiss"),
    ]])
    await _reply(update, f"⚠️ Deploy {slug} to PRODUCTION (merge dev→prod)?",
                 reply_markup=kb)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or update.effective_user.id != config.OWNER_ID:
        return
    await q.answer()
    data = q.data or ""

    if data == "dismiss":
        await q.edit_message_reply_markup(None)
        return

    if data == "do_killall":
        await q.edit_message_reply_markup(None)
        killed = []
        for a in state.all_agents():
            if await _kill_agent(context.application, a["slug"]) is not None:
                killed.append(a["slug"])
        await context.application.bot.send_message(
            config.OWNER_ID, f"💀 killed: {', '.join(killed) or 'nothing'}")
        return

    if data.startswith("confirm_deploy:"):
        slug = data.split(":", 1)[1]
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm prod deploy", callback_data=f"do_deploy:{slug}"),
            InlineKeyboardButton("Cancel", callback_data="dismiss"),
        ]])
        await q.edit_message_reply_markup(kb)
        return

    if data.startswith("do_deploy:"):
        slug = data.split(":", 1)[1]
        agent = state.get_agent(slug)
        await q.edit_message_reply_markup(None)
        if not agent:
            await context.application.bot.send_message(config.OWNER_ID, f"❌ unknown agent {slug}")
            return
        await context.application.bot.send_message(
            config.OWNER_ID, f"🚀 deploying {slug} to production…")
        ok, out = await gitops.deploy_prod(slug, agent["local_path"])
        header = f"🚀 <b>{slug}</b> is LIVE" if ok else f"❌ <b>{slug}</b> prod deploy failed"
        await send_pre(context.application, header, out)


# ---------- free-text routing (plan §5.2) ----------

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    text = (msg.text or "").strip()
    if not text:
        return

    # 1) reply-to an agent's message
    slug = None
    if msg.reply_to_message:
        slug = _routes(context.application).get(msg.reply_to_message.message_id)

    # 2) @name prefix
    if not slug and text.startswith("@"):
        head, _, rest = text[1:].partition(" ")
        if state.get_agent(head.lower()) and rest.strip():
            slug, text = head.lower(), rest.strip()

    # 3) active agent
    if not slug:
        slug = state.get_active()

    if not slug:
        await _reply(update, "no active agent — /new <name> or /switch <name>")
        return
    await _route_to_agent(update, context, slug, text)


async def on_unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Slash-command passthrough: /compact, /model, custom skills … go to the
    agent (reply-to routing respected). Orchestrator commands never reach here
    because their CommandHandlers match first."""
    msg = update.effective_message
    text = (msg.text or "").strip()
    if not text.startswith("/"):
        return
    slug = None
    if msg.reply_to_message:
        slug = _routes(context.application).get(msg.reply_to_message.message_id)
    slug = slug or state.get_active()
    if not slug:
        await _reply(update, "no active agent to send that command to")
        return
    await _route_to_agent(update, context, slug, text)


async def on_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Attachment -> <project>/incoming/<file>, then tell the agent about it."""
    msg = update.effective_message
    doc = msg.document
    photo = msg.photo[-1] if msg.photo else None
    if not doc and not photo:
        return
    caption = (msg.caption or "").strip()

    # same routing rules as text: reply-to > @name in caption > active agent
    slug = None
    if msg.reply_to_message:
        slug = _routes(context.application).get(msg.reply_to_message.message_id)
    if not slug and caption.startswith("@"):
        head, _, rest = caption[1:].partition(" ")
        if state.get_agent(head.lower()):
            slug, caption = head.lower(), rest.strip()
    if not slug:
        slug = state.get_active()
    agent = state.get_agent(slug) if slug else None
    if not agent:
        await _reply(update, "no active agent to send the file to — /new or /switch first")
        return

    if doc:
        name = doc.file_name or f"file_{doc.file_unique_id}"
        tgfile = await doc.get_file()
    else:
        name = f"photo_{photo.file_unique_id}.jpg"
        tgfile = await photo.get_file()
    name = re.sub(r"[^\w.\-]", "_", name)

    incoming = Path(agent["local_path"]) / "incoming"
    incoming.mkdir(exist_ok=True)
    dest = incoming / name
    try:
        await tgfile.download_to_drive(custom_path=str(dest))
    except Exception as e:  # Bot API caps downloads at ~20 MB
        await _reply(update, f"❌ download failed: {e}")
        return

    note = f"[File from Telegram] saved at ./incoming/{name}"
    note += f" — {caption}" if caption else " — take a look."
    if await tmuxctl.has_session(agent["tmux_session"]):
        bridge.note_sent(slug, note)
        await tmuxctl.send_text(agent["tmux_session"], note)
        state.set_status(slug, "BUILDING")
        tail = f"→ {slug}"
    else:
        state.set_status(slug, "DEAD")
        tail = f"⚠️ saved, but {slug}'s session is dead"
    state.audit("file", name, slug)
    await _reply(update, f"📎 {dest}\n{tail}", disable_notification=True)


async def _route_to_agent(update, context, slug: str, text: str) -> None:
    agent = state.get_agent(slug)
    if not agent:
        await _reply(update, f"❌ no agent named {slug} — see /list")
        return
    if not await tmuxctl.has_session(agent["tmux_session"]):
        state.set_status(slug, "DEAD")
        await _reply(update, f"💀 {slug}'s session is gone. /kill it and /new again.")
        return
    bridge.note_sent(slug, text)
    await tmuxctl.send_text(agent["tmux_session"], text)
    state.set_status(slug, "BUILDING")
    state.audit("say", text[:200], slug)
    await update.effective_message.reply_text(f"→ {slug}", disable_notification=True)


# ---------- wiring ----------

async def post_init(app: Application) -> None:
    """Rehydrate after (re)start: reattach to surviving tmux sessions (plan §4.5)."""
    config.ensure_dirs()
    state.db()
    alive, dead = [], []
    for agent in state.all_agents():
        slug = agent["slug"]
        if await tmuxctl.has_session(agent["tmux_session"]):
            await tmuxctl.pipe_to_log(agent["tmux_session"], slug)
            start_relay(app, slug)
            alive.append(slug)
        else:
            state.set_status(slug, "DEAD")
            dead.append(slug)
    summary = "🟢 orchestrator online"
    if alive:
        summary += f"\nreattached: {', '.join(alive)}"
    if dead:
        summary += f"\ndead sessions: {', '.join(dead)} (/kill to clean up)"
    try:
        await app.bot.send_message(config.OWNER_ID, summary)
    except Exception:
        pass
    state.audit("startup", summary)


def register(app: Application) -> None:
    owner = filters.User(user_id=config.OWNER_ID)
    cmds = {
        "help": cmd_help, "start": cmd_help,
        "ls": cmd_ls, "mkdir": cmd_mkdir, "new": cmd_new,
        "adopt": cmd_adopt, "revive": cmd_revive,
        "restart": cmd_restart, "uptime": cmd_uptime,
        "list": cmd_list, "switch": cmd_switch, "status": cmd_status,
        "say": cmd_say, "port": cmd_port,
        "push": cmd_push, "deploy": cmd_deploy,
        "kill": cmd_kill, "killall": cmd_killall,
        "pause": cmd_pause, "resume": cmd_resume,
        "mode": cmd_mode, "esc": cmd_esc, "key": cmd_key,
    }
    for name, fn in cmds.items():
        app.add_handler(CommandHandler(name, fn, filters=owner))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(owner & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(owner & (filters.Document.ALL | filters.PHOTO), on_file))
    # after all CommandHandlers: unmatched /commands pass through to the agent
    app.add_handler(MessageHandler(owner & filters.COMMAND, on_unknown_command))
