#!/usr/bin/env python3
"""
Preflight checks before running the bot: API keys, config, dashboard index.html sanity, optional CLOB.

 python scripts/preflight.py              # paper-friendly (matches settings trading.dry_run)
  python scripts/preflight.py --require-live   # require wallet + CLOB API creds (go-live rehearsal)
  python scripts/preflight.py --check-clob     # optional authenticated CLOB call (needs keys)

Exit code 0 if all checks pass, non-zero otherwise.
"""
import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def load_env():
    """Load root `.env` + config/secrets.env (same rules as src.main)."""
    try:
        from src.env_bootstrap import load_project_dotenv
    except ImportError as e:
        return False, str(e)
    try:
        load_project_dotenv(REPO_ROOT)
        return True, None
    except Exception as e:
        return False, str(e)


def check_config():
    """Load and validate config/settings.yaml. Returns (errors, dry_run)."""
    config_path = REPO_ROOT / "config" / "settings.yaml"
    if not config_path.exists():
        return [f"Config not found: {config_path}"], True
    try:
        import yaml

        with open(config_path, encoding="utf-8") as f:
            config = yaml.safe_load(f)
    except Exception as e:
        return [f"Failed to load config: {e}"], True

    errors = []
    dry_run = config.get("trading", {}).get("dry_run", True)
    if not config.get("trading"):
        errors.append("config: missing 'trading' section")
    else:
        tr = config["trading"]
        if "max_position_size" in tr and (
            not isinstance(tr["max_position_size"], (int, float))
            or tr["max_position_size"] <= 0
        ):
            errors.append("config: trading.max_position_size must be positive")
    if not config.get("risk"):
        errors.append("config: missing 'risk' section")
    if not config.get("polymarket", {}).get("api_endpoint"):
        errors.append("config: polymarket.api_endpoint not set")
    return errors, dry_run


def check_api_keys(*, require_trading_keys: bool):
    """
    If require_trading_keys is False (paper / dry_run in config): private key + CLOB API optional.
    If True (--require-live or settings dry_run false): require private key + full CLOB API creds.
    """
    errors = []
    notes = []

    ai_keys = [
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "ANTHROPIC_API_KEY",
        "GROQ_API_KEY",
        "GOOGLE_API_KEY",
        "MINIMAX_API_KEY",
    ]
    if not any(os.getenv(k) for k in ai_keys):
        errors.append(
            "No AI provider key found. Set at least one of: " + ", ".join(ai_keys)
        )

    private_key = os.getenv("PRIVATE_KEY") or os.getenv("POLYMARKET_PRIVATE_KEY")
    polymarket_creds = [
        "POLYMARKET_API_KEY",
        "POLYMARKET_API_SECRET",
        "POLYMARKET_API_PASSPHRASE",
    ]
    missing_pm = [k for k in polymarket_creds if not os.getenv(k)]

    if require_trading_keys:
        if not private_key:
            errors.append(
                "PRIVATE_KEY or POLYMARKET_PRIVATE_KEY required for live / --require-live"
            )
        if missing_pm:
            errors.append(
                "Polymarket CLOB API creds incomplete: " + ", ".join(missing_pm)
            )
    else:
        if not private_key:
            notes.append(
                "No PRIVATE_KEY / POLYMARKET_PRIVATE_KEY — OK for paper (dry_run); add before live."
            )
        if missing_pm:
            notes.append(
                "Polymarket API key/secret/passphrase missing — OK for paper; required for live CLOB."
            )

    return errors, notes


def check_dashboard_index():
    """Static guard: fetchAll() must bind one variable per Promise.all fetch (avoids ReferenceError in browser)."""
    path = REPO_ROOT / "src" / "dashboard" / "index.html"
    if not path.exists():
        return [f"Dashboard index missing: {path}"]
    import re

    text = path.read_text(encoding="utf-8")
    m = re.search(
        r"(?:const )?\[([^\]]+)\]\s*=\s*await Promise\.all\(\[([\s\S]*?)\]\);\s*\n\s*\} catch",
        text,
    )
    if not m:
        return [
            "Dashboard index.html: could not parse fetchAll() Promise.all block — UI may be broken in-browser."
        ]
    names = [x.strip() for x in m.group(1).split(",")]
    block = m.group(2)
    fetches = len(re.findall(r"\bfetch\(", block))
    if len(names) != fetches:
        return [
            f"Dashboard fetchAll: {len(names)} destructured vars vs {fetches} fetch() calls in Promise.all — fix index.html (dashboard will throw ReferenceError)."
        ]
    return []


def check_kill_switch():
    """Warn if kill switch is active."""
    kill_file = REPO_ROOT / "data" / "KILL_SWITCH"
    if kill_file.exists():
        return [
            "Kill switch is ACTIVE (data/KILL_SWITCH exists). Bot will not place trades until removed."
        ]
    return []


def check_clob(*, require_keys: bool):
    """Try CLOB get_positions. Skips without error in paper mode if no private key."""
    sys.path.insert(0, str(REPO_ROOT))
    pk = os.getenv("PRIVATE_KEY") or os.getenv("POLYMARKET_PRIVATE_KEY")
    if not pk:
        if require_keys:
            return ["CLOB check: no PRIVATE_KEY / POLYMARKET_PRIVATE_KEY"]
        print("NOTE: Skipping CLOB check (no private key; paper mode).")
        return []

    try:
        import asyncio
        import yaml

        with open(REPO_ROOT / "config" / "settings.yaml", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        from src.execution.clob_client import CLOBClient

        client = CLOBClient(config)
        client.set_credentials(
            private_key=pk,
            api_key=os.getenv("POLYMARKET_API_KEY"),
            api_secret=os.getenv("POLYMARKET_API_SECRET"),
            api_passphrase=os.getenv("POLYMARKET_API_PASSPHRASE"),
        )

        async def run():
            return await client.get_positions()

        asyncio.run(run())
        return []
    except Exception as e:
        return [f"CLOB connectivity check failed: {e}"]


def main():
    ap = argparse.ArgumentParser(description="Preflight checks for PolyBot")
    ap.add_argument(
        "--check-clob",
        action="store_true",
        help="Verify CLOB API connectivity (needs private key + API creds)",
    )
    ap.add_argument(
        "--require-live",
        action="store_true",
        help="Require wallet + Polymarket API creds (ignore paper-only leniency)",
    )
    args = ap.parse_args()

    all_errors = []

    ok, err = load_env()
    if not ok:
        all_errors.append(f"Env: {err}")

    cfg_errors, yaml_dry_run = check_config()
    all_errors.extend(cfg_errors)

    require_trading_keys = bool(args.require_live or not yaml_dry_run)
    key_errors, key_notes = check_api_keys(require_trading_keys=require_trading_keys)
    all_errors.extend(key_errors)
    for n in key_notes:
        print("NOTE:", n)

    kill_errors = check_kill_switch()
    for e in kill_errors:
        print("WARNING:", e)

    all_errors.extend(check_dashboard_index())

    if args.check_clob:
        all_errors.extend(check_clob(require_keys=require_trading_keys))

    if all_errors:
        print("Preflight FAILED:")
        for e in all_errors:
            print("  -", e)
        return 1
    print("Preflight OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
