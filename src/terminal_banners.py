"""ASCII terminal banners for PolyBot startup and shutdown (Ctrl+C / SIGTERM)."""

from __future__ import annotations

import os
import signal
import sys
from typing import Any, Dict, List, Optional

__all__ = [
    "framed_lines",
    "resolve_dashboard_display_url",
    "print_startup_banner",
    "print_shutdown_banner",
]


def framed_lines(title: str, inner_lines: List[str], inner_width: int = 58) -> str:
    """Build a fixed-width ASCII box. inner_width = visible text column (between '| ' and ' |')."""

    def pad(s: str) -> str:
        if len(s) > inner_width:
            s = s[: inner_width - 1] + "…"
        return s + " " * (inner_width - len(s))

    border = "+" + "-" * (inner_width + 2) + "+"
    out: List[str] = [border, "| " + pad(title) + " |"]
    for line in inner_lines:
        out.append("| " + pad(line) + " |")
    out.append(border)
    return "\n".join(out)


def resolve_dashboard_display_url(config: Dict[str, Any]) -> Optional[str]:
    """Match dashboard bind/display rules used in main.start_dashboard (URL for humans)."""
    dashboard = config.get("dashboard") or {}
    if not isinstance(dashboard, dict) or not dashboard.get("enabled", False):
        return None

    if os.environ.get("PORT"):
        port = int(os.environ["PORT"])
        host = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
    else:
        host = str(dashboard.get("host", "127.0.0.1"))
        port = int(dashboard.get("dashboard_port", 8080))

    if os.environ.get("RAILWAY_PUBLIC_DOMAIN"):
        pd = os.environ["RAILWAY_PUBLIC_DOMAIN"].strip()
        return pd if pd.startswith("http") else f"https://{pd}"
    if host == "0.0.0.0":
        return f"http://127.0.0.1:{port}"
    return f"http://{host}:{port}"


def _argv_mode_label() -> str:
    if "--dashboard-only" in sys.argv:
        return "dashboard-only (no trading loop)"
    if "--live" in sys.argv:
        return "live"
    return "paper"


def print_startup_banner(
    *,
    config: Dict[str, Any],
    dry_run: bool,
    session_id: Optional[str] = None,
    file=sys.stdout,
) -> None:
    url = resolve_dashboard_display_url(config)
    mode = _argv_mode_label()
    trading = "dry_run / simulated orders" if dry_run else "LIVE — real orders if keys + venues allow"
    dash_line = f"Dashboard: {url}" if url else "Dashboard: disabled in settings.yaml"
    sess = session_id or "(unknown)"
    inner = [
        f"Mode: {mode}",
        f"Trading: {trading}",
        dash_line,
        f"Session: {sess}",
        "Stop: Ctrl+C (SIGINT) or SIGTERM — cooperative shutdown; see logs for flush.",
    ]
    block = framed_lines("PolyBot — starting", inner)
    print(block, file=file, flush=True)


def _signal_label(sig: int) -> str:
    try:
        return signal.Signals(sig).name  # type: ignore[attr-defined]
    except Exception:
        return {2: "SIGINT", 15: "SIGTERM"}.get(sig, f"SIGNAL_{sig}")


def print_shutdown_banner(sig: Optional[int], *, extra: Optional[str] = None, file=sys.stderr) -> None:
    label = _signal_label(sig) if sig is not None else "KeyboardInterrupt / cancel"
    inner = [
        f"Signal: {label}",
        "Shutdown: tasks cancelled, bot.shutdown() completed (see log lines above).",
    ]
    if extra:
        inner.append(extra[:58])
    block = framed_lines("PolyBot — shutdown", inner)
    print(block, file=file, flush=True)
