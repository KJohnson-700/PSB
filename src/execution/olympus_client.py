"""Olympus Trading API adapter.

This adapter intentionally models Olympus as a broker-style execution provider:
build payloads from PSB order intent, submit only when explicitly approved, and
reconcile via the async trade-status endpoint before PSB journals live fills.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import urllib.parse
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

APPROVAL_PHRASE = "APPROVE_OLYMPUS_LIVE_ORDERS"
DEFAULT_BASE_URL = "https://api.olympusx.app"
_LONG_HEX_RE = re.compile(r"0x[a-fA-F0-9]{8,}")
_TRADE_ID_RE = re.compile(r"\btr_[A-Za-z0-9_-]+\b")
_LONG_NUMBER_RE = re.compile(r"\b\d{24,}\b")


@dataclass(frozen=True)
class OlympusTradeResponse:
    trade_id: str
    status: str
    raw: dict[str, Any]


class OlympusClient:
    """Small async wrapper around the Olympus Trading API."""

    def __init__(self, config: dict[str, Any]):
        cfg = (config.get("olympus") or {}) if config else {}
        smoke_cfg = (cfg.get("smoke_test") or {}) if isinstance(cfg, dict) else {}
        self.base_url = (cfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = os.getenv("OLYMPUS_API_KEY") or cfg.get("api_key")
        self.timeout_sec = float(cfg.get("timeout_sec", 20))
        self.live_order_approved = bool(cfg.get("live_order_approved", False))
        self.approval_phrase = str(cfg.get("approval_phrase") or APPROVAL_PHRASE)
        self.smoke_test_enabled = bool(smoke_cfg.get("enabled", False))
        self.smoke_max_order_usd = float(smoke_cfg.get("max_order_usd", 5.0) or 5.0)
        self.smoke_max_orders_per_run = int(smoke_cfg.get("max_orders_per_run", 1) or 1)
        self.smoke_require_market_slug = bool(smoke_cfg.get("require_market_slug", True))
        self.smoke_require_condition_id = bool(smoke_cfg.get("require_condition_id", True))
        # Live-order sizing profile (smoke band). Paper sizing is decided upstream by
        # Kelly and is NOT touched here — only the live BUY notional submitted to Olympus
        # gets proportionally shrunk into the smoke band so it clears the cap while still
        # scaling with conviction (a bigger true-Kelly stake → bigger live order, capped).
        trading_cfg = (config.get("trading") or {}) if config else {}
        self.smoke_scale_live_sizing = bool(smoke_cfg.get("scale_live_sizing", True))
        self.smoke_scale_floor_usd = float(smoke_cfg.get("scale_floor_usd", 1.0) or 1.0)
        # Live smoke-band denominator. Defaults to trading.max_position_size, but can be
        # pinned via smoke_test.true_max_usd so paper Kelly caps can grow (e.g. $500
        # bankroll) WITHOUT rescaling live smoke order sizes. Keeps the live band stable.
        self.smoke_true_max_usd = float(
            smoke_cfg.get("true_max_usd")
            if smoke_cfg.get("true_max_usd") is not None
            else (trading_cfg.get("max_position_size", 0.0) or 0.0)
        )
        # Defense-in-depth: the smoke cap is a LIVE-only broker safety. CLOBClient
        # short-circuits paper (dry_run) before reaching Olympus, so this guard
        # should never fire in paper — but we keep it explicit so any future path
        # that routes paper through submit_trade fails loud instead of silently
        # capping a paper fill. Defaults to True (paper) to match src/main.py.
        self._is_dry_run = bool(trading_cfg.get("dry_run", True))
        self._submitted_orders_this_run = 0

    def _smoke_scaled_buy_notional(self, notional: float) -> float:
        """Shrink a live BUY's dollar notional into the smoke band, preserving
        relative conviction.

        Maps the true-Kelly notional range [0, max_position_size] onto
        [0, smoke_max_order_usd] by a flat factor, then floors at scale_floor_usd
        and hard-clamps at the cap. Returns the notional unchanged when scaling is
        off or inputs are degenerate. No-op outside smoke mode (so full live runs
        submit true Kelly).
        """
        if not (self.smoke_test_enabled and self.smoke_scale_live_sizing):
            return notional
        if notional <= 0 or self.smoke_true_max_usd <= 0:
            return notional
        factor = self.smoke_max_order_usd / self.smoke_true_max_usd
        scaled = notional * factor
        scaled = max(scaled, min(self.smoke_scale_floor_usd, self.smoke_max_order_usd))
        scaled = min(scaled, self.smoke_max_order_usd)
        return scaled

    @staticmethod
    def enabled(config: dict[str, Any]) -> bool:
        trading = (config.get("trading") or {}) if config else {}
        return str(trading.get("execution_provider") or "clob").lower() == "olympus"

    def configured(self) -> bool:
        return bool(self.api_key)

    def set_api_key(self, api_key: Optional[str], base_url: Optional[str] = None) -> None:
        if api_key:
            self.api_key = api_key
        if base_url:
            self.base_url = base_url.rstrip("/")

    def approved_for_live_orders(self) -> bool:
        env_approval = os.getenv("OLYMPUS_LIVE_ORDER_APPROVAL", "")
        return self.live_order_approved or env_approval == self.approval_phrase

    def _enforce_smoke_limits(self, payload: dict[str, Any]) -> None:
        if not self.smoke_test_enabled:
            return
        # Must come AFTER the enabled-check: a paper call with smoke disabled is a
        # no-op, not a routing bug. If we reach here in dry_run, paper was routed
        # through live submission — fail loud rather than cap a paper fill.
        if self._is_dry_run:
            raise RuntimeError(
                "Olympus smoke-test guard called in dry_run (paper) mode — this is "
                "a routing bug. CLOBClient.place_order must not call submit_trade "
                "in paper mode."
            )
        if self._submitted_orders_this_run >= self.smoke_max_orders_per_run:
            raise RuntimeError(
                "Olympus smoke-test order limit reached: "
                f"{self._submitted_orders_this_run}/{self.smoke_max_orders_per_run}"
            )
        if self.smoke_require_condition_id and not payload.get("conditionId"):
            raise RuntimeError("Olympus smoke-test blocked: conditionId is required.")
        if self.smoke_require_market_slug and not payload.get("marketSlug"):
            raise RuntimeError("Olympus smoke-test blocked: marketSlug is required.")

        side = str(payload.get("side") or "").upper()
        if side == "BUY":
            notional = float(payload.get("amountUsd") or 0.0)
            if payload.get("maxPrice") is None:
                raise RuntimeError("Olympus smoke-test blocked: BUY requires maxPrice.")
        elif side == "SELL":
            spec = payload.get("sellSpec") or {}
            shares = float(spec.get("sharesNormalized") or 0.0)
            price = float(payload.get("minPrice") or 0.0)
            notional = shares * price
            if payload.get("minPrice") is None:
                raise RuntimeError("Olympus smoke-test blocked: SELL requires minPrice.")
        else:
            raise RuntimeError(f"Olympus smoke-test blocked: unsupported side {side!r}.")

        if notional <= 0:
            raise RuntimeError("Olympus smoke-test blocked: non-positive order notional.")
        if notional > self.smoke_max_order_usd + 1e-9:
            raise RuntimeError(
                "Olympus smoke-test blocked: order notional "
                f"${notional:.2f} exceeds cap ${self.smoke_max_order_usd:.2f}."
            )

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise RuntimeError("OLYMPUS_API_KEY is required for Olympus API calls.")
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def redact_diagnostic_text(value: Any, *, max_len: int = 220) -> str:
        """Redact identifiers from bounded broker diagnostic text."""
        text = str(value or "")
        text = _TRADE_ID_RE.sub("tr_<redacted>", text)
        text = _LONG_HEX_RE.sub("0x<redacted>", text)
        text = _LONG_NUMBER_RE.sub("<redacted_id>", text)
        text = text.replace("\n", " ").replace("\r", " ")
        return text[:max_len]

    @classmethod
    def safe_error_body(cls, raw: str) -> str:
        """Return a compact, redacted HTTP error body for logs/exceptions."""
        raw = raw[:1000]
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return cls.redact_diagnostic_text(raw)
        if not isinstance(parsed, dict):
            return cls.redact_diagnostic_text(parsed)
        whitelisted: dict[str, str] = {}
        for key in ("status", "code", "errorCode", "error", "message", "reason", "detail"):
            value = parsed.get(key)
            if value is None:
                continue
            if isinstance(value, dict):
                value = value.get("code") or value.get("message") or value.get("reason")
            whitelisted[key] = cls.redact_diagnostic_text(value)
        return json.dumps(whitelisted or {"error": "redacted"}, sort_keys=True)

    def _request_json(
        self,
        method: str,
        path: str,
        payload: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        parsed = urllib.parse.urlparse(self.base_url)
        if parsed.scheme != "https":
            raise RuntimeError("Olympus base_url must use https.")
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers=self._headers(),
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_sec) as response:  # nosec B310
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            error_body = self.safe_error_body(exc.read().decode("utf-8", "replace"))
            raise RuntimeError(f"Olympus HTTP {exc.code}: {error_body}") from exc
        return json.loads(raw)

    async def get_portfolio(self) -> dict[str, Any]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: self._request_json("GET", "/v1/portfolio"))

    @staticmethod
    def cash_balance_from_portfolio(portfolio: dict[str, Any]) -> Optional[float]:
        for key in ("totalCashBalanceUsd", "pusdBalance"):
            raw = portfolio.get(key)
            if raw is not None:
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    continue
        return None

    @staticmethod
    def equity_from_portfolio(portfolio: dict[str, Any]) -> Optional[float]:
        """Total account value (cash + open-position value), matching the Olympus
        dashboard EQUITY figure. Falls back to cash if equity isn't reported."""
        raw = portfolio.get("equityUsd")
        if raw is not None:
            try:
                return float(raw)
            except (TypeError, ValueError):
                pass
        return OlympusClient.cash_balance_from_portfolio(portfolio)

    @staticmethod
    def open_condition_ids_from_portfolio(portfolio: dict[str, Any]) -> set:
        """Set of conditionIds for positions currently OPEN on Olympus. Used to
        reconcile the bot's journal against reality (drop phantoms, keep live ones)."""
        out: set = set()
        for pos in (portfolio.get("positions") or []):
            cid = pos.get("conditionId") or pos.get("condition_id")
            if cid:
                out.add(str(cid).lower())
        return out

    @staticmethod
    def build_trade_payload(
        *,
        token_id: str,
        side: str,
        price: float,
        size: float,
        market_id: Optional[str] = None,
        market_title: Optional[str] = None,
        market_slug: Optional[str] = None,
        condition_id: Optional[str] = None,
        outcome_label: Optional[str] = None,
    ) -> dict[str, Any]:
        side_u = str(side or "").upper()
        if side_u not in {"BUY", "SELL"}:
            raise ValueError(f"Olympus side must be BUY or SELL, got {side!r}")
        if not token_id:
            raise ValueError("Olympus tokenId is required.")
        if not condition_id:
            raise ValueError("Olympus conditionId is required.")
        if not market_title:
            raise ValueError("Olympus marketTitle is required.")
        price_f = max(0.0, min(1.0, float(price)))
        size_f = max(0.0, float(size))
        payload: dict[str, Any] = {
            "side": side_u,
            "tokenId": str(token_id),
            "conditionId": str(condition_id),
            "marketTitle": str(market_title),
            "marketId": str(market_id) if market_id else None,
            "marketSlug": str(market_slug) if market_slug else None,
        }
        if side_u == "BUY":
            if not outcome_label:
                raise ValueError("Olympus BUY requires outcomeLabel.")
            payload.update(
                {
                    "amountUsd": round(price_f * size_f, 6),
                    "maxPrice": round(price_f, 6),
                    "outcomeLabel": outcome_label,
                }
            )
        else:
            payload.update(
                {
                    "sellSpec": {
                        "type": "shares",
                        "sharesNormalized": round(size_f, 6),
                    },
                    "minPrice": round(price_f, 6),
                }
            )
        return payload

    async def submit_trade(self, payload: dict[str, Any]) -> OlympusTradeResponse:
        if not self.approved_for_live_orders():
            raise RuntimeError(
                "Olympus live order blocked: set olympus.live_order_approved=true "
                f"or OLYMPUS_LIVE_ORDER_APPROVAL={self.approval_phrase}."
            )
        # Smoke-band sizing: shrink only the LIVE BUY notional into the configured
        # band (paper sizing upstream is untouched). No-op when scaling/smoke off.
        if str(payload.get("side") or "").upper() == "BUY":
            raw_notional = float(payload.get("amountUsd") or 0.0)
            scaled = self._smoke_scaled_buy_notional(raw_notional)
            if scaled != raw_notional:
                payload["amountUsd"] = round(scaled, 6)
        self._enforce_smoke_limits(payload)
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(
            None, lambda: self._request_json("POST", "/v1/trade", payload)
        )
        trade_id = str(data.get("tradeId") or "")
        if not trade_id:
            raise RuntimeError(f"Olympus trade response missing tradeId: {data}")
        self._submitted_orders_this_run += 1
        return OlympusTradeResponse(
            trade_id=trade_id,
            status=str(data.get("status") or ""),
            raw=data,
        )

    async def get_trade_status(self, trade_id: str) -> dict[str, Any]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: self._request_json("GET", f"/v1/trades/{trade_id}")
        )
