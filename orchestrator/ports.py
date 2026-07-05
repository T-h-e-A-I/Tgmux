"""Port pool management (plan §7)."""

import socket

from . import config, state


def _listening(port: int) -> bool:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.2)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", port))
        s.close()
        return False
    except OSError:
        return True


def allocate() -> int:
    """Lowest port in the pool that is neither assigned in state nor listening."""
    taken = state.used_ports()
    for port in range(config.PORT_MIN, config.PORT_MAX + 1):
        if port in taken:
            continue
        if _listening(port):
            continue
        return port
    raise RuntimeError(f"no free port in {config.PORT_MIN}-{config.PORT_MAX}")


def url_for(port: int) -> str:
    return f"http://{config.vm_host()}:{port}"
