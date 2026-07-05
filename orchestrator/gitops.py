"""Git / GitHub / Vercel operations (plan §6).

Headless `claude -p` is used only for commit-message generation, with a tight
tool allowlist (plan §9.6). Everything else is direct CLI.
"""

import asyncio
import re
from typing import Optional

from . import config, state

URL_RE = re.compile(r"https://[\w.-]+\.vercel\.app\S*")


async def run(cmd: list[str], cwd: str, timeout: float = 600,
              stdin: Optional[str] = None) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(
            proc.communicate(stdin.encode() if stdin is not None else None),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        return 124, f"timed out after {timeout}s: {' '.join(cmd)}"
    return proc.returncode or 0, out.decode(errors="replace").strip()


async def _git(path: str, *args: str, timeout: float = 120) -> tuple[int, str]:
    return await run(["git", *args], cwd=path, timeout=timeout)


async def generate_commit_message(path: str) -> str:
    """One-line commit message via headless claude -p; safe fallback."""
    rc, stat = await _git(path, "diff", "--cached", "--stat")
    if rc != 0 or not stat:
        return "wip: agent update"
    rc, msg = await run(
        [config.CLAUDE_BIN, "-p",
         "Write a single-line conventional commit message (max 70 chars) for "
         "this staged diff. Output ONLY the message, nothing else.",
         "--allowedTools", ""],
        cwd=path, timeout=60, stdin=stat,
    )
    msg = (msg or "").strip().splitlines()[0][:72] if rc == 0 and msg.strip() else ""
    return msg or "wip: agent update"


async def push_dev(slug: str, path: str) -> tuple[bool, str]:
    """git add/commit/push to dev; create the GitHub repo if there's no remote."""
    log: list[str] = []

    rc, _ = await _git(path, "rev-parse", "--git-dir")
    if rc != 0:
        await _git(path, "init", "-b", "dev")
        log.append("initialized git repo (branch dev)")

    await _git(path, "add", "-A")

    rc, _ = await _git(path, "diff", "--cached", "--quiet")
    if rc != 0:  # something staged
        msg = await generate_commit_message(path)
        rc, out = await _git(path, "commit", "-m", msg)
        if rc != 0:
            return False, f"commit failed:\n{out}"
        log.append(f"committed: {msg}")
    else:
        log.append("nothing new to commit")

    # make sure we're on dev
    rc, branch = await _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    if branch != "dev":
        await _git(path, "checkout", "-B", "dev")
        log.append(f"switched {branch} -> dev")

    rc, _ = await _git(path, "remote", "get-url", "origin")
    if rc != 0:
        rc, out = await run(
            ["gh", "repo", "create", slug, "--private",
             "--source=.", "--remote=origin", "--push"],
            cwd=path, timeout=180,
        )
        if rc != 0:
            return False, f"gh repo create failed:\n{out}"
        state.set_field(slug, "github_repo", _extract_repo(out) or slug)
        log.append("created private GitHub repo + pushed")
    else:
        rc, out = await _git(path, "push", "-u", "origin", "dev", timeout=300)
        if rc != 0:
            return False, f"push failed:\n{out}"
        log.append("pushed dev")

    state.audit("push_dev", "; ".join(log), slug)
    return True, "\n".join(log)


def _extract_repo(gh_output: str) -> Optional[str]:
    m = re.search(r"github\.com/([\w.-]+/[\w.-]+)", gh_output)
    return m.group(1) if m else None


async def vercel_preview(slug: str, path: str) -> tuple[bool, str]:
    """Create/refresh a preview deployment; return the preview URL."""
    rc, out = await run(["vercel", "deploy", "--yes"], cwd=path, timeout=600)
    if rc != 0:
        return False, f"vercel preview failed:\n{out[-1500:]}"
    urls = URL_RE.findall(out)
    if not urls:
        return False, f"vercel finished but no URL found:\n{out[-800:]}"
    state.set_field(slug, "vercel_project", slug)
    state.audit("vercel_preview", urls[-1], slug)
    return True, urls[-1]


async def deploy_prod(slug: str, path: str) -> tuple[bool, str]:
    """Gate-approved: merge dev -> prod, push, production deploy (plan §6)."""
    log: list[str] = []

    rc, out = await _git(path, "checkout", "-B", "prod")
    if rc != 0:
        return False, f"checkout prod failed:\n{out}"
    rc, out = await _git(path, "merge", "dev", "--no-edit")
    if rc != 0:
        await _git(path, "merge", "--abort")
        await _git(path, "checkout", "dev")
        return False, f"merge dev->prod failed (aborted):\n{out}"
    log.append("merged dev -> prod")

    rc, out = await _git(path, "push", "-u", "origin", "prod", timeout=300)
    if rc == 0:
        log.append("pushed prod")
    else:
        log.append(f"push prod failed (continuing to vercel): {out[-300:]}")

    await _git(path, "checkout", "dev")  # leave the agent back on dev

    rc, out = await run(["vercel", "--prod", "--yes"], cwd=path, timeout=900)
    if rc != 0:
        return False, f"vercel --prod failed:\n{out[-1500:]}"
    urls = URL_RE.findall(out)
    url = urls[-1] if urls else "(no URL parsed — check `vercel ls`)"
    log.append(f"production: {url}")

    state.audit("deploy_prod", url, slug)
    return True, "\n".join(log)
