"""The tmux <-> Telegram bridge (plan §4): relay loop, pane cleaning,
new-content diffing, and question detection.

Relay strategy: poll `capture-pane` on the rendered screen and diff cleaned
lines. Claude Code is a full TUI, so a raw pipe-pane stream is redraw soup;
the rendered pane is the sane thing to parse. pipe-pane still runs, but only
as a raw per-agent audit log.
"""

import asyncio
import difflib
import hashlib
import json
import logging
import re
import time
from collections import deque
from typing import Callable, Awaitable

log = logging.getLogger(__name__)

from . import config, state, tmuxctl

ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[a-zA-Z]"          # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    r"|\x1b[()][0B]"                   # charset
    r"|[\x00-\x08\x0b-\x1f\x7f]"       # stray control chars
)

# Pure TUI decoration / chrome — dropped entirely.
NOISE_RES = [
    re.compile(r"^[\s╭╮╰╯─│┌┐└┘├┤┬┴┼═╔╗╚╝║>]+$"),
    re.compile(r"\?\s+for shortcuts"),
    re.compile(r"esc to interrupt"),
    re.compile(r"ctrl\+[a-z] to"),
    re.compile(r"auto-accept edits|bypass permissions|plan mode|shift\+tab"),
    re.compile(r"^\s*[✳✻✽✶✢·∗＊*+]\s+\S+…"),                 # spinner lines
    re.compile(r"^\s*[✳✻✽✶✢·∗＊*+]\s+(Thinking|Running|Wibbling)", re.I),
    re.compile(r"^\s*[✳✻✽✶✢·∗＊*+]\s+\w+ for \d"),           # "✻ Worked for 7s"
    re.compile(r"\d+\s+tokens"),
    re.compile(r"claude\s+(-{1,2}\S+\s*)*$"),                  # the launch command echo
    # live input-box line ("❯" / "❯ half-typed text") — but NOT menu items "❯ 1. Yes"
    re.compile(r"^\s*❯(?!\s*\d+\.)"),
    # transcript echo of the user's own messages ("> hello") — already ack'd with "→ agent"
    re.compile(r"^\s*>\s"),
]

# Only real BLOCKING prompts (permission dialogs, menus) trigger a 🔶 ping.
# Conversational questions arrive as normal relayed content.
QUESTION_RES = [
    re.compile(r"do you want", re.I),
    re.compile(r"\(y/n\)", re.I),
    re.compile(r"❯\s*\d+\."),                     # selection menu cursor
    re.compile(r"^\s*\d+\.\s*(yes|no)\b", re.I),  # numbered yes/no options
    re.compile(r"(select|choose)( an?)? option", re.I),
    re.compile(r"press enter to", re.I),
    re.compile(r"waiting for (your|user)", re.I),
]


def clean_pane(raw: str) -> list[str]:
    """ANSI-strip, unbox, drop chrome, collapse blank runs."""
    out: list[str] = []
    for line in raw.splitlines():
        line = ANSI_RE.sub("", line)
        if any(rx.search(line) for rx in NOISE_RES):
            continue
        # Keep dialog/box *content*: strip the │ frame but keep inner text.
        stripped = line.strip()
        if stripped.startswith("│"):
            stripped = stripped[1:]
        if stripped.endswith("│"):
            stripped = stripped[:-1]
        stripped = stripped.rstrip()
        if not stripped.strip():
            if out and out[-1] == "":
                continue
            out.append("")
            continue
        out.append(stripped)
    while out and out[0] == "":
        out.pop(0)
    while out and out[-1] == "":
        out.pop()
    return out


def diff_new_lines(prev: list[str], cur: list[str]) -> list[str]:
    """Lines in `cur` after the previous snapshot's content tail.

    Anchors on the longest suffix of `prev` found intact in `cur` (searched
    from the end), so shared mid-screen lines can't fool it into treating
    real new content as already-seen.
    """
    if not prev:
        return cur
    for k in range(min(len(prev), 40), 0, -1):
        tail = prev[-k:]
        for i in range(len(cur) - k, -1, -1):
            if cur[i:i + k] == tail:
                return cur[i + k:]
    # tail scrolled entirely out of capture: fall back to largest common block
    sm = difflib.SequenceMatcher(a=prev, b=cur, autojunk=False)
    best = max(sm.get_matching_blocks(), key=lambda b: b.size)
    return cur[best.b + best.size:] if best.size else cur


def detect_question(lines: list[str]) -> str | None:
    """If the tail of the pane looks like the agent is waiting on the user,
    return that tail as context; else None."""
    tail = [l for l in lines[-15:] if l.strip()]
    if not tail:
        return None
    for i, line in enumerate(tail):
        for rx in QUESTION_RES:
            if rx.search(line):
                return "\n".join(tail[max(0, i - 3):])
    return None


