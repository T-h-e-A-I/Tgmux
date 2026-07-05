"""Async tmux wrapper: spawn/kill sessions, send keys, capture panes (plan §4)."""

import asyncio
from pathlib import Path
from typing import Optional

from . import config

SESSION_PREFIX = "proj-"


def session_name(slug: str) -> str:
    return f"{SESSION_PREFIX}{slug}"


async def _tmux(*args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "tmux", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace")


async def new_session(name: str, cwd: str, env: Optional[dict[str, str]] = None) -> bool:
    args = ["new-session", "-d", "-s", name, "-c", cwd, "-x", "220", "-y", "50"]
    for k, v in (env or {}).items():
        args += ["-e", f"{k}={v}"]
    rc, _ = await _tmux(*args)
    return rc == 0


async def start_claude(name: str, extra_env: Optional[dict[str, str]] = None) -> None:
    """Launch the interactive Claude Code REPL inside an existing session."""
    prefix = ""
    for k, v in (extra_env or {}).items():
        prefix += f"{k}={v} "
    await send_text(name, f"{prefix}{config.CLAUDE_BIN}")


async def send_text(name: str, text: str) -> None:
    """Send literal text followed by Enter (plan §4.2)."""
    await _tmux("send-keys", "-t", name, "-l", text)
    await asyncio.sleep(0.3)  # let the TUI ingest the paste before Enter
    await _tmux("send-keys", "-t", name, "Enter")


async def send_key(name: str, key: str) -> None:
    await _tmux("send-keys", "-t", name, key)


async def capture(name: str, lines: int = 200) -> str:
    """Rendered pane text (tmux strips ANSI attributes without -e)."""
    rc, out = await _tmux("capture-pane", "-t", name, "-p", "-S", f"-{lines}")
    return out if rc == 0 else ""


async def has_session(name: str) -> bool:
    rc, _ = await _tmux("has-session", "-t", name)
    return rc == 0


async def kill_session(name: str) -> bool:
    rc, _ = await _tmux("kill-session", "-t", name)
    return rc == 0


async def list_sessions() -> list[str]:
    rc, out = await _tmux("list-sessions", "-F", "#{session_name}")
    if rc != 0:
        return []
    return [s for s in out.splitlines() if s.startswith(SESSION_PREFIX)]


async def pipe_to_log(name: str, slug: str) -> None:
    """Raw audit log of everything the pane emits (plan §4.3-B, used for audit)."""
    log = Path(config.LOG_DIR) / f"{slug}.log"
    await _tmux("pipe-pane", "-o", "-t", name, f"cat >> {log}")
