# CLAUDE.md — Tgmux (self-improvement rules)

This repo IS the Telegram orchestrator that is relaying your messages. You are
editing the running system; be surgical.

- Python venv: `.venv/bin/python`. After ANY code change run
  `.venv/bin/python -m py_compile orchestrator/*.py` — a syntax error here
  bricks the bot the owner controls you with.
- The daemon must be restarted to pick up your changes. You cannot restart it
  yourself: when your change is done and compile-checked, tell the owner to
  send /restart, then stop and wait.
- NEVER kill tmux sessions named `proj-*` and never run `pkill -f orchestrator`
  — that includes your own session and the daemon relaying this chat.
- Never touch `.env`, `state.db`, or `logs/` contents.
- Architecture map: `orchestrator/bridge.py` (tmux↔TG relay + question
  detection), `bot.py` (commands/routing), `tmuxctl.py`, `gitops.py`
  (push/deploy), `state.py` (SQLite+audit), `ports.py`, `config.py`.
  The design doc is `Plan.md`; keep `README.md` in sync with what you change.
- When you need a decision from the owner, ask ONE short question and wait —
  replies arrive via Telegram on a phone.
- Small, reviewable changes. Do not push or deploy; the owner gates that.
