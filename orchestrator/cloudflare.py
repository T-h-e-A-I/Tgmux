"""Cloudflare DNS operations for custom subdomains (used by /domain).

One tap in Telegram gives a project a real subdomain: bot.py attaches the
domain in Vercel (gitops.attach_domain) and then calls ensure_cname() here to
point <sub>.<base> at Vercel's edge. The CNAME is left UNPROXIED (grey-cloud)
so Vercel can complete its ACME challenge and issue the TLS cert.

Auth: a scoped API token (Zone.DNS:Edit on the one zone) in
CLOUDFLARE_API_TOKEN. The zone's apex is DOMAIN_BASE. Both live in .env.
"""

import logging
from typing import Optional

import httpx

from . import config

log = logging.getLogger(__name__)

API = "https://api.cloudflare.com/client/v4"
VERCEL_TARGET = "cname.vercel-dns.com"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.CLOUDFLARE_API_TOKEN}",
        "Content-Type": "application/json",
    }


def configured() -> bool:
    return bool(config.CLOUDFLARE_API_TOKEN and config.DOMAIN_BASE)


async def _zone_id(client: httpx.AsyncClient, base: str) -> Optional[str]:
    r = await client.get(f"{API}/zones", params={"name": base}, headers=_headers())
    data = r.json()
    res = data.get("result") or []
    return res[0]["id"] if data.get("success") and res else None


async def ensure_cname(sub: str, base: str,
                       target: str = VERCEL_TARGET) -> tuple[bool, str]:
    """Idempotently point <sub>.<base> at `target`, unproxied. Creates the
    record, or updates an existing one so re-running /domain is safe."""
    if not config.CLOUDFLARE_API_TOKEN:
        return False, "CLOUDFLARE_API_TOKEN not set in .env"
    fqdn = f"{sub}.{base}"
    body = {"type": "CNAME", "name": sub, "content": target,
            "proxied": False, "ttl": 1}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            zid = await _zone_id(client, base)
            if not zid:
                return False, f"no Cloudflare zone for {base} (token scope?)"

            r = await client.get(f"{API}/zones/{zid}/dns_records",
                                  params={"name": fqdn}, headers=_headers())
            existing = (r.json().get("result") or [])
            if existing:
                rid = existing[0]["id"]
                r = await client.put(f"{API}/zones/{zid}/dns_records/{rid}",
                                     json=body, headers=_headers())
                verb = "updated"
            else:
                r = await client.post(f"{API}/zones/{zid}/dns_records",
                                      json=body, headers=_headers())
                verb = "created"

            data = r.json()
            if not data.get("success"):
                errs = "; ".join(e.get("message", "") for e in data.get("errors", []))
                return False, f"Cloudflare API error: {errs or r.status_code}"
            return True, f"{verb} CNAME {fqdn} → {target} (DNS-only)"
    except (httpx.HTTPError, ValueError, KeyError) as e:
        log.exception("cloudflare ensure_cname failed")
        return False, f"Cloudflare request failed: {e}"
