"""
Resolution Tracker — Checks Polymarket for market resolutions and settles paper positions.

This is CRITICAL for reliable paper trading. Without it, positions stay open forever
and PnL is meaningless. This module:

1. Periodically polls Polymarket Gamma API for market resolution status
2. When a market resolves, calculates real PnL based on actual outcome
3. Logs the exit in the trade journal with the real resolution
4. Updates the exposure manager with win/loss for kill switch tracking

No fabricated results — only real market outcomes from Polymarket.
"""
import logging
import time
from datetime import datetime
from typing import Dict, List, Optional, Any

import requests

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"


class ResolutionTracker:
    """Tracks market resolutions and settles paper trade positions."""

    def __init__(self, check_interval_seconds: int = 120):
        self.check_interval = check_interval_seconds
        self._last_check = 0.0
        self._resolution_cache: Dict[str, Dict[str, Any]] = {}
        # Guard: once a trade_id is settled this session, never settle it again.
        # Prevents double-settlement when positions.json is stale on restart.
        self._settled_trade_ids: set = set()

    def check_and_settle(
        self,
        journal,
        risk_manager,
        exposure_manager=None,
        bankroll: float = 0.0,
        ctf_redeemer=None,
    ) -> List[Dict]:
        """Check all open positions for resolution and settle any that have resolved.

        Returns list of settled positions with their outcomes.
        """
        now = time.time()
        if now - self._last_check < self.check_interval:
            return []
        self._last_check = now

        open_positions = journal.get_open_positions()
        if not open_positions:
            return []

        # Collect unique market IDs
        market_ids = list(set(p.get("market_id", "") for p in open_positions if p.get("market_id")))
        if not market_ids:
            return []

        logger.info(f"Resolution check: {len(market_ids)} markets with open positions")

        # Fetch resolution status for all markets
        resolved_markets = self._fetch_resolutions(market_ids)

        if not resolved_markets:
            return []

        settled = []
        for pos in open_positions:
            mid = pos.get("market_id", "")
            if mid not in resolved_markets:
                continue

            # Skip positions we already settled this session — guards against
            # stale positions.json causing double-settlement on restart.
            trade_id_check = pos.get("trade_id", "")
            if trade_id_check in self._settled_trade_ids:
                logger.debug(f"Skipping already-settled trade: {trade_id_check}")
                continue

            resolution = resolved_markets[mid]
            outcome_won = resolution.get("outcome_won")  # "YES" or "NO" or None

            if outcome_won is None:
                continue  # Market closed but not yet resolved

            # Calculate settlement PnL
            trade_id = pos.get("trade_id", "")
            action = pos.get("action", "")
            side = pos.get("side", "")
            size = pos.get("size", 0)
            entry_price = pos.get("entry_price", 0)
            strategy = pos.get("strategy", "")
            outcome_bet = pos.get("outcome", "")  # What we bet on: YES or NO

            # Settlement: if we bought YES and YES won → payout = size / entry_price * 1.0
            # Polymarket settlement: YES resolves to $1, NO resolves to $0
            if action == "BUY_YES":
                if outcome_won == "YES":
                    # We bought YES at entry_price, it resolved to $1
                    exit_price = 1.0
                else:
                    # YES resolved to $0
                    exit_price = 0.0
            elif action == "SELL_YES":
                if outcome_won == "YES":
                    # We sold YES, it went to $1 — we lose
                    exit_price = 1.0
                else:
                    # We sold YES, it went to $0 — we win
                    exit_price = 0.0
            elif action == "BUY_NO":
                if outcome_won == "NO":
                    exit_price = 1.0
                else:
                    exit_price = 0.0
            else:
                continue

            # PnL calculation
            if side == "BUY":
                pnl = (exit_price - entry_price) * size
            else:
                pnl = (entry_price - exit_price) * size

            reason = f"RESOLVED:{outcome_won} (real)"
            logger.info(
                f"SETTLEMENT: {strategy} {action} '{pos.get('market_question', '')[:50]}' "
                f"-> {outcome_won} | entry={entry_price:.3f} exit={exit_price:.3f} "
                f"PnL=${pnl:+.2f}"
            )

            # 2026-08-19 DATA-LOOP FIX: this path settled positions with NO exit telemetry.
            # Measured: 0 of 13 resolution exits carried mae_pct while 696 of 696 exits on
            # every other path did — so any position that reached settlement was invisible to
            # stop-tuning and to the exit calibration entirely. Derive the same fields the
            # normal exit path writes, from the peak/trough the journal accumulates over the
            # hold. Fail-open: telemetry must never block a settlement.
            _tel = None
            try:
                _entry = float(entry_price or 0.0)
                if _entry > 0:
                    _peak = float(pos.get("peak_token_price", _entry) or _entry)
                    _trough = float(pos.get("trough_token_price", _entry) or _entry)
                    _tel = {
                        "mae_pct": round((_trough - _entry) / _entry, 4),
                        "mfe_pct": round((_peak - _entry) / _entry, 4),
                        "pnl_pct_at_exit": round((exit_price - _entry) / _entry, 4),
                        "effective_stop_loss_pct": None,
                        "exit_path": "resolution_tracker",
                    }
            except (TypeError, ValueError, ZeroDivisionError):
                _tel = None

            # Log exit in journal
            journal.log_exit(
                trade_id=trade_id,
                exit_price=exit_price,
                bankroll=bankroll,
                reason=reason,
                exit_telemetry=_tel,
            )

            # Mark settled so we never process this trade_id again this session
            self._settled_trade_ids.add(trade_id)

            # Remove from risk manager
            if risk_manager and trade_id in risk_manager.active_positions:
                risk_manager.remove_position(trade_id)

            # Update exposure manager for kill switch tracking
            if exposure_manager:
                exposure_manager.record_trade(pnl=pnl, strategy=strategy, market_id=mid)

            # ── CTF redemption (live mode only) ──────────────────────────
            # Winning positions need on-chain redemption to convert conditional
            # tokens back to USDC. Dry-run mode logs only; no chain interaction.
            if ctf_redeemer and pnl > 0:
                condition_id = resolution.get("condition_id")
                redeem_side = (
                    "YES" if action == "BUY_YES" else
                    "NO"  if action in ("BUY_NO", "SELL_YES") else
                    None
                )
                if redeem_side:
                    ctf_redeemer.redeem(
                        condition_id=condition_id,
                        outcome_won=redeem_side,
                        market_question=pos.get("market_question", ""),
                    )
                else:
                    logger.warning(
                        "[CTFRedeemer] Cannot determine redeem_side for action=%r "
                        "on '%s' — winnings unclaimed",
                        action,
                        pos.get("market_question", "")[:50],
                    )

            settled.append({
                "trade_id": trade_id,
                "market_id": mid,
                "market_question": pos.get("market_question", ""),
                "strategy": strategy,
                "action": action,
                "outcome_won": outcome_won,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "pnl": pnl,
                "window_size": str(pos.get("window_size") or ""),
                "settled_at": datetime.now().isoformat(),
            })

        if settled:
            logger.info(f"Resolution tracker: Settled {len(settled)} positions")

        return settled

    def _fetch_resolutions(self, market_ids: List[str]) -> Dict[str, Dict]:
        """Fetch resolution status from Polymarket Gamma API.

        Returns dict of market_id → {outcome_won: "YES"/"NO"/None, resolved: bool}
        """
        resolved = {}

        for mid in market_ids:
            # Check cache first
            if mid in self._resolution_cache:
                resolved[mid] = self._resolution_cache[mid]
                continue

            try:
                resp = requests.get(
                    f"{GAMMA_API}/markets/{mid}",
                    timeout=10,
                )
                if resp.status_code != 200:
                    continue

                data = resp.json()

                # Check if market is resolved
                is_closed = data.get("closed", False)
                resolution = data.get("resolution", None)  # Could be "YES", "NO", or null

                if not is_closed:
                    continue

                if resolution:
                    outcome_won = resolution.upper() if isinstance(resolution, str) else None
                else:
                    # Some markets use outcomes array
                    outcomes = data.get("outcomes", [])
                    outcome_prices = data.get("outcomePrices", "")
                    if outcome_prices:
                        try:
                            # outcomePrices is a JSON string like '["1","0"]' or '["0","1"]'
                            import json
                            prices = json.loads(outcome_prices) if isinstance(outcome_prices, str) else outcome_prices
                            if len(prices) >= 2:
                                yes_price = float(prices[0])
                                if yes_price >= 0.99:
                                    outcome_won = "YES"
                                elif yes_price <= 0.01:
                                    outcome_won = "NO"
                                else:
                                    continue  # Not fully resolved
                            else:
                                continue
                        except (ValueError, json.JSONDecodeError):
                            continue
                    else:
                        continue

                result = {
                    "outcome_won": outcome_won,
                    "resolved": True,
                    "resolved_at": data.get("resolvedAt", datetime.now().isoformat()),
                    # conditionId is needed by CTFRedeemer for on-chain redemption
                    "condition_id": data.get("conditionId"),
                }
                resolved[mid] = result
                # Cache resolved markets (they won't change)
                self._resolution_cache[mid] = result
                logger.info(f"Market {mid} resolved: {outcome_won}")

            except requests.RequestException as e:
                logger.debug(f"Could not fetch resolution for {mid}: {e}")
            except Exception as e:
                logger.warning(f"Resolution fetch error for {mid}: {e}")

        return resolved

    def check_price_updates(
        self,
        journal,
        bankroll: float = 0.0,
        return_prices: bool = False,
        clob_prices: Optional[dict[str, float]] = None,
    ) -> int | tuple[int, dict[str, float]]:
        """Update current (unrealized) marks for all open positions.

        ``clob_prices`` maps market_id -> CLOB ``/midpoint`` YES price. When a
        held market is present there, the mark is taken from CLOB — the SAME
        ruler used for entry (scanner ``/midpoint``) and for the exit/stop mark.
        This keeps the dashboard's mark-to-market consistent with the price the
        bot actually enters and exits on, in both live and paper (CLOB is the
        live-correct source and the closest paper can mirror it). Gamma
        ``outcomePrices`` is used only as a fallback for markets CLOB couldn't
        price this cycle. Resolution *detection* (``check_resolutions``) is
        unaffected and remains Gamma-authoritative.

        Returns number of positions updated.
        """
        open_positions = journal.get_open_positions()
        if not open_positions:
            return (0, {}) if return_prices else 0

        clob_prices = clob_prices or {}
        updated = 0
        market_prices: dict[str, float] = {}
        for pos in open_positions:
            mid = pos.get("market_id", "")
            trade_id = pos.get("trade_id", "")
            if not mid or not trade_id:
                continue

            # Prefer the CLOB /midpoint mark (one ruler with entry + exit).
            clob_mark = clob_prices.get(mid)
            if clob_mark is not None:
                journal.log_price_update(trade_id, float(clob_mark), bankroll)
                market_prices[mid] = float(clob_mark)
                updated += 1
                continue

            try:
                resp = requests.get(f"{GAMMA_API}/markets/{mid}", timeout=10)
                if resp.status_code != 200:
                    continue

                data = resp.json()
                outcome_prices = data.get("outcomePrices", "")
                if outcome_prices:
                    import json
                    outcome_price_list = (
                        json.loads(outcome_prices)
                        if isinstance(outcome_prices, str)
                        else outcome_prices
                    )
                    if len(outcome_price_list) >= 1:
                        current_yes_price = float(outcome_price_list[0])
                        journal.log_price_update(trade_id, current_yes_price, bankroll)
                        market_prices[mid] = current_yes_price
                        updated += 1
            except Exception as e:
                logger.debug(f"Price update failed for {trade_id}: {e}")

        return (updated, market_prices) if return_prices else updated
