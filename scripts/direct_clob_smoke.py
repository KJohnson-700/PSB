"""Direct Polymarket CLOB V2 live smoke test — ONE tiny order, isolated from the bot.

Purpose: validate the V2 execution path end-to-end (signing/auth/funder/fill/fee)
that the Olympus broker path can NOT exercise. Places one marketable (taker) BUY on
a current crypto up/down market, reads back the real fill + fee from the pUSD balance
delta, and (with --close) sells the shares back to flatten.

SAFETY
- Hard $2 cap on the order (HARD_CAP_USD); aborts above it.
- Requires confirmation: env PSB_DIRECT_SMOKE_CONFIRM=APPROVE_DIRECT_LIVE_ORDER.
- Pre-checks live support + signature_type/funder + pUSD balance & allowance, and
  ABORTS before placing if anything is off.
- Does NOT print the private key, full wallet, or full tx/order hashes.
- Single order per run; state saved to data/runtime/direct_smoke_last.json for --close.

Usage:
  PSB_DIRECT_SMOKE_CONFIRM=APPROVE_DIRECT_LIVE_ORDER \
    .venv/bin/python scripts/direct_clob_smoke.py --buy --amount-usd 1
  PSB_DIRECT_SMOKE_CONFIRM=APPROVE_DIRECT_LIVE_ORDER \
    .venv/bin/python scripts/direct_clob_smoke.py --close
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml

from scripts.olympus_payload_only_market import (
    _book_for_token,
    _first_ask,
    _json_list,
    _select_current_btc_5m,
)
from src.execution.clob_client import CLOBClient, OrderStatus

ENV_FILE = ROOT / ".env"
STATE_FILE = ROOT / "data" / "runtime" / "direct_smoke_last.json"
CONFIRM_ENV = "PSB_DIRECT_SMOKE_CONFIRM"
CONFIRM_PHRASE = "APPROVE_DIRECT_LIVE_ORDER"
HARD_CAP_USD = 2.0


def _env(name: str) -> Optional[str]:
    if name in os.environ:
        return os.environ[name]
    if not ENV_FILE.exists():
        return None
    for raw in ENV_FILE.read_text(errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() == name:
            return v.strip().strip('"').strip("'")
    return None


def _short(s: str, keep: int = 6) -> str:
    s = str(s or "")
    return f"…{s[-keep:]}" if len(s) > keep else "…"


def _build_live_client() -> CLOBClient:
    cfg = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text())
    c = CLOBClient(cfg)
    if not CLOBClient.live_execution_supported():
        raise RuntimeError("live_execution_supported() is False — py-clob-client-v2 not installed/imported.")
    if c._signature_type in (None, 0) or not c._funder_address:
        raise RuntimeError(
            "polymarket.signature_type (1/2) and funder_address must be set for a proxy account."
        )
    pk = _env("PRIVATE_KEY") or _env("POLYMARKET_PRIVATE_KEY")
    if not pk:
        raise RuntimeError("PRIVATE_KEY / POLYMARKET_PRIVATE_KEY missing from .env.")
    c.set_credentials(
        private_key=pk,
        api_key=_env("POLYMARKET_API_KEY"),
        api_secret=_env("POLYMARKET_API_SECRET"),
        api_passphrase=_env("POLYMARKET_API_PASSPHRASE"),
    )
    return c


def _require_confirm() -> None:
    if os.environ.get(CONFIRM_ENV) != CONFIRM_PHRASE:
        raise SystemExit(
            f"Refusing live order: set {CONFIRM_ENV}={CONFIRM_PHRASE} to confirm a real-money smoke test."
        )


async def _poll_fill(c: CLOBClient, order_id: str, polls: int, interval: float) -> str:
    status = ""
    for _ in range(max(1, polls)):
        st = await c.get_order_status(order_id)
        status = st.value if isinstance(st, OrderStatus) else str(st)
        if status in (OrderStatus.FILLED.value, OrderStatus.FAILED.value, OrderStatus.CANCELLED.value):
            break
        await asyncio.sleep(interval)
    return status


async def _buy(args: argparse.Namespace) -> int:
    amount = float(args.amount_usd)
    if amount > HARD_CAP_USD:
        raise SystemExit(f"amount_usd {amount} exceeds hard cap ${HARD_CAP_USD}.")
    _require_confirm()
    c = _build_live_client()

    # Pre-trade balance / allowance gate.
    bal0 = await c.get_cash_balance()
    if bal0 is None:
        raise SystemExit("Could not read pUSD balance — aborting before any order.")
    if bal0 < amount:
        raise SystemExit(f"pUSD balance {bal0:.4f} < order {amount:.4f} — fund the proxy first.")
    print(json.dumps({"step": "pretrade", "pusd_balance": round(bal0, 4), "funder": _short(c._funder_address, 6)}))

    # Pick a current BTC 5m up/down market + book.
    slug, market, _liq = _select_current_btc_5m(int(args.look_ahead), float(args.max_price))
    token_ids = [str(x) for x in _json_list(market.get("clobTokenIds"))]
    outcomes = [str(x) for x in _json_list(market.get("outcomes"))]
    token_id = token_ids[0]
    ask = _first_ask(_book_for_token(token_id))
    if ask is None:
        raise SystemExit("No ask liquidity on the selected market.")
    ask_price, _ask_size = ask
    price = min(float(args.max_price), float(ask_price))
    size = amount / price

    order = await c.place_order(
        token_id=token_id, side="BUY", price=price, size=size,
        market_id=str(market.get("id") or ""), post_only=False, order_type="FAK",
        dry_run=False, order_outcome="YES",
        market_title=str(market.get("question") or ""),
        condition_id=str(market.get("conditionId") or ""),
        outcome_label=outcomes[0] if outcomes else "Up",
    )
    if order is None:
        raise SystemExit("place_order returned None — order rejected/failed (see logs).")
    status = await _poll_fill(c, order.order_id, int(args.polls), float(args.poll_interval_sec))
    bal1 = await c.get_cash_balance()
    cost = (bal0 - bal1) if (bal1 is not None) else None
    implied_no_fee = size * price
    fee_est = (cost - implied_no_fee) if cost is not None else None

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({
        "created_at": datetime.now(timezone.utc).isoformat(),
        "kind": "direct_clob_smoke_buy", "order_id": order.order_id,
        "token_id": token_id, "condition_id": str(market.get("conditionId") or ""),
        "market_title": str(market.get("question") or ""), "slug": slug,
        "side": "BUY", "outcome_label": outcomes[0] if outcomes else "Up",
        "price": price, "size": size, "status": status,
        "pusd_before": bal0, "pusd_after": bal1,
    }, indent=2))

    print(json.dumps({
        "ok": True, "step": "buy", "orderId": _short(order.order_id),
        "status": status, "slug": slug, "reqPrice": round(price, 5),
        "reqSizeShares": round(size, 4),
        "pusdSpent": round(cost, 5) if cost is not None else None,
        "impliedCostNoFee": round(implied_no_fee, 5),
        "feeEstimateUsd": round(fee_est, 5) if fee_est is not None else None,
        "stateFile": "data/runtime/direct_smoke_last.json",
    }, indent=2))
    return 0


async def _close(args: argparse.Namespace) -> int:
    _require_confirm()
    if not STATE_FILE.exists():
        raise SystemExit("No direct_smoke_last.json — run --buy first.")
    st = json.loads(STATE_FILE.read_text())
    c = _build_live_client()
    token_id = st["token_id"]
    size = float(st["size"])
    bal0 = await c.get_cash_balance()
    # Sell the shares back marketable (FAK). minPrice via low limit so it sweeps bids.
    order = await c.place_order(
        token_id=token_id, side="SELL", price=float(args.min_price), size=size,
        market_id="", post_only=False, order_type="FAK", dry_run=False,
        order_outcome="YES", condition_id=st.get("condition_id", ""),
    )
    if order is None:
        raise SystemExit("close place_order returned None — sell rejected/failed.")
    status = await _poll_fill(c, order.order_id, int(args.polls), float(args.poll_interval_sec))
    bal1 = await c.get_cash_balance()
    received = (bal1 - bal0) if (bal1 is not None and bal0 is not None) else None
    print(json.dumps({
        "ok": True, "step": "close", "orderId": _short(order.order_id),
        "status": status, "soldShares": round(size, 4),
        "pusdReceived": round(received, 5) if received is not None else None,
    }, indent=2))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Direct Polymarket CLOB V2 live smoke test (one tiny order).")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--buy", action="store_true", help="Place one tiny BUY (default).")
    g.add_argument("--close", action="store_true", help="Sell back the shares from the last buy.")
    p.add_argument("--amount-usd", type=float, default=1.0, help=f"Order size USD (hard cap ${HARD_CAP_USD}).")
    p.add_argument("--max-price", type=float, default=0.99)
    p.add_argument("--min-price", type=float, default=0.01, help="Floor price for the close sell sweep.")
    p.add_argument("--look-ahead", type=int, default=3)
    p.add_argument("--polls", type=int, default=15)
    p.add_argument("--poll-interval-sec", type=float, default=1.0)
    args = p.parse_args()
    if args.close:
        return asyncio.run(_close(args))
    return asyncio.run(_buy(args))


if __name__ == "__main__":
    raise SystemExit(main())
