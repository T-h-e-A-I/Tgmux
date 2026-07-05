"""Configuration: .env loading, paths, tunables."""

import os
import socket
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("TELEGRAM_OWNER_ID", "0") or "0")

PROJECTS_DIR = Path(os.environ.get("PROJECTS_DIR", "/root/projects"))
STATE_DB = Path(os.environ.get("STATE_DB", str(ROOT / "state.db")))
LOG_DIR = Path(os.environ.get("LOG_DIR", str(ROOT / "logs")))

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

PORT_MIN = int(os.environ.get("PORT_MIN", "3001"))
PORT_MAX = int(os.environ.get("PORT_MAX", "3099"))

# Bridge tunables (seconds)
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "2.0"))
RELAY_MIN_INTERVAL = float(os.environ.get("RELAY_MIN_INTERVAL", "5.0"))
IDLE_TIMEOUT = float(os.environ.get("IDLE_TIMEOUT", "60"))

_VM_HOST = os.environ.get("VM_HOST", "")


def vm_host() -> str:
    """Public host/IP used in /port URLs. Auto-detects the primary interface IP."""
    global _VM_HOST
    if _VM_HOST:
        return _VM_HOST
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        _VM_HOST = s.getsockname()[0]
        s.close()
    except OSError:
        _VM_HOST = "127.0.0.1"
    return _VM_HOST


def ensure_dirs() -> None:
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
