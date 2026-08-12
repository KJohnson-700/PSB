#!/usr/bin/env python3
"""VPN / egress-path check for the monitoring tick.

WHY THIS EXISTS (2026-08-12): Claude reported "no VPN is running" and recommended
running the bot bare. That was WRONG and would have geoblocked the bot. The check
that produced it only looked at ``utun*`` interfaces — but this box runs a native
macOS **IPsec/IKEv2** VPN on ``ipsec0``, which that grep could never see. The VPN
also holds the DEFAULT ROUTE, i.e. every Polymarket request egresses through it, so
"is the VPN up" is not cosmetic: it is why ``geoblock`` is 0.

So this script asserts the thing that actually matters — **which interface the
default route points at** — rather than pattern-matching interface names. It checks
every VPN interface family (utun / ipsec / ppp / gpd), not one of them.

WHAT IT REPORTS
  * vpn_interfaces  — every non-loopback tunnel iface WITH an IPv4 (an iface with no
    address is an inactive macOS system tunnel, not a live VPN; utun0-3 are always
    "UP,RUNNING" and mean nothing on their own).
  * default_route   — the interface the default route uses, and whether it is a tunnel.
  * dns             — do the Polymarket hosts resolve right now.
  * log_signals     — geoblock hits + DNS NameResolutionError bursts in the bot log,
    time-filtered to a recent window (a burst 6 hours ago is not a live fault).

STATUS
  critical : default route NOT via a tunnel (bare egress => geoblock risk), or a
             Polymarket host fails to resolve, or geoblock hits in the window.
  warn     : tunnel up and routing, but DNS errors inside the window (tunnel blip).
  ok       : tunnel up, holds default route, hosts resolve, no signals.

Exit code = 0 ok / 1 warn / 2 critical, so a tick can branch on it. ``--json`` emits
a machine-readable row. Never raises: any probe failure degrades to "unknown" for
that field rather than taking the monitoring tick down with it.

Usage:
    .venv/bin/python scripts/vpn_check.py
    .venv/bin/python scripts/vpn_check.py --json --window-min 20
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta

# Tunnel interface families. NOT just utun — the miss that caused this script.
TUNNEL_PREFIXES = ("utun", "ipsec", "ppp", "gpd", "tap", "tun")
POLYMARKET_HOSTS = ("gamma-api.polymarket.com", "clob.polymarket.com")
_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def _run(cmd: list[str], timeout: float = 6.0) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.stdout or ""
    except Exception:
        return ""


def _is_tunnel(ifc: str) -> bool:
    return any(ifc.startswith(p) for p in TUNNEL_PREFIXES)


def vpn_interfaces() -> list[dict]:
    """Tunnel interfaces that actually carry an IPv4. Name alone proves nothing."""
    out = _run(["ifconfig"])
    found, cur = [], None
    for line in out.splitlines():
        m = re.match(r"^([a-z0-9]+):", line)
        if m:
            cur = m.group(1)
            continue
        if cur and _is_tunnel(cur):
            m2 = re.search(r"\binet (\d+\.\d+\.\d+\.\d+)", line)
            if m2:
                found.append({"iface": cur, "ipv4": m2.group(1)})
    return found


def default_route() -> dict:
    """Which interface the default route egresses through — the load-bearing fact."""
    out = _run(["netstat", "-rn", "-f", "inet"])
    for line in out.splitlines():
        parts = line.split()
        if parts and parts[0] == "default" and len(parts) >= 4:
            ifc = parts[-1]
            return {"iface": ifc, "via_tunnel": _is_tunnel(ifc), "raw": line.strip()}
    return {"iface": None, "via_tunnel": None, "raw": None}


def dns_ok() -> dict:
    res = {}
    for host in POLYMARKET_HOSTS:
        out = _run(["dscacheutil", "-q", "host", "-a", "name", host])
        m = re.search(r"ip_address:\s*(\d+\.\d+\.\d+\.\d+)", out)
        res[host] = m.group(1) if m else None
    return res


def log_signals(log_path: str, window_min: int) -> dict:
    """Geoblock + DNS-failure counts, TIME-FILTERED. An old burst is not a live fault."""
    cutoff = (datetime.now() - timedelta(minutes=window_min)).strftime("%Y-%m-%d %H:%M:%S")
    geo = dns_err = 0
    last_dns = None
    ts = None
    try:
        with open(log_path, errors="ignore") as fh:
            for line in fh:
                m = _TS.match(line)
                if m:
                    ts = m.group(1)
                if ts is None or ts < cutoff:
                    continue
                low = line.lower()
                if "geoblock" in low:
                    geo += 1
                if "NameResolutionError" in line or "Failed to resolve" in line:
                    dns_err += 1
                    last_dns = ts
    except OSError:
        return {"available": False, "geoblock": None, "dns_errors": None,
                "last_dns_error": None, "window_min": window_min}
    return {"available": True, "geoblock": geo, "dns_errors": dns_err,
            "last_dns_error": last_dns, "window_min": window_min}


def check(log_path: str, window_min: int) -> dict:
    ifaces = vpn_interfaces()
    route = default_route()
    dns = dns_ok()
    sig = log_signals(log_path, window_min)

    reasons: list[str] = []
    status = "ok"

    if route.get("via_tunnel") is False:
        status = "critical"
        reasons.append(f"default route via {route.get('iface')} — BARE EGRESS, not a tunnel (geoblock risk)")
    elif route.get("via_tunnel") is None:
        status = "warn"
        reasons.append("could not read default route")

    unresolved = [h for h, ip in dns.items() if not ip]
    if unresolved:
        status = "critical"
        reasons.append("DNS FAILS: " + ", ".join(unresolved))

    if sig.get("geoblock"):
        status = "critical"
        reasons.append(f"{sig['geoblock']} geoblock hits in last {window_min}m")

    if not ifaces:
        # Only meaningful if the route also is not a tunnel; a split setup can route
        # via a tunnel whose address we failed to parse. Do not over-escalate.
        if status == "ok":
            status = "warn"
        reasons.append("no tunnel interface carries an IPv4")

    if sig.get("dns_errors") and status == "ok":
        status = "warn"
        reasons.append(f"{sig['dns_errors']} DNS errors in last {window_min}m "
                       f"(last {sig.get('last_dns_error')}) — possible tunnel blip")

    return {"status": status, "reasons": reasons, "vpn_interfaces": ifaces,
            "default_route": route, "dns": dns, "log_signals": sig}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--window-min", type=int, default=20,
                    help="minutes of bot log to scan for geoblock/DNS signals")
    ap.add_argument("--log", default=None, help="bot log path (default: today's)")
    a = ap.parse_args()

    log_path = a.log or f"data/logs/polybot_{datetime.now():%Y%m%d}.log"
    r = check(log_path, a.window_min)

    if a.json:
        print(json.dumps(r, separators=(",", ":")))
    else:
        icon = {"ok": "OK", "warn": "WARN", "critical": "CRITICAL"}[r["status"]]
        rt = r["default_route"]
        print(f"VPN {icon}")
        print(f"  default route : {rt.get('iface')} (tunnel={rt.get('via_tunnel')})")
        ifs = ", ".join(f"{i['iface']}={i['ipv4']}" for i in r["vpn_interfaces"]) or "(none with IPv4)"
        print(f"  tunnels       : {ifs}")
        for h, ip in r["dns"].items():
            print(f"  dns {h:32} {ip or 'NO RESOLVE'}")
        s = r["log_signals"]
        if s.get("available"):
            print(f"  log({s['window_min']}m)    : geoblock={s['geoblock']} dns_errors={s['dns_errors']}")
        else:
            print("  log           : unavailable")
        for reason in r["reasons"]:
            print(f"  ! {reason}")

    return {"ok": 0, "warn": 1, "critical": 2}[r["status"]]


if __name__ == "__main__":
    sys.exit(main())
