"""
Execution Module
Order execution and risk management
"""

import asyncio
import logging
import time
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from src.execution.fill_sim import simulate_book_fill
from src.execution.olympus_client import OlympusClient
try:
    from importlib.metadata import PackageNotFoundError, version as package_version
except ImportError:  # pragma: no cover - Python 3.8 fallback if ever needed
    PackageNotFoundError = Exception
    package_version = None
try:
    # py-clob-client-v2: Polymarket's unified SDK. Production cut to CLOB V2 on
    # 2026-04-28; the legacy V1 `py_clob_client` package no longer settles orders.
    from py_clob_client_v2 import (
        ClobClient as PyClobClient,
        ApiCreds,
        AssetType,
        BalanceAllowanceParams,
        OrderArgs,
        MarketOrderArgs,
        OrderType,
        OrderPayload,
        Side,
        PartialCreateOrderOptions,
        SignatureTypeV2,
    )
except ImportError:
    PyClobClient = None
    ApiCreds = None
    AssetType = None
    BalanceAllowanceParams = None
    OrderArgs = None
    MarketOrderArgs = None
    OrderType = None
    OrderPayload = None
    Side = None
    PartialCreateOrderOptions = None
    SignatureTypeV2 = None

logger = logging.getLogger(__name__)

LEGACY_CLOB_CLIENT_LIVE_BLOCK_REASON = (
    "Live CLOB execution is blocked: this environment imports archived "
    "py-clob-client V1. Polymarket docs now direct Python trading integrations "
    "to py-clob-client-v2 / the unified SDK. Paper/read-only paths may run, but "
    "real order placement must wait for the SDK migration."
)


class OrderStatus(Enum):
    PENDING = "pending"
    FILLED = "filled"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class Order:
    """Represents a trade order"""

    order_id: str
    market_id: str
    token_id: str
    side: str
    outcome: str
    price: float
    size: float
    filled_size: float = 0.0
    status: OrderStatus = OrderStatus.PENDING
    created_at: datetime = None
    updated_at: datetime = None
    execution: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now()
        if self.updated_at is None:
            self.updated_at = datetime.now()


@dataclass
class Position:
    """Represents an open position"""

    position_id: str
    market_id: str
    market_question: str
    outcome: str
    size: float
    entry_price: float
    current_price: float
    pnl: float
    opened_at: datetime
    end_date: Optional[datetime]
    strategy: str = "unknown"
    # YES = entry_price quotes YES token (BUY_YES, SELL_YES). NO = BUY_NO (NO token).
    entry_leg: str = "YES"
    window_size: str = ""
    # Highest token mark seen while the position was open; used for peak-aware
    # profit protection on up/down exits.
    peak_token_price: float = 0.0
    # CLOB outcome token ids (from market clobTokenIds); used for book/mid by token and dashboard.
    token_id_yes: str = ""
    token_id_no: str = ""
    edge: float = 0.0
    confidence: float = 0.0
    entry_signal: Dict[str, Any] = field(default_factory=dict)
    condition_id: str = ""
    market_slug: str = ""


