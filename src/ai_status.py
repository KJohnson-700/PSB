"""Single place to describe whether LLM calls can actually run (config + keys)."""

from typing import Any, Dict, List, Optional


def compute_ai_status(
    config: Dict[str, Any],
    api_keys: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Return dashboard/log-friendly AI readiness.

    ``ready`` is True only when ai.enabled, provider_chain non-empty, and every
    provider's ``api_key_secret`` is present in ``api_keys`` (or in ``os.environ``
    when ``api_keys`` is None — used by dashboard when bot is not running).
    """
    import os

    ai = config.get("ai") or {}
    enabled = bool(ai.get("enabled", True))
    live_inferencing = bool(ai.get("live_inferencing", True))
    chain: List[Dict[str, Any]] = list(ai.get("provider_chain") or [])
    missing: List[str] = []
    usable = 0

    def _has_secret(name: str) -> bool:
        if not name:
            return False
        if api_keys is not None:
            v = api_keys.get(name)
            return v is not None and str(v).strip() != ""
        return bool(os.getenv(name))

    # Provider chain is fallthrough: need at least one callable provider (key or local).
    for p in chain:
        if p.get("local"):
            usable += 1
            continue
        sec = p.get("api_key_secret")
        if not sec:
            continue
        name = str(sec)
        if _has_secret(name):
            usable += 1
        else:
            missing.append(name)

    ready = enabled and len(chain) > 0 and usable >= 1

    if not enabled:
        reason = "LLM off (ai.enabled: false) — quant mode only"
    elif not live_inferencing:
        reason = (
            "LLM configured but ai.live_inferencing: false — no live provider calls "
            "(toggle in dashboard to resume)"
        )
    elif not chain:
        reason = "LLM off — provider_chain is empty"
    elif usable == 0:
        reason = f"LLM off — no usable provider (missing env key(s): {', '.join(missing)})"
    elif missing:
        reason = (
            f"LLM on — {usable} usable provider(s) in chain; "
            f"optional fallbacks missing: {', '.join(missing)}"
        )
    else:
        reason = f"LLM on — {len(chain)} provider(s), keys OK"

    return {
        "enabled": enabled,
        "live_inferencing": live_inferencing,
        "chain_count": len(chain),
        "ready": ready,
        "reason": reason,
        "missing_keys": missing,
    }


def format_ai_log_line(status: Dict[str, Any]) -> str:
    """One-line startup message."""
    if status.get("ready") and status.get("live_inferencing", True):
        extra = ""
        miss = status.get("missing_keys") or []
        if miss:
            extra = f"; optional fallback key(s) missing: {', '.join(miss)}"
        return (
            f"AI STATUS: ON — {status['chain_count']} provider(s) in chain, "
            f"≥1 callable provider{extra}."
        )
    if status.get("ready") and not status.get("live_inferencing", True):
        return (
            "AI STATUS: PAUSED — keys OK but ai.live_inferencing is false "
            "(no live LLM calls until toggled on)."
        )
    return f"AI STATUS: OFF — {status.get('reason', 'unknown')}"