def _qhash(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


# Echo suppression: the TUI echoes injected input as "> text", and long
# messages WRAP — continuation lines lack the "> " marker, so the noise
# filter alone can't catch them. Remember what we typed into each agent
# and drop any relayed line that is a fragment of a recent send.

_RECENT_SENT: dict[str, deque] = {}
_ECHO_TTL = 600.0  # seconds a sent message keeps suppressing its echo


def note_sent(slug: str, text: str) -> None:
    q = _RECENT_SENT.setdefault(slug, deque(maxlen=20))
    q.append((" ".join(text.split()), time.time()))


def is_echo(slug: str, line: str) -> bool:
    frag = " ".join(line.lstrip("> ").split())
    if len(frag) < 4:  # too short to match meaningfully
        return False
    now = time.time()
    return any(
        now - ts < _ECHO_TTL and frag in sent
        for sent, ts in _RECENT_SENT.get(slug, ())
    )


# Baseline = last pane snapshot that was relayed (or accepted as already-seen).
# Persisted so a daemon /restart doesn't swallow output produced in between.

def _baseline_path(slug: str):
    return config.LOG_DIR / f"{slug}.baseline.json"


def load_baseline(slug: str) -> list[str] | None:
    try:
        return json.loads(_baseline_path(slug).read_text())
    except (OSError, ValueError):
        return None


def save_baseline(slug: str, lines: list[str]) -> None:
    try:
        _baseline_path(slug).write_text(json.dumps(lines))
    except OSError:
        log.exception("could not persist baseline for %s", slug)


def drop_baseline(slug: str) -> None:
    _baseline_path(slug).unlink(missing_ok=True)


async def relay_loop(
    slug: str,
    notify: Callable[[str, str, bool], Awaitable[None]],
) -> None:
    """Per-agent bridge task.

    notify(slug, text, is_question) sends to Telegram (implemented in bot.py).

    Behavior:
      - new meaningful pane content -> coalesced, throttled progress messages
        (suppressed while the agent is paused/muted)
      - question detected on a stable pane -> ping + status WAITING_FOR_ME
        (always sent, even when muted — that is the point of the bridge)
      - pane unchanged for IDLE_TIMEOUT -> status IDLE
      - tmux session gone -> status DEAD, final notice, task exits
    """
    agent0 = state.get_agent(slug)
    if agent0 is None:
        return
    sess = agent0["tmux_session"]

    # Baseline: last relayed snapshot. Persisted across daemon restarts so
    # output produced around a /restart still gets delivered. Only on the
    # very first attach do we seed from the current screen (skip backlog).
    baseline = load_baseline(slug)
    if baseline is None:
        baseline = clean_pane(await tmuxctl.capture(sess)) \
            if await tmuxctl.has_session(sess) else []
        save_baseline(slug, baseline)

    last_seen = baseline
    last_sent = 0.0
    last_change = time.time()
    last_question = ""

    while True:
        await asyncio.sleep(config.POLL_INTERVAL)
        agent = state.get_agent(slug)
        if agent is None:
            return
        if not await tmuxctl.has_session(sess):
            state.set_status(slug, "DEAD")
            await notify(slug, "tmux session ended — agent is gone. /new to respawn.", True)
            return

        cur = clean_pane(await tmuxctl.capture(sess))
        now = time.time()

        if cur != last_seen:  # pane still moving: just track, don't diff yet
            last_seen = cur
            last_change = now
            if agent["status"] != "BUILDING":
                state.set_status(slug, "BUILDING")
            continue

        # --- pane is stable: diff the snapshot against the last relayed one ---
        new = [l for l in diff_new_lines(baseline, cur) if not is_echo(slug, l)]
        while new and not new[0].strip():
            new.pop(0)
        while new and not new[-1].strip():
            new.pop()

        question = detect_question(cur)
        if question and _qhash(question) != last_question:
            last_question = _qhash(question)
            state.set_status(slug, "WAITING_FOR_ME")
            body = "\n".join(new).strip() or question
            baseline = cur
            save_baseline(slug, baseline)
            await notify(slug, body, True)
            last_sent = now
            continue

        if new and not agent["paused"] and now - last_sent >= config.RELAY_MIN_INTERVAL:
            baseline = cur
            save_baseline(slug, baseline)
            await notify(slug, "\n".join(new), False)
            last_sent = now

        if (
            agent["status"] == "BUILDING"
            and now - last_change >= config.IDLE_TIMEOUT
        ):
            state.set_status(slug, "IDLE")