class CLOBClient:
    """CLOB Client Wrapper for Polymarket"""

    def __init__(self, config: Dict[str, Any]):
        # Root config: trading.* lives at top level, not under polymarket.
        self._root_config = config
        _trading_config = config.get("trading", {}) or {}
        _slippage_guard = _trading_config.get("slippage_guard", {}) or {}
        self._paper_entry_fresh_fill = bool(
            _trading_config.get("paper_entry_fresh_fill", False)
        )
        self._paper_entry_fresh_fill_slip_tol = float(
            _trading_config.get(
                "paper_entry_fresh_fill_slippage_tol",
                _slippage_guard.get("max_slippage_cents", 0.02),
            )
            or 0.0
        )
        self.config = config.get("polymarket", {})
        self.api_endpoint = self.config.get(
            "api_endpoint", "https://clob.polymarket.com"
        )
        self.chain_id = self.config.get("chain_id", 137)
        self.private_key = None
        self.creds = None
        self.client = None
        self.pending_orders: Dict[str, Order] = {}
        self.order_history: List[Order] = []
        self._max_order_history = 1000
        # Level-0 client for public `get_order_book` when no signer/trading keys set.
        self._readonly_py_client: Optional[Any] = None
        self._fee_rate_cache: Dict[str, float] = {}
        self._tick_size_cache: Dict[str, str] = {}
        # 2026-07-19 exit-mark /midpoint de-dup cache. The fast-exit loop (3s) marks
        # each held token ~4-5x per tick across the hold/stop/TP + price-update sites,
        # each = one httpx GET /midpoint via py_clob_client -> the dominant 429 source.
        # Short TTL (< exit cadence) collapses the intra-tick duplicates into ONE GET;
        # every new tick still marks off a fresh fetch. Only successful mids are cached.
        self._midpoint_cache: Dict[str, tuple] = {}
        try:
            self._midpoint_cache_ttl = float(config.get('trading', {}).get('midpoint_cache_ttl_sec', 1.5) or 0.0)
        except (TypeError, ValueError):
            self._midpoint_cache_ttl = 1.5
        # Derived L2 credentials expire ~7 days after creation; Polymarket does not
        # rotate them and auth calls fail silently once stale. Track when they were
        # set and refuse to trade past a configurable age so we fail loud, not silent.
        self._creds_set_at: Optional[datetime] = None
        self._creds_max_age = timedelta(
            hours=float(self.config.get("creds_max_age_hours", 144))  # 6 days
        )
        # When creds go stale, re-derive L2 from the L1 signer the py-clob-client
        # already holds (idempotent, no raw-key re-handling) instead of refusing.
        self._auto_rederive_credentials = bool(
            self.config.get("auto_rederive_credentials", True)
        )
        # V2 proxy/Safe execution. Polymarket website accounts hold funds in a
        # proxy / Gnosis-Safe wallet, NOT the signing EOA, and CLOB V2 rejects
        # raw-EOA orders ("maker address not allowed", py-clob-client-v2 #51). So
        # a live order must carry the account's signature type + funder (proxy)
        # address. signature_type: 1=POLY_PROXY (email/Magic signup),
        # 2=POLY_GNOSIS_SAFE (browser-wallet signup), 0=EOA (rejected by prod),
        # 3=POLY_1271 (deposit wallet). Accepts an int or the enum name string.
        self._signature_type = self._resolve_signature_type(
            self.config.get("signature_type")
        )
        self._funder_address = (self.config.get("funder_address") or "").strip() or None
        self._execution_provider = str(
            (self._root_config.get("trading") or {}).get("execution_provider") or "clob"
        ).lower()
        self.olympus_client = OlympusClient(self._root_config)
        olympus_cfg = (self._root_config.get("olympus") or {}) if self._root_config else {}
        olympus_smoke_cfg = (
            (olympus_cfg.get("smoke_test") or {}) if isinstance(olympus_cfg, dict) else {}
        )
        self._olympus_await_fill_on_submit = bool(
            olympus_cfg.get(
                "await_fill_on_submit",
                bool(olympus_smoke_cfg.get("enabled", False)),
            )
        )
        self._olympus_fill_poll_attempts = int(
            olympus_cfg.get("fill_poll_attempts", 12) or 12
        )
        self._olympus_fill_poll_interval_sec = float(
            olympus_cfg.get("fill_poll_interval_sec", 1.0) or 1.0
        )

    def using_olympus(self) -> bool:
        return self._execution_provider == "olympus"

    def olympus_configured(self) -> bool:
        return self.olympus_client.configured()

    def set_olympus_credentials(
        self, api_key: Optional[str], base_url: Optional[str] = None
    ) -> None:
        self.olympus_client.set_api_key(api_key, base_url)

    @staticmethod
    def _olympus_execution_report(
        payload: Optional[Dict[str, Any]],
        *,
        requested_price: Optional[float] = None,
        requested_size: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Extract whitelisted fill-quality fields from an Olympus status payload.

        The raw payload may include account identifiers or other broker metadata,
        so only known execution fields are propagated into logs/journals.
        """
        if not isinstance(payload, dict):
            return {}

        def _first_float(*keys: str) -> Optional[float]:
            for key in keys:
                raw = payload.get(key)
                if raw is None:
                    continue
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    continue
            return None

        status = str(payload.get("status") or "").upper() or None
        report: Dict[str, Any] = {
            "execution_provider": "olympus",
            "olympus_status": status,
        }
        filled_price = _first_float(
            "filledPrice",
            "averageFillPrice",
            "avgFillPrice",
            "avgPrice",
            "price",
        )
        filled_size = _first_float(
            "filledSharesNormalized",
            "filledSize",
            "sharesNormalized",
            "size",
        )
        spent_usd = _first_float("spentUsd", "filledAmountUsd", "amountUsd")
        requested_amount_usd = _first_float("requestedAmountUsd", "amountUsd")
        fee_usdc = _first_float("feeUsd", "feesUsd", "totalFeeUsd", "fee")
        for key, value in (
            ("olympus_filled_price", filled_price),
            ("olympus_filled_size", filled_size),
            ("olympus_spent_usd", spent_usd),
            ("olympus_requested_amount_usd", requested_amount_usd),
            ("olympus_fee_usdc", fee_usdc),
        ):
            if value is not None:
                report[key] = round(value, 8)
        if requested_price is not None:
            try:
                report["olympus_requested_price"] = round(float(requested_price), 8)
            except (TypeError, ValueError):
                pass
        if requested_size is not None:
            try:
                report["olympus_requested_size"] = round(float(requested_size), 8)
            except (TypeError, ValueError):
                pass
        if filled_price is not None and requested_price not in (None, 0):
            try:
                report["olympus_price_delta"] = round(
                    float(filled_price) - float(requested_price), 8
                )
            except (TypeError, ValueError):
                pass
        failure_report = CLOBClient._olympus_failure_report(payload)
        if failure_report:
            report.update(failure_report)
        return {k: v for k, v in report.items() if v is not None}

    @staticmethod
    def _olympus_failure_report(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Extract bounded, redacted failure diagnostics from Olympus status payloads."""
        if not isinstance(payload, dict):
            return {}

        def _find_value(*keys: str) -> Optional[Any]:
            for key in keys:
                if key in payload and payload.get(key) not in (None, ""):
                    return payload.get(key)
            nested = payload.get("error")
            if isinstance(nested, dict):
                for key in keys:
                    if key in nested and nested.get(key) not in (None, ""):
                        return nested.get(key)
            return None

        code = _find_value(
            "errorCode",
            "failureCode",
            "rejectCode",
            "reasonCode",
            "code",
        )
        reason = _find_value(
            "failureReason",
            "rejectReason",
            "statusReason",
            "reason",
            "message",
            "detail",
        )
        report: Dict[str, Any] = {}
        if code is not None:
            report["olympus_failure_code"] = OlympusClient.redact_diagnostic_text(
                code,
                max_len=80,
            )
        if reason is not None:
            report["olympus_failure_reason"] = OlympusClient.redact_diagnostic_text(
                reason,
                max_len=180,
            )
        return report

    @staticmethod
    def _format_olympus_failure_for_log(order: Order) -> str:
        status = str(order.execution.get("olympus_status") or order.status.value).upper()
        code = order.execution.get("olympus_failure_code") or "unknown"
        reason = order.execution.get("olympus_failure_reason") or "unknown"
        return f"status={status} code={code} reason={reason}"

    def _update_olympus_order_execution(
        self,
        order_id: str,
        status_payload: Dict[str, Any],
    ) -> Optional[Order]:
        order = self.pending_orders.get(order_id)
        if order is None:
            return None
        order.execution.update(
            self._olympus_execution_report(
                status_payload,
                requested_price=order.price,
                requested_size=order.size,
            )
        )
        if "olympus_status_history" not in order.execution:
            order.execution["olympus_status_history"] = []
        status = order.execution.get("olympus_status")
        if status and status not in order.execution["olympus_status_history"]:
            order.execution["olympus_status_history"].append(status)
        filled_price = order.execution.get("olympus_filled_price")
        filled_size = order.execution.get("olympus_filled_size")
        if isinstance(filled_price, (int, float)):
            order.price = float(filled_price)
        if isinstance(filled_size, (int, float)) and filled_size > 0:
            order.filled_size = float(filled_size)
        order.updated_at = datetime.now()
        return order

    async def _await_olympus_terminal_order(self, order: Order) -> Order:
        for _ in range(max(0, self._olympus_fill_poll_attempts)):
            await asyncio.sleep(max(0.0, self._olympus_fill_poll_interval_sec))
            try:
                payload = await self.olympus_client.get_trade_status(order.order_id)
            except Exception as exc:
                logger.error(
                    "Error polling Olympus trade status: %s",
                    exc,
                )
                break
            self._update_olympus_order_execution(order.order_id, payload)
            status = str(payload.get("status") or "").upper()
            if status == "SUCCEEDED":
                order.status = OrderStatus.FILLED
                return order
            if status == "FAILED":
                order.status = OrderStatus.FAILED
                return order
        return order

    @staticmethod
    def live_execution_supported() -> bool:
        """True only when the V2 CLOB SDK is installed AND its order API imported.

        Polymarket hard-cut production to CLOB V2 on 2026-04-28; the legacy V1
        ``py-clob-client`` package no longer settles orders. This client is ported
        to ``py-clob-client-v2``, so live execution requires that package to be
        present and its order types to have imported cleanly. We check both the
        installed-package metadata and the live import symbols so a partial/broken
        install fails closed rather than placing orders that can't be signed.
        See vault: projects/psb/research/2026-06-13-py-clob-client-v2-migration-spec-kimi.md
        """
        if PyClobClient is None or OrderArgs is None or OrderType is None:
            return False
        if package_version is None:
            return True
        try:
            package_version("py-clob-client-v2")
            return True
        except PackageNotFoundError:
            return False

    @staticmethod
    def _resolve_signature_type(raw: Any) -> Optional[int]:
        """Map a config signature_type (int or enum-name string) to a V2 int.

        Accepts ``1`` / ``"1"`` / ``"POLY_PROXY"`` etc. Returns None when unset so
        the SDK falls back to its EOA default (which prod rejects — we warn at
        client construction).
        """
        if raw is None or raw == "":
            return None
        if isinstance(raw, bool):
            return None
        if isinstance(raw, (int, float)):
            return int(raw)
        name = str(raw).strip()
        if name.isdigit():
            return int(name)
        if SignatureTypeV2 is not None:
            member = getattr(SignatureTypeV2, name.upper(), None)
            if member is not None:
                return int(member)
        logger.warning("Unrecognized polymarket.signature_type=%r; ignoring.", raw)
        return None

    def set_credentials(
        self,
        private_key: str,
        api_key: str = None,
        api_secret: str = None,
        api_passphrase: str = None,
    ):
        if not self.live_execution_supported():
            raise RuntimeError(LEGACY_CLOB_CLIENT_LIVE_BLOCK_REASON)
        if PyClobClient is None or ApiCreds is None:
            raise RuntimeError(
                "py-clob-client-v2 is required for live CLOB execution. "
                "Install project requirements before running with dry_run=false."
            )
        # CLOB V2 rejects raw-EOA orders (#51). Proxy/Safe accounts must supply a
        # signature_type (1/2) and the funder (proxy) address, or live orders will
        # be refused at match time with "maker address not allowed".
        if self._signature_type in (None, 0) or not self._funder_address:
            logger.warning(
                "CLOB V2 live config incomplete: signature_type=%s funder=%s. "
                "Polymarket V2 rejects raw-EOA orders — set polymarket.signature_type "
                "(1=POLY_PROXY email/Magic, 2=POLY_GNOSIS_SAFE browser-wallet) and "
                "polymarket.funder_address to your Polymarket proxy wallet address.",
                self._signature_type,
                self._funder_address,
            )
        creds = ApiCreds(api_key, api_secret, api_passphrase)
        self.client = PyClobClient(
            host=self.api_endpoint,
            chain_id=self.chain_id,
            key=private_key,
            creds=creds,
            signature_type=self._signature_type,
            funder=self._funder_address,
        )
        # Clear plaintext copies — PyClobClient holds its own internal copy
        del private_key
        self.private_key = None
        self._creds_set_at = datetime.now()

    def credentials_age(self) -> Optional[timedelta]:
        """Time since L2 credentials were set, or None if never set."""
        if self._creds_set_at is None:
            return None
        return datetime.now() - self._creds_set_at

    def credentials_expired(self) -> bool:
        """
        True when derived L2 creds are older than the configured max age.

        Polymarket's L2 credentials expire ~7 days after derivation with no
        rotation and no expiry signal — calls just start failing with auth
        errors. We treat creds past `creds_max_age_hours` (default 6 days) as
        expired so the failure is loud and pre-trade, not a silent mid-session
        auth death on day ~8. If creds were never set we cannot judge age, so
        this returns False and the existing `self.client` guards take over.
        """
        age = self.credentials_age()
        if age is None:
            return False
        return age >= self._creds_max_age

    async def _rederive_l2_credentials(self) -> bool:
        """
        Re-derive L2 API creds from the L1 signer the py-clob-client already holds.

        `set_credentials` hands the L1 private key to `PyClobClient(key=...)` and
        then deletes its own plaintext copy. The client keeps an internal signer,
        so we can mint fresh L2 creds via an L1 signature without ever re-handling
        the raw key. `create_or_derive_api_creds` is idempotent (create if absent,
        derive if present) — the same restart-safe bootstrap pattern the D8-X SDK
        documents. Returns True on success and resets the expiry clock.
        """
        if not self.client:
            logger.error("Cannot re-derive credentials: CLOB client not initialized.")
            return False
        # V2 renamed create_or_derive_api_creds -> create_or_derive_api_key.
        # Prefer the V2 name, fall back to the V1 name for forward/back safety.
        derive = getattr(self.client, "create_or_derive_api_key", None) or getattr(
            self.client, "create_or_derive_api_creds", None
        )
        set_creds = getattr(self.client, "set_api_creds", None)
        if not callable(derive) or not callable(set_creds):
            logger.error(
                "Installed CLOB SDK lacks create_or_derive_api_key/set_api_creds "
                "— cannot self-heal expired credentials. Re-derive manually (L1 "
                "sign) before trading."
            )
            return False
        try:
            loop = asyncio.get_event_loop()
            new_creds = await loop.run_in_executor(None, derive)
            await loop.run_in_executor(None, lambda: set_creds(new_creds))
        except Exception as exc:
            logger.error("Failed to re-derive L2 credentials: %s", exc)
            return False
        self._creds_set_at = datetime.now()
        logger.info("Re-derived L2 credentials from L1 signer; expiry clock reset.")
        return True

    async def ensure_fresh_credentials(self, force_rederive: bool = False) -> bool:
        """
        Guarantee usable L2 creds before a live action.

        Returns True when creds are fresh, or when a stale set was successfully
        re-derived. Returns False only when creds are stale and could not be
        refreshed (auto-rederive disabled, no signer, or derive failed) — the
        caller should then refuse to trade rather than fire a silent auth error.

        `force_rederive=True` mints fresh creds unconditionally (idempotent
        bootstrap pattern) regardless of the tracked age — used at startup, where
        `_creds_set_at` only records when we *loaded* the .env creds, not when
        they were actually derived, so they could already be near expiry.
        """
        if force_rederive and self.client:
            if await self._rederive_l2_credentials():
                return True
            logger.warning(
                "Forced credential re-derive failed; falling back to staleness check."
            )
        if not self.credentials_expired():
            return True
        if not self._auto_rederive_credentials:
            logger.warning(
                "L2 credentials are stale and auto-rederive is disabled; "
                "refusing to use them."
            )
            return False
        return await self._rederive_l2_credentials()

    @staticmethod
    def _normalize_usdc_amount(raw: Any) -> Optional[float]:
        """Normalize CLOB integer micro-USDC or decimal-string balances to dollars."""
        if raw is None:
            return None
        try:
            value = Decimal(str(raw))
        except (InvalidOperation, TypeError, ValueError):
            return None
        if value < 0:
            return None
        # CLOB balance-allowance returns collateral in USDC base units on current
        # py-clob-client builds. Small decimal strings are already human USDC.
        if value == value.to_integral_value() and value >= Decimal("100000"):
            value = value / Decimal("1000000")
        return float(value)

    @classmethod
    def _extract_cash_balance(cls, payload: Any) -> Optional[float]:
        if not isinstance(payload, dict):
            return None
        for key in ("balance", "cash", "collateral", "collateral_balance"):
            amount = cls._normalize_usdc_amount(payload.get(key))
            if amount is not None:
                return amount
        return None

    async def get_cash_balance(self) -> Optional[float]:
        """Fetch authenticated Polymarket collateral balance in USDC."""
        if self.using_olympus():
            try:
                portfolio = await self.olympus_client.get_portfolio()
            except Exception as exc:
                logger.error("Error fetching Olympus wallet bankroll: %s", exc)
                return None
            balance = self.olympus_client.cash_balance_from_portfolio(portfolio)
            if balance is None:
                logger.error("Could not parse Olympus wallet bankroll payload: %s", portfolio)
            return balance
        if not self.client:
            logger.error("CLOB client not initialized — cannot fetch live wallet bankroll.")
            return None
        if BalanceAllowanceParams is None or AssetType is None:
            logger.error("py-clob-client balance types unavailable — cannot fetch wallet bankroll.")
            return None
        try:
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            loop = asyncio.get_event_loop()
            payload = await loop.run_in_executor(
                None,
                lambda: self.client.get_balance_allowance(params),
            )
        except Exception as exc:
            logger.error("Error fetching Polymarket wallet bankroll: %s", exc)
            return None
        balance = self._extract_cash_balance(payload)
        if balance is None:
            logger.error("Could not parse Polymarket wallet bankroll payload: %s", payload)
        return balance

    async def get_account_value(self) -> Optional[float]:
        """Total account value for the bankroll (cash + open-position value).

        On Olympus this is the dashboard EQUITY figure, so the bot's bankroll
        matches what the user sees and stays accurate when positions are open.
        Direct CLOB has no single equity endpoint, so compose wallet cash plus
        Data-API open-position mark value (so deployed capital is not counted as
        a loss — P&L only moves on realized win/loss, not on capital deployment).
        """
        if self.using_olympus():
            try:
                portfolio = await self.olympus_client.get_portfolio()
            except Exception as exc:
                logger.error("Error fetching Olympus account equity: %s", exc)
                return None
            equity = self.olympus_client.equity_from_portfolio(portfolio)
            if equity is None:
                logger.error("Could not parse Olympus equity payload: %s", portfolio)
            return equity
        cash = await self.get_cash_balance()
        if cash is None:
            return None
        pos_value = await self.clob_open_position_value()
        if pos_value is None:
            logger.warning(
                "Direct CLOB account value using cash-only fallback; open-position value unavailable."
            )
            return cash
        return cash + pos_value

    async def _clob_positions_data(self) -> Optional[List[Dict[str, Any]]]:
        """Raw direct-CLOB positions from the public Data API.

        Returns None on fetch/shape failure so callers fail SAFE. Read-only.
        """
        if self.using_olympus() or not self._funder_address:
            return None
        url = "https://data-api.polymarket.com/positions"
        params = {"user": self._funder_address, "sizeThreshold": "1"}
        try:
            import httpx

            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # noqa: BLE001 - fail SAFE on any error
            logger.error("Error fetching CLOB positions from Data API: %s", exc)
            return None
        if not isinstance(data, list):
            logger.warning(
                "CLOB positions Data-API: unexpected shape %s",
                type(data).__name__,
            )
            return None
        return data

    async def clob_open_position_value(self) -> Optional[float]:
        """Current USDC mark value of direct-CLOB open positions.

        Sums Data-API ``size * curPrice`` for live shares. Returns None on any
        fetch/parse failure so account-value callers can fall back to cash only.
        """
        data = await self._clob_positions_data()
        if data is None:
            return None
        total = 0.0
        for p in data:
            if not isinstance(p, dict):
                continue
            try:
                size = float(p.get("size"))
                cur_price = float(p.get("curPrice"))
            except (TypeError, ValueError):
                logger.warning(
                    "CLOB position value parse failed for condition=%s asset=%s",
                    p.get("conditionId"),
                    p.get("asset"),
                )
                return None
            if size > 0:
                total += size * cur_price
        return total

    async def olympus_open_condition_ids(self) -> Optional[set]:
        """conditionIds of positions currently OPEN on Olympus, for journal
        reconciliation. Returns None on fetch failure so callers can fail SAFE
        (keep positions) rather than wrongly dropping them."""
        if not self.using_olympus():
            return None
        try:
            portfolio = await self.olympus_client.get_portfolio()
        except Exception as exc:
            logger.error("Error fetching Olympus positions for reconcile: %s", exc)
            return None
        return self.olympus_client.open_condition_ids_from_portfolio(portfolio)

    async def clob_open_condition_ids(self) -> Optional[set]:
        """conditionIds the account currently HOLDS on Polymarket, for journal
        reconciliation on the direct CLOB. Reads the public Data API
        (GET data-api.polymarket.com/positions?user=<funder>) since py-clob-client-v2
        dropped get_positions. Returns the set of lowercased conditionIds with a live
        share balance, or None on ANY fetch/parse failure so callers fail SAFE (keep
        all journal positions rather than wrongly abandon a real one). Read-only."""
        if self.using_olympus() or not self._funder_address:
            return None
        data = await self._clob_positions_data()
        if data is None:
            return None
        cids: set = set()
        for p in data:
            if not isinstance(p, dict):
                continue
            try:
                sz = float(p.get("size") or 0)
            except (TypeError, ValueError):
                sz = 0.0
            cid = str(p.get("conditionId") or "").lower()
            if cid and sz > 0:
                cids.add(cid)
        return cids

    async def place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        market_id: str = None,
        post_only: bool = False,
        dry_run: bool = True,
        order_outcome: Optional[str] = None,
        order_type: str = "GTC",
        market_title: Optional[str] = None,
        market_slug: Optional[str] = None,
        condition_id: Optional[str] = None,
        outcome_label: Optional[str] = None,
    ) -> Optional[Order]:
        _outcome = (
            order_outcome
            if order_outcome in ("YES", "NO")
            else ("YES" if side == "BUY" else "NO")
        )
        if dry_run:
            # Keep paper as close to live as possible: quantize the paper fill price
            # to the market's REAL tick size using the same rounding the live V2
            # path applies, so paper entry prices match what the CLOB would actually
            # fill at (no free fractional-cent edge). Fails soft to the raw price if
            # the tick can't be read. Taker fees are modeled separately downstream.
            tick_size = await self.fetch_tick_size(token_id)
            fill_price = self._quantize_price_for_tick(
                price, tick_size, side=side, order_type=order_type
            )
            logger.info(
                f"[DRY RUN] Would place order: {side} {size} @ {fill_price} "
                f"({order_type}) [requested {price}, tick {tick_size}]"
            )
            order = Order(
                order_id=f"dry_{datetime.now().timestamp()}",
                market_id=market_id or "",
                token_id=token_id,
                side=side,
                outcome=_outcome,
                price=fill_price,
                size=size,
                filled_size=size,
                status=OrderStatus.FILLED,
            )
            self.order_history.append(order)
            if len(self.order_history) > self._max_order_history:
                self.order_history = self.order_history[-self._max_order_history :]
            return order

        if self.using_olympus():
            olympus_outcome_label = (
                outcome_label
                or ("Yes" if _outcome == "YES" else "No" if _outcome == "NO" else None)
            )
            try:
                payload = self.olympus_client.build_trade_payload(
                    token_id=token_id,
                    side=side,
                    price=price,
                    size=size,
                    market_id=market_id,
                    market_title=market_title,
                    market_slug=market_slug,
                    condition_id=condition_id,
                    outcome_label=olympus_outcome_label,
                )
                response = await self.olympus_client.submit_trade(payload)
            except Exception as exc:
                logger.error("Olympus order blocked/failed: %s", exc)
                return None
            initial_execution = {
                "execution_provider": "olympus",
                "olympus_status": str(response.status or "").upper() or None,
                "olympus_requested_price": round(float(price), 8),
                "olympus_requested_size": round(float(size), 8),
            }
            order = Order(
                order_id=response.trade_id,
                market_id=market_id or "",
                token_id=token_id,
                side=side,
                outcome=_outcome,
                price=price,
                size=size,
                status=OrderStatus.PENDING,
                execution={k: v for k, v in initial_execution.items() if v is not None},
            )
            self.pending_orders[order.order_id] = order
            response_raw = getattr(response, "raw", None)
            if isinstance(response_raw, dict):
                self._update_olympus_order_execution(order.order_id, response_raw)
            if self._olympus_await_fill_on_submit:
                order = await self._await_olympus_terminal_order(order)
                if order.status == OrderStatus.FAILED:
                    logger.error(
                        "Olympus trade failed before journaling: %s",
                        self._format_olympus_failure_for_log(order),
                    )
                    return None
            self.order_history.append(order)
            if len(self.order_history) > self._max_order_history:
                self.order_history = self.order_history[-self._max_order_history :]
            logger.info(
                "Olympus trade queued: side=%s provider=olympus",
                side,
            )
            return order

        if not self.client:
            logger.error("CLOB client not initialized. Call set_credentials first.")
            return None
        if not self.live_execution_supported():
            logger.error(LEGACY_CLOB_CLIENT_LIVE_BLOCK_REASON)
            return None
        if not await self.ensure_fresh_credentials():
            age = self.credentials_age()
            age_h = age.total_seconds() / 3600 if age else 0
            logger.error(
                "Refusing live order: L2 credentials are %.1fh old (max %.1fh) "
                "and could not be refreshed. Re-derive credentials (L1 sign) "
                "before trading — Polymarket auth fails silently once creds "
                "expire (~7 days).",
                age_h,
                self._creds_max_age.total_seconds() / 3600,
            )
            return None
        if OrderArgs is None or OrderType is None:
            logger.error("py-clob-client order types unavailable — cannot place live order")
            return None

        # py-clob-client takes the time-in-force on post_order (GTC/FAK/FOK/GTD), NOT
        # on OrderArgs (which has no order_type/post_only fields — passing them there
        # raises). GTC = resting limit (entries); FAK = fill-and-kill / marketable
        # (take resting liquidity now, cancel the remainder) for stop/market exits so
        # the close actually fills instead of resting at a stale bid and re-gapping.
        _ot = getattr(OrderType, str(order_type).upper(), OrderType.GTC)
        tick_size = await self.fetch_tick_size(token_id)
        order_price = self._quantize_price_for_tick(
            price,
            tick_size,
            side=side,
            order_type=order_type,
        )
        if abs(float(order_price) - float(price)) > 1e-12:
            logger.info(
                "Quantized CLOB order price for tick size: token=%s side=%s type=%s "
                "raw=%.6f tick=%s quantized=%.6f",
                token_id[:20],
                side,
                order_type,
                float(price),
                tick_size,
                float(order_price),
            )

        # V2 create_order takes a PartialCreateOrderOptions carrying tick_size /
        # neg_risk. Pass the tick size we already fetched and quantized against so
        # the SDK doesn't re-round; leave neg_risk=None so it resolves per-market.
        create_opts = (
            PartialCreateOrderOptions(tick_size=str(tick_size))
            if (PartialCreateOrderOptions is not None and tick_size)
            else None
        )

        # 2026-07-27 (CLOB entry-outage fix): FAK/FOK are MARKET orders and Polymarket
        # validates them as such — market-buy maker amount <=2 decimals (USDC), taker
        # <=4 decimals (shares). The LIMIT path (create_order/get_order_amounts) builds a
        # BUY as maker=size*price rounded to `amount`(=4) dec / taker=size to `size`(=2)
        # dec — the precision is SWAPPED vs market validation, so every marketable order
        # 400'd ("invalid amounts, market buy orders maker amount max 2 decimals"),
        # blocking ALL entries (152 rejects / ~76 min outage). Route marketable orders
        # through the SDK MARKET path (create_market_order/get_market_order_amounts),
        # which rounds maker->2dec / taker->4dec correctly. MarketOrderArgs.amount =
        # USDC for BUY (size*price budget), shares for SELL (size). GTC (resting limits)
        # keep the create_order path unchanged.
        _marketable = str(order_type).upper() in ("FAK", "FOK")
        _use_market = _marketable and MarketOrderArgs is not None

        try:
            loop = asyncio.get_event_loop()
            if _use_market:
                if str(side).upper() == "BUY":
                    _mkt_amount = float(size) * float(order_price)  # USDC budget
                else:
                    _mkt_amount = float(size)  # shares to sell
                market_args = MarketOrderArgs(
                    token_id=token_id,
                    amount=_mkt_amount,
                    side=side,
                    price=order_price,
                    order_type=_ot,
                )
                signed_order = await loop.run_in_executor(
                    None, lambda: self.client.create_market_order(market_args, create_opts)
                )
            else:
                order_args = OrderArgs(
                    token_id=token_id,
                    side=side,
                    price=order_price,
                    size=size,
                )
                signed_order = await loop.run_in_executor(
                    None, lambda: self.client.create_order(order_args, create_opts)
                )
            resp = await loop.run_in_executor(
                None, lambda: self.client.post_order(signed_order, _ot, post_only)
            )
            if not isinstance(resp, dict):
                raise RuntimeError(f"unexpected post_order response type: {type(resp).__name__}")
            venue_order_id = (
                resp.get("order_id")
                or resp.get("orderID")
                or resp.get("id")
                or resp.get("orderId")
            )
            if not venue_order_id:
                raise RuntimeError(f"post_order response missing order id keys: {sorted(resp.keys())}")

            order = Order(
                order_id=str(venue_order_id),
                market_id=market_id or "",
                token_id=token_id,
                side=side,
                outcome=_outcome,
                price=order_price,
                size=size,
                status=OrderStatus.PENDING,
            )
            self.pending_orders[order.order_id] = order
            self.order_history.append(order)
            if len(self.order_history) > self._max_order_history:
                self.order_history = self.order_history[-self._max_order_history :]
            return order
        except Exception as e:
            logger.error(f"Error placing order: {e}")
            recovered = await self._recover_recent_trade_after_order_error(
                token_id=token_id,
                side=side,
                size=size,
                price=order_price,
                market_id=market_id or "",
                outcome=_outcome,
            )
            if recovered is not None:
                logger.warning(
                    "Recovered likely fill after post_order error: order_id=%s token=%s",
                    recovered.order_id,
                    token_id[:20],
                )
                return recovered
            return None

    async def place_entry_order(
        self,
        *,
        token_id: str,
        side: str,
        price: float,
        size: float,
        window: Optional[str] = None,
        market_id: str = None,
        dry_run: bool = True,
        order_outcome: Optional[str] = None,
        entry_mode: str = "marketable",
        maker_wait_sec: float = 8.0,
        hybrid_windows: Any = ("15m", "1h"),
        market_title: Optional[str] = None,
        market_slug: Optional[str] = None,
        condition_id: Optional[str] = None,
        outcome_label: Optional[str] = None,
    ) -> Optional[Order]:
        """Place an ENTRY honoring the configured fill mode.

        - ``marketable``: one FAK taker fill — guaranteed, pays the taker fee.
        - ``maker``: one post_only GTC — zero fee, but may never fill.
        - ``hybrid``: rest as post_only GTC for ``maker_wait_sec``; if not fully
          filled, cancel and cross the remainder to a FAK taker. Only applies to
          ``hybrid_windows`` (e.g. 15m/1h); other windows fall back to marketable
          because resting orders rarely fill in fast markets and the latency hurts.

        Paper/dry_run ALWAYS uses the conservative marketable path (instant taker
        fill). Whether a maker leg would have filled in N seconds is pure live
        microstructure and cannot be modeled offline, so paper assumes the worst
        (taker fee) and live can only do better — never paper over-optimism.

        Partial-fill safety: if the maker leg is PARTIAL we keep the partial and do
        NOT cross the remainder, to guarantee we never double-fill.
        """
        meta = dict(
            market_id=market_id,
            order_outcome=order_outcome,
            market_title=market_title,
            market_slug=market_slug,
            condition_id=condition_id,
            outcome_label=outcome_label,
        )
        mode = str(entry_mode or "marketable").lower()
        w = str(window or "").lower()
        try:
            _hybrid_ws = {str(x).lower() for x in (hybrid_windows or ())}
        except TypeError:
            _hybrid_ws = {"15m", "1h"}
        if mode == "hybrid" and w not in _hybrid_ws:
            mode = "marketable"

        # Paper: conservative taker fill regardless of mode (maker savings live-only).
        if dry_run:
            _eff_price = price
            _eff_size = size
            if self._paper_entry_fresh_fill and str(side).upper() == "BUY":
                try:
                    _book = await self.fetch_order_book_snapshot(token_id)
                    if not isinstance(_book, dict):
                        logger.warning(
                            "paper entry fresh-fill: no book snapshot for %s; fail-open at signal price",
                            token_id,
                        )
                    else:
                        _asks = sorted(
                            (
                                (float(level.get("price")), float(level.get("size")))
                                for level in (_book.get("asks") or [])
                                if level.get("price") is not None and level.get("size") is not None
                            ),
                            key=lambda x: x[0],
                        )
                        if not _asks:
                            logger.info(
                                "paper entry fresh-fill no-fill: empty ask book token=%s signal=%.4f size=%.4f",
                                token_id,
                                price,
                                size,
                            )
                            return None
                        _fill_px, _filled = simulate_book_fill(
                            "BUY",
                            size,
                            _asks,
                            marketable=False,
                            limit_price=price + self._paper_entry_fresh_fill_slip_tol,
                            pad_remainder_at_worst=False,
                        )
                        if _filled <= 0 or _fill_px * _filled < 1.0:
                            logger.info(
                                "paper entry fresh-fill no-fill: token=%s signal=%.4f limit=%.4f fill_px=%.4f filled=%.4f",
                                token_id,
                                price,
                                price + self._paper_entry_fresh_fill_slip_tol,
                                _fill_px,
                                _filled,
                            )
                            return None
                        _eff_price, _eff_size = _fill_px, _filled
                except Exception as exc:
                    logger.warning(
                        "paper entry fresh-fill failed for %s; fail-open at signal price: %s",
                        token_id,
                        exc,
                    )
            return await self.place_order(
                token_id=token_id, side=side, price=_eff_price, size=_eff_size,
                post_only=False, order_type="FAK", dry_run=True, **meta,
            )

        if mode == "maker":
            return await self.place_order(
                token_id=token_id, side=side, price=price, size=size,
                post_only=True, order_type="GTC", dry_run=False, **meta,
            )
        if mode != "hybrid":  # marketable (default)
            return await self.place_order(
                token_id=token_id, side=side, price=price, size=size,
                post_only=False, order_type="FAK", dry_run=False, **meta,
            )

        # hybrid: maker leg first
        maker = await self.place_order(
            token_id=token_id, side=side, price=price, size=size,
            post_only=True, order_type="GTC", dry_run=False, **meta,
        )
        if maker is None:
            logger.info("[entry hybrid] maker post failed/rejected; crossing to taker.")
            return await self.place_order(
                token_id=token_id, side=side, price=price, size=size,
                post_only=False, order_type="FAK", dry_run=False, **meta,
            )
        try:
            await asyncio.sleep(max(0.0, float(maker_wait_sec)))
        except Exception:
            pass
        status = await self.get_order_status(maker.order_id)
        if status == OrderStatus.FILLED:
            logger.info("[entry hybrid] filled as MAKER (no taker fee): %s", maker.order_id)
            return maker
        # Cancel whatever's resting; for PARTIAL keep the partial (never double-fill).
        await self.cancel_order(maker.order_id)
        if status == OrderStatus.PARTIAL:
            logger.info(
                "[entry hybrid] maker PARTIAL on %s; keeping partial, not crossing "
                "remainder (double-fill safety).", maker.order_id,
            )
            return maker
        logger.info(
            "[entry hybrid] maker unfilled (status=%s) on %s; crossing full size to taker.",
            status, maker.order_id,
        )
        return await self.place_order(
            token_id=token_id, side=side, price=price, size=size,
            post_only=False, order_type="FAK", dry_run=False, **meta,
        )

    async def cancel_order(self, order_id: str) -> bool:
        if not self.client:
            logger.error("CLOB client not initialized.")
            return False

        try:
            loop = asyncio.get_event_loop()
            # V2 renamed cancel(order_id) -> cancel_order(OrderPayload(orderID=...)).
            await loop.run_in_executor(
                None, lambda: self.client.cancel_order(OrderPayload(orderID=order_id))
            )
            if order_id in self.pending_orders:
                self.pending_orders[order_id].status = OrderStatus.CANCELLED
                self.pending_orders[order_id].updated_at = datetime.now()
            return True
        except Exception as e:
            logger.error(f"Error canceling order {order_id}: {e}")
            return False

    async def get_order_status(self, order_id: str) -> Optional[OrderStatus]:
        if self.using_olympus():
            try:
                data = await self.olympus_client.get_trade_status(order_id)
            except Exception as exc:
                logger.error("Error getting Olympus trade status: %s", exc)
                return None
            status = str(data.get("status") or "").upper()
            if status == "SUCCEEDED":
                self._update_olympus_order_execution(order_id, data)
                return OrderStatus.FILLED
            if status == "FAILED":
                self._update_olympus_order_execution(order_id, data)
                return OrderStatus.FAILED
            if status in {"QUEUED", "PROCESSING"}:
                self._update_olympus_order_execution(order_id, data)
                return OrderStatus.PENDING
            logger.warning(
                "Unknown Olympus trade status: %s",
                self._olympus_failure_report(data) or {"status": data.get("status")},
            )
            return None
        if not self.client:
            logger.error("CLOB client not initialized.")
            return None

        try:
            loop = asyncio.get_event_loop()
            order_data = await loop.run_in_executor(
                None, lambda: self.client.get_order(order_id)
            )
            # /data/order/{id} only returns *active* orders. A filled or cancelled
            # order comes back empty (or without a usable status) — the endpoint
            # "lies by omission." Treating that empty response as PENDING would
            # strand a position that already filled, so fall back to trade history.
            status = order_data.get("status") if isinstance(order_data, dict) else None
            # 2026-07-27 STATUS FIX: Polymarket returns statuses in mixed case + vocab —
            # a filled order can read 'MATCHED'/'MINED'/'CONFIRMED' (uppercase). The old
            # code only matched lowercase 'filled'/'cancelled' and treated every other
            # non-empty status (incl. 'MATCHED') as PENDING, so an exit that ACTUALLY
            # FILLED on-venue got stranded in a "still pending" loop and the position rode
            # to resolution. Normalize case+vocab; for any other non-empty status fall
            # through to trade-history reconciliation (returns FILLED iff a real matched
            # trade exists, else PENDING) instead of blindly returning PENDING.
            _st = str(status or "").strip().lower()
            if _st in ("filled", "matched", "mined", "confirmed"):
                return OrderStatus.FILLED
            # failed/rejected/expired are TERMINAL non-fills → CANCELLED so the caller
            # clears the pending order and re-arms a fresh exit instead of polling a dead
            # order to resolution (Codex nit 2026-07-27).
            if _st in ("cancelled", "canceled", "failed", "rejected", "expired"):
                return OrderStatus.CANCELLED
            # Any other non-empty status (live/open/delayed/unmatched/...) OR empty:
            # reconcile against /data/trades by venue order id — only a truly-open order
            # with no matching trade rests as PENDING.
            return await self._recover_status_from_trades(order_id)
        except Exception as e:
            logger.error(f"Error getting order status for {order_id}: {e}")
            # The order endpoint can also raise on an empty/filled order. Try the
            # trade-history fallback before giving up.
            try:
                return await self._recover_status_from_trades(order_id)
            except Exception as e2:
                logger.error(f"Trade-history fallback failed for {order_id}: {e2}")
                return None

    async def debug_stuck_order(self, order_id: str) -> dict:
        """READ-ONLY diagnostic for a stuck 'pending' exit order.

        Returns the raw CLOB order record and whether trade history references the
        order, so we can definitively tell a RESTING order (non-empty active record,
        status like 'live'/'matched') from a KILLED FAK that is being MISREPORTED as
        pending (empty record + no trade -> _recover_status_from_trades returns
        PENDING at its tail, stranding the position). No side effects; never raises.
        """
        out: Dict[str, Any] = {"order_id": order_id}
        if self.using_olympus() or not self.client:
            out["note"] = "no clob client / olympus"
            return out
        loop = asyncio.get_event_loop()
        try:
            rec = await loop.run_in_executor(
                None, lambda: self.client.get_order(order_id)
            )
            out["order_record_empty"] = not bool(rec)
            if isinstance(rec, dict):
                out["record_status"] = rec.get("status")
                out["record_side"] = rec.get("side")
                out["record_price"] = rec.get("price")
                out["record_original_size"] = rec.get("original_size") or rec.get("size")
                out["record_size_matched"] = rec.get("size_matched")
                out["record_type"] = rec.get("order_type") or rec.get("type")
            else:
                out["order_record_repr"] = repr(rec)[:200]
        except Exception as e:  # noqa: BLE001 - diagnostic must never raise
            out["order_record_error"] = str(e)
        try:
            trades = await loop.run_in_executor(
                None, lambda: self.client.get_trades()
            )
            id_keys = ("order_id", "maker_order_id", "taker_order_id")
            matches = []
            for t in trades or []:
                if isinstance(t, dict) and any(t.get(k) == order_id for k in id_keys):
                    matches.append(
                        {k: t.get(k) for k in ("size", "price", "side", "status") if k in t}
                    )
            out["trade_matches"] = matches
            out["trades_fetched"] = len(trades or [])
        except Exception as e:  # noqa: BLE001
            out["trades_error"] = str(e)
        return out

    async def _recover_status_from_trades(
        self, order_id: str
    ) -> Optional[OrderStatus]:
        """
        Recover an order's terminal status from /data/trades when the order
        endpoint returns empty (filled/cancelled orders are dropped from it).

        Returns FILLED if any trade references this order id, else PENDING
        (resting/unmatched — not yet terminal). Never raises into the caller's
        happy path; raising here is caught and logged by get_order_status.
        """
        loop = asyncio.get_event_loop()
        trades = await loop.run_in_executor(
            None, lambda: self.client.get_trades()
        )
        # Trade payloads vary by py-clob-client version; an order id can appear
        # under several keys depending on whether we were maker or taker.
        id_keys = ("order_id", "maker_order_id", "taker_order_id")
        for trade in trades or []:
            if not isinstance(trade, dict):
                continue
            for key in id_keys:
                if trade.get(key) == order_id:
                    logger.info(
                        "Reconciled order %s as FILLED from trade history "
                        "(order endpoint returned empty).",
                        order_id,
                    )
                    return OrderStatus.FILLED
            # Some builds nest the order ids inside maker/taker sub-objects.
            for nested_key in ("maker_orders", "taker_order"):
                nested = trade.get(nested_key)
                entries = nested if isinstance(nested, list) else [nested]
                for entry in entries:
                    if isinstance(entry, dict) and entry.get("order_id") == order_id:
                        logger.info(
                            "Reconciled order %s as FILLED from nested trade "
                            "history (order endpoint returned empty).",
                            order_id,
                        )
                        return OrderStatus.FILLED
        # No matching trade — order is resting/unmatched, not yet terminal.
        return OrderStatus.PENDING

    async def _recover_recent_trade_after_order_error(
        self,
        *,
        token_id: str,
        side: str,
        size: float,
        price: float,
        market_id: str,
        outcome: str,
    ) -> Optional[Order]:
        """
        Best-effort recovery for the Polymarket "POST errored but filled" shape.

        If post_order raises after the venue accepted/matched the order, the bot
        may not receive an order id. Query recent trades for the same asset token
        before treating the attempt as no-fill. This is intentionally narrow and
        only returns FILLED when the trade payload carries the same token id.
        """
        if not self.client:
            return None
        try:
            loop = asyncio.get_event_loop()

            def _get_trades():
                try:
                    from py_clob_client_v2 import TradeParams

                    return self.client.get_trades(TradeParams(asset_id=token_id))
                except Exception:
                    return self.client.get_trades()

            trades = await loop.run_in_executor(None, _get_trades)
        except Exception as exc:
            logger.error("Post-order trade reconciliation failed: %s", exc)
            return None

        token_l = str(token_id or "").lower()
        for trade in trades or []:
            if not isinstance(trade, dict):
                continue
            asset_values = [
                trade.get("asset_id"),
                trade.get("token_id"),
                trade.get("maker_asset_id"),
                trade.get("taker_asset_id"),
            ]
            if not any(str(v or "").lower() == token_l for v in asset_values):
                continue

            order_id = (
                trade.get("order_id")
                or trade.get("taker_order_id")
                or trade.get("maker_order_id")
                or trade.get("id")
                or f"recovered_{int(time.time() * 1000)}"
            )
            filled_size = (
                trade.get("size")
                or trade.get("matched_amount")
                or trade.get("amount")
                or size
            )
            trade_price = trade.get("price") or price
            order = Order(
                order_id=str(order_id),
                market_id=market_id,
                token_id=token_id,
                side=side,
                outcome=outcome,
                price=float(trade_price),
                size=float(size),
                filled_size=float(filled_size),
                status=OrderStatus.FILLED,
            )
            self.order_history.append(order)
            if len(self.order_history) > self._max_order_history:
                self.order_history = self.order_history[-self._max_order_history :]
            return order
        return None

    async def can_sell_token(self, token_id: str, market_id: str) -> bool:
        """
        Test whether a token can be sold (post-only limit) without placing a real order.
        Used as a pre-trade guard to detect 'unsellable token' risk — the failure mode
        that destroyed a bot going from $23 to $1.50 in 46 hours.

        Polls the orderbook for the given token. If bids exist at a non-zero price,
        the token is sellable. Returns False if the book is empty or only has bids
        at zero price (indicating the market maker won't take the other side).

        Args:
            token_id: The outcome token ID to test
            market_id: The parent market ID (used for logging)

        Returns:
            True if the token can likely be sold, False otherwise.
        """
        if dry_run := self._root_config.get("trading", {}).get("dry_run", True):
            return True

        pc = self._py_client_for_public_reads()
        if not pc:
            logger.error("[can_sell_token] CLOB public-read client unavailable — refusing live trade")
            return False

        try:
            loop = asyncio.get_event_loop()
            book = await loop.run_in_executor(
                None, lambda: pc.get_order_book(token_id)
            )
            bids = book.get("bids", []) or []
            asks = book.get("asks", []) or []
            def _positive_price_count(levels: Any) -> int:
                count = 0
                for level in levels:
                    if not isinstance(level, dict):
                        continue
                    try:
                        if float(level.get("price", 0) or 0) > 0:
                            count += 1
                    except (TypeError, ValueError):
                        continue
                return count

            bid_count = _positive_price_count(bids)
            ask_count = _positive_price_count(asks)
            logger.debug(
                f"[can_sell_token] {market_id[:20]} token={token_id[:20]} "
                f"bids={bid_count} asks={ask_count}"
            )
            return bid_count > 0
        except Exception as e:
            logger.warning(f"[can_sell_token] check failed for {token_id[:20]} — treating as unsellable: {e}")
            return False

    def _py_client_for_public_reads(self):
        """Return authenticated client if present, else a level-0 host-only client."""
        if self.client:
            return self.client
        if PyClobClient is None:
            return None
        if self._readonly_py_client is None:
            self._readonly_py_client = PyClobClient(
                host=self.api_endpoint,
                chain_id=self.chain_id,
            )
        return self._readonly_py_client

    async def fetch_order_book_snapshot(self, token_id: str) -> Optional[Dict[str, Any]]:
        """Public CLOB GET order book (REST). Works without trading keys."""
        pc = self._py_client_for_public_reads()
        if not pc or not token_id:
            return None
        try:
            loop = asyncio.get_event_loop()
            summary = await loop.run_in_executor(
                None, lambda: pc.get_order_book(token_id)
            )

            # py-clob-client v2 returns a dict; v1 returned a BookSummary object.
            # Normalize both shapes so order-book-dependent UI doesn't break.
            if isinstance(summary, dict):
                raw_bids = summary.get("bids") or []
                raw_asks = summary.get("asks") or []
                asset_id = summary.get("asset_id")
                market = summary.get("market")
                timestamp = summary.get("timestamp")
            else:
                raw_bids = getattr(summary, "bids", None) or []
                raw_asks = getattr(summary, "asks", None) or []
                asset_id = getattr(summary, "asset_id", None)
                market = getattr(summary, "market", None)
                timestamp = getattr(summary, "timestamp", None)

            def _normalize_order(o):
                if isinstance(o, dict):
                    return {"price": float(o.get("price", 0)), "size": float(o.get("size", 0))}
                return {"price": float(getattr(o, "price", 0)), "size": float(getattr(o, "size", 0))}

            bids = [_normalize_order(o) for o in raw_bids]
            asks = [_normalize_order(o) for o in raw_asks]
            return {
                "token_id": token_id,
                "asset_id": asset_id,
                "market": market,
                "timestamp": timestamp,
                "bids": bids,
                "asks": asks,
            }
        except Exception as e:
            logger.warning("[fetch_order_book_snapshot] %s", e)
            return None

    async def fetch_taker_fee_rate(self, token_id: str) -> Optional[float]:
        """Return market taker fee rate as a decimal, e.g. ``0.07``.

        Uses py-clob-client's official fee endpoint wrapper
        ``get_fee_rate_bps(token_id)`` and normalizes bps to the decimal fee
        formula used by Polymarket: ``shares * fee_rate * p * (1 - p)``.
        """
        tid = str(token_id or "").strip()
        if not tid:
            return None
        if tid in self._fee_rate_cache:
            return self._fee_rate_cache[tid]
        pc = self._py_client_for_public_reads()
        if not pc:
            return None
        try:
            loop = asyncio.get_event_loop()
            fee_bps = await loop.run_in_executor(None, lambda: pc.get_fee_rate_bps(tid))
            rate = max(0.0, float(fee_bps or 0) / 10_000.0)
            self._fee_rate_cache[tid] = rate
            return rate
        except Exception as e:
            logger.warning("[fetch_taker_fee_rate] %s", e)
            return None

    async def fetch_tick_size(self, token_id: str) -> Optional[str]:
        """Return CLOB minimum tick size for an outcome token."""
        tid = str(token_id or "").strip()
        if not tid:
            return None
        if tid in self._tick_size_cache:
            return self._tick_size_cache[tid]
        pc = self._py_client_for_public_reads()
        if not pc:
            return None
        try:
            loop = asyncio.get_event_loop()
            tick = await loop.run_in_executor(None, lambda: pc.get_tick_size(tid))
            tick_s = str(tick)
            self._tick_size_cache[tid] = tick_s
            return tick_s
        except Exception as e:
            logger.warning("[fetch_tick_size] %s", e)
            return None

    async def fetch_midpoint(self, token_id: str) -> Optional[float]:
        """Return the CLOB ``/midpoint`` for an outcome token (e.g. ``0.55``).

        This is the SAME source the scanner uses to mark entries
        (``scanner.fetch_prices`` -> ``/midpoint``). Marking exits/stops off this
        endpoint instead of a hand-rolled ``(best_bid+best_ask)/2`` keeps entry,
        mark-to-market, and stop on ONE consistent ruler — the cross-ruler
        mismatch (CLOB book mid vs Gamma outcomePrices) was cutting BUY_NO
        winners with phantom stops (2026-06-17: stopped −$4.59/−$3.94 on markets
        that resolved +$17.80/+$30.92). Fail-safe: returns None on any error so
        the caller falls back to the book mid.
        """
        tid = str(token_id or "").strip()
        if not tid:
            return None
        _ttl = getattr(self, "_midpoint_cache_ttl", 1.5)
        if not hasattr(self, "_midpoint_cache"):
            self._midpoint_cache = {}
        if _ttl > 0:
            _hit = self._midpoint_cache.get(tid)
            if _hit is not None and (time.monotonic() - _hit[1]) < _ttl:
                return _hit[0]
        pc = self._py_client_for_public_reads()
        if not pc:
            return None
        try:
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(None, lambda: pc.get_midpoint(tid))
            mid = resp.get("mid") if isinstance(resp, dict) else getattr(resp, "mid", None)
            if mid is None:
                return None
            mid_f = float(mid)
            _out = mid_f if 0.0 < mid_f < 1.0 else None
            if _out is not None and _ttl > 0:
                if len(self._midpoint_cache) > 5000:
                    self._midpoint_cache.clear()
                self._midpoint_cache[tid] = (_out, time.monotonic())
            return _out
        except Exception as e:
            logger.warning("[fetch_midpoint] %s", e)
            return None

    @staticmethod
    def _quantize_price_for_tick(
        price: float,
        tick_size: Optional[str],
        *,
        side: str,
        order_type: str,
    ) -> float:
        """Quantize an order price without rounding away from execution intent."""
        if not tick_size:
            return float(price)
        try:
            tick = Decimal(str(tick_size))
            raw = Decimal(str(price))
        except (InvalidOperation, TypeError, ValueError):
            return float(price)
        if tick <= 0:
            return float(price)

        marketable = str(order_type or "").upper() in {"FAK", "FOK"}
        side_u = str(side or "").upper()
        units = raw / tick
        if side_u == "BUY":
            rounded_units = units.to_integral_value(
                rounding="ROUND_CEILING" if marketable else "ROUND_FLOOR"
            )
        else:
            rounded_units = units.to_integral_value(
                rounding="ROUND_FLOOR" if marketable else "ROUND_CEILING"
            )
        quantized = rounded_units * tick
        quantized = max(tick, min(Decimal("1") - tick, quantized))
        # Avoid float artifacts that can fail SDK price_valid string checks.
        decimals = max(0, -tick.as_tuple().exponent)
        return float(round(quantized, decimals))

    async def get_positions(self) -> List[Position]:
        """CLOB-side position read.

        py-clob-client-v2 removed ``get_positions`` from the SDK — positions now
        come from the Polymarket Data API
        (``GET https://data-api.polymarket.com/positions?user=<funder>``). PSB
        tracks open positions in its own journal/state and has no live caller for
        this method, so we return an empty list rather than ship speculative,
        untested Data-API parsing into the execution path. Wire the Data API here
        only when a real consumer needs on-chain position reconciliation.
        """
        return []


