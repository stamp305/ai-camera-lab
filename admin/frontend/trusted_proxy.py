"""The caller's real IP when we sit behind our own reverse proxy (nginx etc.).

X-Forwarded-For is only believed hop by hop from the right: starting at the TCP peer, while
the current hop is a proxy we trust (TRUSTED_PROXIES), step one entry left. The first hop
that is not ours is the caller. A header sent by anyone else, or entries a caller put in
front of ours, are never used, so nobody can claim an address they do not have.

TRUSTED_PROXIES: comma-separated IPs/CIDRs. Default = loopback only (nginx on this host).
"""
from __future__ import annotations

import ipaddress
import os

_DEFAULT = "127.0.0.1/32,::1/128"


def _parse(spec: str) -> tuple:
    nets = []
    for part in (spec or "").split(","):
        part = part.strip()
        if part:
            try:
                nets.append(ipaddress.ip_network(part, strict=False))
            except ValueError:
                raise SystemExit(f"TRUSTED_PROXIES: not an IP or CIDR: {part!r}")
    return tuple(nets)


def _addr(s: str):
    try:
        a = ipaddress.ip_address(s.strip().strip("[]"))
        return a.ipv4_mapped or a if a.version == 6 else a
    except ValueError:
        return None


class TrustedProxies:
    def __init__(self, spec: str | None = None):
        self.nets = _parse(_DEFAULT if spec is None else spec)

    def trusted(self, a) -> bool:
        return a is not None and any(a.version == n.version and a in n for n in self.nets)

    def real_ip(self, peer: str, forwarded_for: str = "") -> str:
        cur = _addr(peer or "")
        if cur is None:
            return ""
        hops = [h for h in (forwarded_for or "").split(",") if h.strip()][-20:]
        while self.trusted(cur) and hops:
            nxt = _addr(hops.pop())
            if nxt is None:           # garbage from a client: stop at the last hop we trust
                break
            cur = nxt
        return str(cur)


def from_env(name: str) -> TrustedProxies:
    return TrustedProxies(os.environ.get(name))