class RiskManager:
    """Risk Management Engine"""

    @staticmethod
    def position_entry_notional(p: Any) -> float:
        """Approximate USD cost at entry for exposure caps (matches evaluate_entry logic)."""
        entry_price = float(getattr(p, "entry_price", 0) or 0)
        sz = float(getattr(p, "size", 0) or 0)
        return sz * entry_price

    @staticmethod
    def _topic_asset(strategy: Optional[str]) -> str:
        raw = str(strategy or "unknown").strip().lower()
        if raw == "bitcoin":
            return "btc"
        if raw.endswith("_macro"):
            raw = raw[: -len("_macro")]
        return raw or "unknown"

    @staticmethod
    def _topic_direction(
        action: Optional[str] = None,
        direction: Optional[str] = None,
        entry_leg: Optional[str] = None,
        outcome: Optional[str] = None,
    ) -> str:
        direction_u = str(direction or "").strip().upper()
        if direction_u in {"LONG", "SHORT"}:
            return direction_u
        if direction_u == "UP":
            return "LONG"
        if direction_u == "DOWN":
            return "SHORT"

        action_u = str(action or "").strip().upper()
        if action_u == "BUY_YES":
            return "LONG"
        if action_u in {"BUY_NO", "SELL_YES"}:
            return "SHORT"

        entry_leg_u = str(entry_leg or "").strip().upper()
        outcome_u = str(outcome or "").strip().upper()
        if entry_leg_u == "NO" or outcome_u == "NO":
            return "SHORT"
        if entry_leg_u == "YES" or outcome_u == "YES":
            return "LONG"
        return "UNKNOWN"

    @classmethod
    def topic_key_for_entry(
        cls,
        strategy: Optional[str],
        action: Optional[str] = None,
        direction: Optional[str] = None,
    ) -> str:
        return f"{cls._topic_asset(strategy)}|{cls._topic_direction(action, direction)}"

    @classmethod
    def topic_key_for_position(cls, position: Any) -> str:
        entry_signal = getattr(position, "entry_signal", {}) or {}
        if not isinstance(entry_signal, dict):
            entry_signal = {}
        asset = cls._topic_asset(getattr(position, "strategy", None))
        direction = cls._topic_direction(
            action=entry_signal.get('action'),
            direction=entry_signal.get('direction'),
            entry_leg=getattr(position, 'entry_leg', None),
            outcome=getattr(position, 'outcome', None),
        )
        return f"{asset}|{direction}"

    def _max_topic_exposure(self) -> float:
        trading_config = self.config.get("trading", {}) or {}
        return float(trading_config.get("max_topic_exposure", self.config.get("max_topic_exposure", 0.20)) or 0.0)

    def __init__(self, config: Dict[str, Any]):
        self.config = config  # Pass the full config
        risk_config = self.config.get("risk", {})
        trading_config = self.config.get("trading", {})
        self.term_risk_config = self.config.get("term_risk", {})
        self.max_concurrent_positions = risk_config.get("max_concurrent_positions", 10)
        self.max_trades_per_day = risk_config.get("max_trades_per_day", 50)
        self.paper_max_trades_per_day = risk_config.get(
            "paper_max_trades_per_day", self.max_trades_per_day
        )
        self.daily_loss_limit = risk_config.get("daily_loss_limit", 0.15)
        self.emergency_stop_loss = risk_config.get("emergency_stop_loss", 0.25)
        self.daily_trades = 0
        self.daily_pnl = 0.0
        self.bankroll = 0.0
        self.last_reset = datetime.now(timezone.utc)
        self.emergency_stopped = False
        self.active_positions: Dict[str, Position] = {}

    def effective_max_trades_per_day(self) -> int:
        if self.config.get("trading", {}).get("dry_run", True):
            return int(self.paper_max_trades_per_day)
        return int(self.max_trades_per_day)

    def can_trade(self, strategy: str = None) -> tuple:
        if self.emergency_stopped:
            return False, "Emergency stop activated"
        if self._should_reset_daily():
            self._reset_daily()
        # Only enforce daily loss limit in live trading — paper/ghost sessions need the data
        if not self.config.get("trading", {}).get("dry_run", True):
            if (
                self.bankroll > 0
                and self.daily_pnl < -self.bankroll * self.daily_loss_limit
            ):
                return False, f"Daily loss limit reached: {self.daily_pnl:.2f}"
        if self.daily_trades >= self.effective_max_trades_per_day():
            return False, "Daily trade limit reached"

        if len(self.active_positions) >= self.max_concurrent_positions:
            return False, "Max concurrent positions reached"
        if strategy:
            strategy_cfg = (
                (self.config.get("strategies", {}) or {}).get(strategy, {}) or {}
            )
            raw_strategy_limit = strategy_cfg.get("max_concurrent_positions")
            if raw_strategy_limit is not None:
                try:
                    strategy_limit = int(raw_strategy_limit)
                except (TypeError, ValueError):
                    strategy_limit = 0
                if strategy_limit > 0:
                    strategy_positions = sum(
                        1
                        for p in self.active_positions.values()
                        if getattr(p, "strategy", "") == strategy
                    )
                    if strategy_positions >= strategy_limit:
                        return (
                            False,
                            f"Max concurrent positions reached for {strategy}",
                        )
        return True, "OK"

    def _get_market_term(self, end_date: Optional[datetime]) -> tuple:
        """Classifies market based on time to resolution."""
        if not end_date:
            return "SHORT_TERM", 0

        days_left = (end_date - datetime.now(end_date.tzinfo)).days

        if days_left >= 14:
            return "LONG_TERM", days_left
        if 7 <= days_left < 14:
            return "MID_TERM", days_left
        return "SHORT_TERM", days_left

    def evaluate_entry(
        self,
        end_date: Optional[datetime],
        current_edge: float,
        bankroll: float,
        strategy: str = None,
        requested_size: float = 0.0,
        market_id: Optional[str] = None,
        action: Optional[str] = None,
        direction: Optional[str] = None,
    ) -> tuple:
        """
        Final check before placing order.
        Returns (bool: can_trade, float: position_size, str: reason)

        PSB's active execution surface is crypto up/down. All positions share
        the same term budget so new crypto assets cannot bypass or get stranded
        in stale legacy buckets.
        """
        term, _ = self._get_market_term(end_date)
        min_edge_map = self.term_risk_config.get("min_edge", {})
        caps_map = self.term_risk_config.get("caps", {})

        # 1. Check if edge is worth the lockup time
        if current_edge < min_edge_map.get(term, 0.05):
            return (
                False,
                0.0,
                f"Edge {current_edge:.2f} too low for {term} (min: {min_edge_map.get(term, 0.05)})",
            )

        # 2. Check if we have budget left for this category
        current_exposure_dict = {t: 0.0 for t in caps_map.keys()}
        for pos in self.active_positions.values():
            pos_term, _ = self._get_market_term(pos.end_date)
            current_exposure_dict[pos_term] += self.position_entry_notional(pos)

        category_spent = current_exposure_dict.get(term, 0.0)
        available_budget = (bankroll * caps_map.get(term, 0.0)) - category_spent

        if available_budget <= 0:
            logger.warning(f"RISK ALERT: {term} budget full. Saving liquidity.")
            return False, 0.0, f"{term} budget full"

        # 3. Return Kelly-computed size, capped only by remaining budget.
        final_size = min(requested_size, available_budget) if requested_size > 0 else available_budget
        if final_size <= 0:
            return False, 0.0, "Entry size resolved to zero"

        can_topic, topic_reason = self.check_topic_exposure(
            bankroll=bankroll,
            trade_size=final_size,
            strategy=strategy,
            action=action,
            direction=direction,
        )
        if not can_topic:
            logger.warning(
                "RISK ALERT: topic exposure blocked market=%s strategy=%s action=%s reason=%s",
                market_id,
                strategy,
                action,
                topic_reason,
            )
            return False, 0.0, topic_reason

        return True, round(final_size, 2), "OK"

    def check_topic_exposure(
        self,
        *,
        bankroll: float,
        trade_size: float,
        strategy: Optional[str],
        action: Optional[str] = None,
        direction: Optional[str] = None,
    ) -> tuple:
        max_topic_exposure = self._max_topic_exposure()
        if max_topic_exposure <= 0 or bankroll <= 0:
            return True, "OK"

        topic = self.topic_key_for_entry(strategy, action=action, direction=direction)
        current_exposure = sum(
            self.position_entry_notional(p)
            for p in self.active_positions.values()
            if self.topic_key_for_position(p) == topic
        )
        cap = bankroll * max_topic_exposure
        if (current_exposure + trade_size) > cap:
            return (
                False,
                "topic_exposure_limit: "
                f"{topic} {current_exposure + trade_size:.2f}/{cap:.2f}",
            )
        return True, "OK"

    def check_strategy_risk(
        self, strategy_name: str, trade_size: float, bankroll: float
    ) -> tuple:
        strategy_config = self.config.get("strategies", {}).get(strategy_name, {})
        max_exposure_pct = strategy_config.get("max_strategy_exposure_pct", 0.05)
        max_trade_size_pct = strategy_config.get("max_trade_size_pct", 0.01)

        # Check max trade size
        if trade_size > (bankroll * max_trade_size_pct):
            return False, f"Trade size exceeds max for {strategy_name}"

        # Check max strategy exposure (dollar cost)
        current_exposure = sum(
            self.position_entry_notional(p)
            for p in self.active_positions.values()
            if getattr(p, "strategy", "") == strategy_name
        )
        if (current_exposure + trade_size) > (bankroll * max_exposure_pct):
            return False, f"Strategy exposure limit reached for {strategy_name}"

        return True, "OK"

    def check_position_risk(
        self, market_id: str, topic: str, current_positions: Dict[str, float]
    ) -> tuple:
        if market_id in self.active_positions:
            return False, "Already have position in this market"
        topic_exposure = current_positions.get(topic, 0.0)
        max_topic_exposure = self._max_topic_exposure()
        if topic_exposure >= max_topic_exposure:
            return False, f"Topic exposure limit reached for {topic}"
        return True, "OK"

    def add_position(self, position: Position):
        self.active_positions[position.position_id] = position
        self.daily_trades += 1
        logger.info(f"Added position: {position.position_id}")

    def remove_position(self, position_id: str):
        if position_id in self.active_positions:
            del self.active_positions[position_id]

    def update_pnl(self, pnl: float):
        self.daily_pnl += pnl
        if (
            self.bankroll > 0
            and self.daily_pnl < -self.bankroll * self.emergency_stop_loss
        ):
            self.trigger_emergency_stop()

    def trigger_emergency_stop(self):
        self.emergency_stopped = True
        logger.critical("EMERGENCY STOP TRIGGERED")

    def reset_emergency_stop(self):
        self.emergency_stopped = False

    def _should_reset_daily(self) -> bool:
        last_reset = self.last_reset
        if last_reset.tzinfo is None:
            last_reset = last_reset.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc).date() > last_reset.date()

    def _reset_daily(self):
        self.daily_trades = 0
        self.daily_pnl = 0.0
        self.last_reset = datetime.now(timezone.utc)

    def get_portfolio_summary(self, total_bankroll: float) -> Dict[str, Any]:
        # Total cost (dollars) — entry-leg aware for BUY_NO vs SELL_YES
        total_cost = sum(
            self.position_entry_notional(p) for p in self.active_positions.values()
        )
        total_exposure = total_cost
        return {
            "total_positions": len(self.active_positions),
            "total_exposure": total_exposure,
            "total_cost": round(total_exposure, 2),
            "exposure_pct": total_exposure / total_bankroll
            if total_bankroll > 0
            else 0,
            "daily_pnl": self.daily_pnl,
            "daily_trades": self.daily_trades,
            "emergency_stopped": self.emergency_stopped,
        }
