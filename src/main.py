"""
Main Entry Point
PolyBot AI - Polymarket Trading Bot
"""

import asyncio
import logging
import os
import re
import signal
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Any, Optional
import yaml
import uvicorn

# Add src and project root to path
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.market.scanner import MarketScanner, Market
from src.market.websocket import WebSocketClient
from src.analysis.ai_agent import AIAgent
from src.analysis.math_utils import PositionSizer
from src.strategies.arbitrage import ArbitrageStrategy, TradeSignal
from src.strategies.fade import FadeStrategy, FadeSignal
from src.strategies.neh import NothingEverHappensStrategy, NEHSignal
from src.strategies.bitcoin import BitcoinStrategy, BitcoinSignal
from src.strategies.sol_lag import SOLLagStrategy, SOLLagSignal
from src.strategies.eth_lag import ETHLagStrategy
from src.strategies.hype_lag import HYPELagStrategy
from src.strategies.xrp_dump_hedge import XRPDumpHedgeStrategy, XRPDumpHedgeSignal
from src.execution.clob_client import CLOBClient, RiskManager, Position
from src.execution.trade_journal import TradeJournal
from src.execution.exposure_manager import ExposureManager
from src.execution.resolution_tracker import ResolutionTracker
from src.execution.ctf_redeemer import CTFRedeemer
from src.execution.live_testing import (
    PositionExitManager,
    PerformanceTracker,
    ExitDecision,
)
from src.analysis.kelly_sizer import KellySizer, get_kelly_sizer
from src.notifications.notification_manager import NotificationManager
from src.env_bootstrap import load_project_dotenv

# Kill switch: if this file exists, the bot will not place new trades (paper or live).
KILL_SWITCH_FILE = Path(__file__).resolve().parent.parent / "data" / "KILL_SWITCH"


def _detect_window_from_question(question: str) -> str:
    """Infer 5m vs 15m window from Polymarket question time range.

    "April 21, 1:30AM-1:35AM ET" → "5m"
    "April 21, 1:30AM-1:45AM ET" → "15m"
    """
    m = re.search(r'(\d+):(\d+)(AM|PM)[–\-](\d+):(\d+)(AM|PM)', question, re.IGNORECASE)
    if not m:
        return "15m"
    h1, m1, p1, h2, m2, p2 = m.groups()
    h1, m1, h2, m2 = int(h1), int(m1), int(h2), int(m2)
    if p1.upper() == 'PM' and h1 != 12:
        h1 += 12
    elif p1.upper() == 'AM' and h1 == 12:
        h1 = 0
    if p2.upper() == 'PM' and h2 != 12:
        h2 += 12
    elif p2.upper() == 'AM' and h2 == 12:
        h2 = 0
    start_min = h1 * 60 + m1
    end_min = h2 * 60 + m2
    delta = abs(end_min - start_min)
    return "5m" if delta <= 6 else "15m"


def _in_resolution_window(
    market, max_days: float, min_hours: float
) -> bool:
    """True if market resolves within [min_hours, max_days] from now. Used to cut noise from end-of-year markets."""
    if market.end_date is None:
        return False
    now = datetime.now(timezone.utc)
    end = market.end_date
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    td = end - now
    hours = td.total_seconds() / 3600
    if hours < min_hours:
        return False
    if td.days > max_days:
        return False
    return True


def _filter_short_horizon(markets, config: Dict) -> list:
    """Filter to markets resolving within the configured window. Crypto (15m) markets are exempt."""
    max_days = config.get("trading", {}).get("max_days_to_resolution", 14)
    min_hours = config.get("trading", {}).get("min_hours_to_resolution", 24)
    result = []
    for m in markets:
        if _is_crypto_market(m):
            result.append(m)  # Crypto 15m markets always included
        elif _in_resolution_window(m, max_days, min_hours):
            result.append(m)
    return result


def _is_crypto_market(market) -> bool:
    """Crypto up/down markets (15m) resolve in minutes; exempt from resolution window filter."""
    if not market.end_date:
        return False
    now = datetime.now(timezone.utc)
    end = market.end_date.replace(tzinfo=timezone.utc) if market.end_date.tzinfo is None else market.end_date
    td = end - now
    return td.total_seconds() < 86400  # Resolves within 24h = likely 15m candle market


class PolyBot:
    """Main trading bot orchestrator"""

    def __init__(self, config_path: str = None):
        # Load configuration
        self.config = self._load_config(config_path)
        self._apply_exposure_env_overrides()

        # Initialize components
        self.market_scanner = MarketScanner(self.config)
        self.ws_client = WebSocketClient(self.config)
        self.ai_agent = AIAgent(self.config)
        self.position_sizer = PositionSizer(
            kelly_fraction=self.config.get("trading", {}).get("kelly_fraction", 0.25),
            max_position_pct=self.config.get("trading", {}).get(
                "max_exposure_per_trade", 0.05
            ),
            min_position=self.config.get("trading", {}).get(
                "default_position_size", 10
            ),
            max_position=self.config.get("trading", {}).get("max_position_size", 15),
        )
        self.kelly_sizer = get_kelly_sizer(self.config)
        self.fade_strategy = FadeStrategy(
            self.config, self.ai_agent, self.position_sizer, self.kelly_sizer
        )
        self.neh_strategy = NothingEverHappensStrategy(
            self.config, self.ai_agent, self.position_sizer
        )
        is_paper = self.config.get("trading", {}).get("dry_run", True)
        # Each crypto strategy gets its OWN exposure manager so losses
        # in one don't pause the other.
        self.btc_exposure_manager = ExposureManager(self.config, is_paper=is_paper)
        self.sol_exposure_manager = ExposureManager(self.config, is_paper=is_paper)
        self.eth_exposure_manager = ExposureManager(self.config, is_paper=is_paper)
        self.hype_exposure_manager = ExposureManager(self.config, is_paper=is_paper)
        self.xrp_exposure_manager = ExposureManager(self.config, is_paper=is_paper)
        self.event_exposure_manager = ExposureManager(self.config, is_paper=is_paper)
        # Keep a reference for resolution tracker settlements
        self.exposure_manager = self.btc_exposure_manager

        self.bitcoin_strategy = BitcoinStrategy(
            self.config,
            self.ai_agent,
            self.position_sizer,
            self.kelly_sizer,
            exposure_manager=self.btc_exposure_manager,
        )

        # ArbitrageStrategy shares the BTC price service so it can use live
        # OHLCV data (ATR-based vol) as its PRIMARY signal for crypto price
        # markets.  AI is still used as a 30% optional confirmer.
        self.arbitrage_strategy = ArbitrageStrategy(
            self.config,
            self.ai_agent,
            self.position_sizer,
            btc_service=self.bitcoin_strategy.btc_service,
        )

        self.sol_lag_strategy = SOLLagStrategy(
            self.config,
            self.ai_agent,
            self.position_sizer,
            self.kelly_sizer,
            exposure_manager=self.sol_exposure_manager,
        )
        self.eth_lag_strategy = ETHLagStrategy(
            self.config,
            self.ai_agent,
            self.position_sizer,
            self.kelly_sizer,
            exposure_manager=self.eth_exposure_manager,
        )
        self.hype_lag_strategy = HYPELagStrategy(
            self.config,
            self.ai_agent,
            self.position_sizer,
            self.kelly_sizer,
            exposure_manager=self.hype_exposure_manager,
        )
        self.xrp_dump_hedge_strategy = XRPDumpHedgeStrategy(
            self.config,
            self.position_sizer,
            self.kelly_sizer,
            exposure_manager=self.xrp_exposure_manager,
            btc_service=self.bitcoin_strategy.btc_service,
        )
        self.clob_client = CLOBClient(self.config)
        self.risk_manager = RiskManager(self.config)
        self.notifier = NotificationManager(self.config)

        # Track last signal counts per strategy (for dashboard)
        self.last_signal_counts = {
            "fade": 0,
            "arbitrage": 0,
            "neh": 0,
            "bitcoin": 0,
            "sol_lag": 0,
            "eth_lag": 0,
            "hype_lag": 0,
            "xrp_dump_hedge": 0,
        }
        # ISO timestamp of the last time each strategy completed a cycle
        self.last_cycle_times: Dict[str, str] = {}
        # Running total of signals ever generated (never resets, lets dashboard show cumulative activity)
        self.cumulative_signal_counts: Dict[str, int] = {}
        # Per-strategy scan diagnostics (AI usage + skip buckets) for observability.
        self.last_ai_scan_stats: Dict[str, Dict[str, Any]] = {}

        # Trade journal: Railway container restarts always start a FRESH session at
        # initial_bankroll (500). This ensures every restart = clean test run.
        # Resume only if PAPER_SESSION_ID is explicitly set to an existing session name.
        _forced_session = os.environ.get("PAPER_SESSION_ID")
        _resume_session = os.environ.get("PAPER_RESUME_SESSION", "false").lower() in ("1", "true", "yes")
        if _forced_session and not _resume_session:
            # Explicit session name given — use it (e.g. PAPER_SESSION_ID=reset_20260416)
            self.journal = TradeJournal(session_id=_forced_session, resume_latest=False)
            logging.info(f"Forced session via PAPER_SESSION_ID={_forced_session}")
        elif _resume_session:
            # Opt-in to resume: PAPER_RESUME_SESSION=true + no session name
            self.journal = TradeJournal(resume_latest=True)
            logging.info(f"Resuming latest session: {self.journal.session_id}")
        else:
            # Default: fresh session every restart ( Railway container lifecycle = test cycle)
            new_id = datetime.now().strftime("test_%Y%m%d_%H%M%S")
            self.journal = TradeJournal(session_id=new_id, resume_latest=False)
            self.bankroll = float(self.config.get("backtest", {}).get("initial_bankroll", 500.0))
            logging.info(f"Fresh session on restart: {new_id} @ ${self.bankroll:.2f}")

        # Resolution tracker — fetches REAL outcomes from Polymarket API
        # Resolution check every 60s — crypto candle markets resolve in 15 minutes
        self.resolution_tracker = ResolutionTracker(check_interval_seconds=60)

        # CTF Redeemer — claims resolved on-chain positions (live mode only).
        # In dry_run (paper) mode this logs DRY RUN messages and never touches the chain.
        # In live mode it calls redeemPositions() on the Polygon CTF contract after each win.
        _dry_run = self.config.get("trading", {}).get("dry_run", True)
        self.ctf_redeemer = CTFRedeemer(
            dry_run=_dry_run,
            private_key=os.environ.get("WALLET_PRIVATE_KEY"),
            rpc_url=os.environ.get("RPC_URL"),
        )

        # Position exit manager — checks active positions for TP/SL/time exits
        self.exit_manager = PositionExitManager(self.config)

        # Performance tracker — aggregates live trade metrics
        self.perf_tracker = PerformanceTracker()

        # State
        self.running = False
        # Serialize order placement, exits, resolution settlement — avoids races between
        # _main_loop and _crypto_fast_loop mutating bankroll / active_positions.
        self._execution_lock = asyncio.Lock()
        self._dashboard_server = None
        _initial_bankroll = self.config.get("backtest", {}).get("initial_bankroll", 1000.0)
        # Restore bankroll from last journal snapshot (or last entries line) so restarts
        # don't reset to initial_bankroll when snapshots.jsonl is sparse.
        _last_snap = self.journal.get_snapshots(limit=1)
        if _last_snap and _last_snap[-1].get("bankroll") is not None:
            self.bankroll = float(_last_snap[-1]["bankroll"])
            logging.info(
                f"Bankroll restored from last snapshot: ${self.bankroll:,.2f} "
                f"(initial was ${_initial_bankroll:,.2f})"
            )
        else:
            _from_log = self.journal.last_bankroll_from_entries_log()
            if _from_log is not None:
                self.bankroll = _from_log
                logging.info(
                    f"Bankroll restored from journal entries: ${self.bankroll:,.2f} "
                    f"(initial was ${_initial_bankroll:,.2f})"
                )
            else:
                self.bankroll = _initial_bankroll
        self.scan_interval = 300  # 5 minutes — conserve API tokens, accuracy > speed

        # Sync open positions AFTER bankroll is known so risk manager has correct baseline
        self._sync_journal_to_risk_manager()
        self.risk_manager.bankroll = self.bankroll

        # Restore today's daily_pnl and daily_trades from journal so mid-day restarts
        # don't reset loss-limit checks to zero (bug: bot could breach daily limit, restart,
        # and immediately trade again as if no losses occurred)
        self._restore_daily_stats()

        # Setup logging
        self._setup_logging()
        em0 = self.btc_exposure_manager
        logging.warning(
            "EXPOSURE per-lane: loss_kill_switch_enabled=%s max_consecutive_losses=%s pause_cycles=%s "
            "(btc/sol/eth/xrp/event each have separate streaks). "
            "If Railway runs an old image, set Variables EXPOSURE_LOSS_KILL_SWITCH_ENABLED=true and restart.",
            em0.loss_kill_switch_enabled,
            em0.max_consecutive_losses,
            em0.pause_cycles,
        )

    def _apply_exposure_env_overrides(self) -> None:
        """Apply exposure toggles from env before ExposureManager construction.

        Docker bakes ``config/settings.yaml`` at **build** time. Without a redeploy,
        production can still have ``loss_kill_switch_enabled: false`` even after Git
        changes. Railway **Variables** override at **process start**:

        - ``EXPOSURE_LOSS_KILL_SWITCH_ENABLED=true`` (or 1/yes/on) → force ON
        - ``false`` / ``0`` / ``no`` / ``off`` → force OFF
        """
        raw = os.environ.get("EXPOSURE_LOSS_KILL_SWITCH_ENABLED", "").strip().lower()
        if raw in ("1", "true", "yes", "on"):
            self.config.setdefault("exposure", {})["loss_kill_switch_enabled"] = True
        elif raw in ("0", "false", "no", "off"):
            self.config.setdefault("exposure", {})["loss_kill_switch_enabled"] = False

    def _load_config(self, config_path: str = None) -> Dict[str, Any]:
        """Load configuration from YAML file"""
        if config_path is None:
            config_path = (
                Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
            )

        try:
            with open(config_path, "r") as f:
                config = yaml.safe_load(f)
            logging.info(f"Loaded config from {config_path}")
            # Merge DISCORD_WEBHOOK_URL from env if not set in YAML
            notifications = config.setdefault("notifications", {})
            if not notifications.get("discord_webhook") and os.getenv(
                "DISCORD_WEBHOOK_URL"
            ):
                notifications["discord_webhook"] = os.getenv("DISCORD_WEBHOOK_URL")
            return config
        except Exception as e:
            logging.warning(f"Could not load config: {e}, using defaults")
            return self._default_config()

    def apply_config_updates(self, updates: Dict[str, Any]) -> None:
        """Merge partial config (e.g. dashboard POST /api/config) into the running bot."""
        from src.config_merge import deep_merge_config

        deep_merge_config(self.config, updates)
        self.ai_agent.refresh_from_config(self.config.get("ai", {}))
        if updates.get("exposure"):
            exp = self.config.get("exposure") or {}
            for attr in (
                "btc_exposure_manager",
                "sol_exposure_manager",
                "eth_exposure_manager",
                "hype_exposure_manager",
                "xrp_exposure_manager",
                "event_exposure_manager",
            ):
                mgr = getattr(self, attr, None)
                if mgr is not None:
                    mgr.reload_from_config(exp)

    def _default_config(self) -> Dict[str, Any]:
        """Default configuration"""
        return {
            "polymarket": {"min_liquidity": 10000, "max_spread": 0.05},
            "trading": {
                "dry_run": True,
                "kelly_fraction": 0.25,
                "max_exposure_per_trade": 0.05,
            },
            "strategies": {
                "arbitrage": {"min_edge": 0.10, "ai_confidence_threshold": 0.70},
            },
            "ai": {"provider": "openai", "model": "gpt-4o"},
            "notifications": {"enabled": False},
            "risk": {"max_concurrent_positions": 10, "daily_loss_limit": 0.15},
        }

    def _sync_journal_to_risk_manager(self):
        """Load open positions from journal into risk manager so we respect limits on restart.

        NOTE: We add positions directly to the dict instead of calling add_position()
        because add_position() increments daily_trades counter. Synced positions are
        historical — they should NOT count toward today's trade limit.
        """
        for pos_data in self.journal.get_open_positions():
            try:
                opened_at = datetime.now()
                if pos_data.get("opened_at"):
                    try:
                        opened_at = datetime.fromisoformat(pos_data["opened_at"])
                    except (ValueError, TypeError):
                        pass
                position = Position(
                    position_id=pos_data["trade_id"],
                    market_id=pos_data["market_id"],
                    market_question=pos_data.get("market_question", ""),
                    outcome=pos_data.get("outcome", "YES"),
                    size=pos_data.get("size", 0),
                    entry_price=pos_data.get("entry_price", 0),
                    current_price=pos_data.get(
                        "current_price", pos_data.get("entry_price", 0)
                    ),
                    pnl=pos_data.get("pnl", 0),
                    opened_at=opened_at,
                    end_date=None,
                    strategy=pos_data.get("strategy", "unknown"),
                )
                # Add directly to dict — do NOT call add_position() as it increments daily_trades
                self.risk_manager.active_positions[position.position_id] = position
            except Exception as e:
                logging.warning(
                    f"Could not sync position {pos_data.get('trade_id')}: {e}"
                )
        synced = len(self.risk_manager.active_positions)
        if synced:
            logging.info(
                f"Synced {synced} open positions from journal to risk manager (daily_trades NOT incremented)"
            )

    def _restore_daily_stats(self):
        """Restore daily_pnl and daily_trades from today's journal EXIT entries.

        Without this, a mid-day restart resets the daily loss limit check to zero,
        allowing the bot to keep trading after already breaching its loss limit.
        """
        today = datetime.now().date()
        daily_pnl = 0.0
        daily_trades = 0
        try:
            for entry in self.journal.get_all_entries(limit=5000):
                ts_str = entry.get("timestamp", "")
                if not ts_str:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_str).date()
                except (ValueError, TypeError):
                    continue
                if ts != today:
                    continue
                if entry.get("event") == "EXIT":
                    pnl = entry.get("pnl", 0) or 0
                    # Sanity guard: skip obviously-buggy EXIT records caused by the
                    # pre-fix token-ordering mismatch on SELL_YES positions.
                    # Bound phantom EXIT PnL using max_position_size (USD) from config.
                    max_plausible = self.config.get("trading", {}).get("max_position_size", 15) * 40
                    if abs(pnl) > max_plausible:
                        logging.debug(
                            f"_restore_daily_stats: skipping anomalous EXIT "
                            f"pnl={pnl:+.2f} (>{max_plausible:.0f}) "
                            f"strategy={entry.get('strategy','?')}"
                        )
                        continue
                    daily_pnl += pnl
                elif entry.get("event") == "ENTRY":
                    daily_trades += 1
            if daily_pnl != 0 or daily_trades > 0:
                self.risk_manager.daily_pnl = daily_pnl
                self.risk_manager.daily_trades = daily_trades
                logging.info(
                    f"Restored daily stats from journal: "
                    f"daily_pnl=${daily_pnl:+.2f}, daily_trades={daily_trades}"
                )
        except Exception as e:
            logging.warning(f"Could not restore daily stats: {e}")

    def _setup_logging(self):
        """Setup logging configuration.

        Uses force=True to override any prior basicConfig call (e.g., from early imports).
        """
        log_config = self.config.get("logging", {})
        level = getattr(logging, log_config.get("level", "INFO"))

        handlers = []
        if log_config.get("console", True):
            handlers.append(logging.StreamHandler())
        if log_config.get("file", True):
            log_dir = Path(__file__).resolve().parent.parent / "data" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            handlers.append(
                logging.FileHandler(
                    log_dir / f"polybot_{datetime.now().strftime('%Y%m%d')}.log"
                )
            )

        # force=True removes any existing handlers/config so our setup actually takes effect.
        logging.basicConfig(
            level=level,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            handlers=handlers,
            force=True,
        )

    def set_api_keys(self, api_keys: Dict[str, str]):
        """Set required API keys for all services."""
        # Pass all keys to the AI agent, which will select the ones it needs
        self.ai_agent.set_api_keys(api_keys)

        # Set credentials for the CLOB client (either key name — env loader may use either)
        polymarket_key = api_keys.get("PRIVATE_KEY") or api_keys.get(
            "POLYMARKET_PRIVATE_KEY"
        )
        if polymarket_key:
            self.clob_client.set_credentials(
                private_key=polymarket_key,
                api_key=api_keys.get("POLYMARKET_API_KEY"),
                api_secret=api_keys.get("POLYMARKET_API_SECRET"),
                api_passphrase=api_keys.get("POLYMARKET_API_PASSPHRASE"),
            )
        else:
            logging.warning(
                "Polymarket private key (PRIVATE_KEY or POLYMARKET_PRIVATE_KEY) not found in .env / config/secrets.env."
            )

    async def start(self):
        """Start the trading bot"""
        self.running = True
        logging.info("=" * 50)
        logging.info("PolyBot AI Starting...")
        logging.info(
            f"Dry Run Mode: {self.config.get('trading', {}).get('dry_run', True)}"
        )
        logging.info("=" * 50)

        from src.ops_pulse import log_ops_startup

        log_ops_startup(self)

        # Notify started (optional — off by default to reduce Discord noise)
        if getattr(self.notifier, "alert_on_status", False):
            await self.notifier.notify_status(
                {"positions": 0, "daily_pnl": 0, "trades_today": 0, "running": True}
            )

        # Run main loop + fast crypto loop + daily coach concurrently
        await asyncio.gather(
            self._main_loop(),
            self._crypto_fast_loop(),
            self._daily_coach_loop(),
        )

        # Cleanup
        await self.shutdown()

        # Force exit — uvicorn daemon thread may hold process on Windows
        import os as _os

        _os._exit(0)

    async def _main_loop(self):
        """Main trading cycle — all strategies, runs every scan_interval."""
        while self.running:
            try:
                await self._trading_cycle()
                await asyncio.sleep(self.scan_interval)
            except Exception as e:
                logging.error(f"Error in trading cycle: {e}", exc_info=True)
                try:
                    await self.notifier.notify_error(str(e))
                except Exception as notify_err:
                    logging.error(f"Failed to send error notification: {notify_err}")
                await asyncio.sleep(30)  # Back off before retry, don't tight-loop on errors

    async def _crypto_fast_loop(self):
        """Fast 5-minute loop for BTC and SOL crypto strategies only.

        These need rapid scanning because they trade 5m/15m candle markets
        that resolve quickly.  Running them on the same slow 15-min main
        cycle means we miss most opportunities.
        """
        CRYPTO_INTERVAL = (
            120  # 2 minutes — entry windows are 2-2.5 min wide; 60s scan catches MACD after crossing
        )
        # Wait a short delay so the first main cycle can populate market data
        await asyncio.sleep(30)

        while self.running:
            try:
                await self._crypto_cycle()
                await asyncio.sleep(CRYPTO_INTERVAL)
            except Exception as e:
                logging.error(f"Error in crypto fast cycle: {e}", exc_info=True)
                try:
                    await self.notifier.notify_error(str(e))
                except Exception:
                    pass
                await asyncio.sleep(30)  # Back off before retry

    async def _daily_coach_loop(self):
        """Run the strategy coach once per day at UTC 06:00 to analyze yesterday's trades."""
        import subprocess
        while self.running:
            try:
                now = datetime.now(timezone.utc)
                # Target 06:00 UTC daily
                next_run = now.replace(hour=6, minute=0, second=0, microsecond=0)
                if next_run <= now:
                    next_run += timedelta(days=1)
                wait_sec = (next_run - now).total_seconds()
                logging.info(f"[coach] Next analysis run in {wait_sec/3600:.1f}h (UTC 06:00)")
                await asyncio.sleep(wait_sec)

                if not self.running:
                    break
                logging.info("[coach] Running daily strategy analysis...")
                proc = await asyncio.create_subprocess_exec(
                    "python", "scripts/strategy_coach.py", "--days-back", "30",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
                output = stdout.decode(errors="replace") if stdout else ""
                logging.info(f"[coach] Analysis complete:\n{output[-2000:]}")
            except asyncio.TimeoutError:
                logging.warning("[coach] Daily analysis timed out after 5 minutes")
            except Exception as e:
                logging.error(f"[coach] Daily analysis error: {e}", exc_info=True)

    async def _crypto_cycle(self):
        """Execute BTC + SOL strategies only (fast cycle)."""
        from src.ops_pulse import log_ops_pulse

        if self._kill_switch_active():
            logging.warning("[FAST] Kill switch active — skipping crypto cycle.")
            log_ops_pulse(self, "crypto")
            return
        logging.info("[FAST] Crypto fast cycle starting...")

        # Scan markets (same scanner, just need BTC/SOL markets)
        opportunities = await self.market_scanner.scan_for_opportunities()
        high_liquidity = opportunities.get("high_liquidity", [])
        scanner_meta = opportunities.get("scanner_meta", {})
        if scanner_meta:
            self.last_ai_scan_stats["scanner"] = dict(scanner_meta)
            logging.info(
                "[FAST] Scanner lookahead: 15m=%s 5m=%s | counts: 15m=%s 5m=%s hype_alt=%s",
                scanner_meta.get("look_ahead_15m"),
                scanner_meta.get("look_ahead_5m"),
                scanner_meta.get("updown_15m_count"),
                scanner_meta.get("updown_5m_count"),
                scanner_meta.get("updown_hype_alt_count"),
            )

        if not high_liquidity:
            logging.info("Crypto cycle: No markets available")
            log_ops_pulse(self, "crypto")
            return

        # For crypto, we do NOT filter out held markets — we want to see
        # if new up/down candle markets appeared even if we hold other BTC/SOL positions
        # (each candle market is a different market_id)
        held_market_ids = set()
        for pos in self.risk_manager.active_positions.values():
            held_market_ids.add(pos.market_id)
        for pos in self.journal.get_open_positions():
            held_market_ids.add(pos.get("market_id", ""))

        available_markets = [m for m in high_liquidity if m.id not in held_market_ids]
        short_horizon = _filter_short_horizon(available_markets, self.config)

        xrp_horizon = short_horizon
        xrp_cfg = self.config.get("strategies", {}).get("xrp_dump_hedge", {})
        if xrp_cfg.get("enabled", False):
            extra = self.xrp_dump_hedge_strategy.markets_for_followup(
                high_liquidity, self.journal, self.risk_manager
            )
            merged = {m.id: m for m in short_horizon}
            for m in extra:
                merged[m.id] = m
            xrp_horizon = list(merged.values())

        # Strategy: Bitcoin Up/Down
        try:
            btc_signals = await self.bitcoin_strategy.scan_and_analyze(
                markets=short_horizon, bankroll=self.bankroll
            )
            _now_iso = datetime.now().isoformat(timespec="seconds")
            self.last_signal_counts["bitcoin"] = len(btc_signals)
            self.last_cycle_times["bitcoin"] = _now_iso
            self.cumulative_signal_counts["bitcoin"] = (
                self.cumulative_signal_counts.get("bitcoin", 0) + len(btc_signals)
            )
            self.last_ai_scan_stats["bitcoin"] = dict(
                getattr(self.bitcoin_strategy, "last_scan_stats", {}) or {}
            )
            for signal in btc_signals:
                await self._execute_bitcoin_signal(signal)
            if btc_signals:
                logging.info(f"[FAST] Crypto BTC: {len(btc_signals)} signals")
            else:
                logging.info("[FAST] Crypto BTC: No signals this cycle")
            _btc_stats = self.last_ai_scan_stats.get("bitcoin", {})
            if _btc_stats:
                logging.info(
                    "[FAST] BTC diagnostics: ai_calls=%s assists=%s vetos=%s holds=%s top_skips=%s",
                    _btc_stats.get("ai_calls", 0),
                    _btc_stats.get("ai_assists", 0),
                    _btc_stats.get("ai_vetos", 0),
                    _btc_stats.get("ai_holds", 0),
                    _btc_stats.get("top_skip_reasons", {}),
                )
        except Exception as e:
            logging.error(f"Crypto BTC error: {e}")

        # Strategy: SOL Lag
        try:
            # Pass current open positions so strategy can enforce concurrent position cap
            self.sol_lag_strategy._open_positions_snapshot = list(
                self.risk_manager.active_positions.values()
            )
            sol_signals = await self.sol_lag_strategy.scan_and_analyze(
                markets=short_horizon, bankroll=self.bankroll
            )
            _now_iso = datetime.now().isoformat(timespec="seconds")
            self.last_signal_counts["sol_lag"] = len(sol_signals)
            self.last_cycle_times["sol_lag"] = _now_iso
            self.cumulative_signal_counts["sol_lag"] = (
                self.cumulative_signal_counts.get("sol_lag", 0) + len(sol_signals)
            )
            for signal in sol_signals:
                await self._execute_sol_lag_signal(signal)
            if sol_signals:
                logging.info(f"[FAST] Crypto SOL: {len(sol_signals)} signals")
            else:
                logging.info("[FAST] Crypto SOL: No signals this cycle")
        except Exception as e:
            logging.error(f"Crypto SOL error: {e}")

        # Strategy: ETH Lag
        try:
            eth_lag_cfg = self.config.get("strategies", {}).get("eth_lag", {})
            if eth_lag_cfg.get("enabled", False):
                self.eth_lag_strategy._open_positions_snapshot = list(
                    self.risk_manager.active_positions.values()
                )
                eth_signals = await self.eth_lag_strategy.scan_and_analyze(
                    markets=short_horizon, bankroll=self.bankroll
                )
                _now_iso = datetime.now().isoformat(timespec="seconds")
                self.last_signal_counts["eth_lag"] = len(eth_signals)
                self.last_cycle_times["eth_lag"] = _now_iso
                self.cumulative_signal_counts["eth_lag"] = (
                    self.cumulative_signal_counts.get("eth_lag", 0) + len(eth_signals)
                )
                for signal in eth_signals:
                    await self._execute_sol_lag_signal(signal)
                if eth_signals:
                    logging.info(f"[FAST] Crypto ETH: {len(eth_signals)} signals")
                else:
                    logging.info("[FAST] Crypto ETH: No signals this cycle")
        except Exception as e:
            logging.error(f"Crypto ETH error: {e}", exc_info=True)

        # Strategy: HYPE Lag
        try:
            hype_lag_cfg = self.config.get("strategies", {}).get("hype_lag", {})
            if hype_lag_cfg.get("enabled", False):
                self.hype_lag_strategy._open_positions_snapshot = list(
                    self.risk_manager.active_positions.values()
                )
                hype_signals = await self.hype_lag_strategy.scan_and_analyze(
                    markets=short_horizon, bankroll=self.bankroll
                )
                _now_iso = datetime.now().isoformat(timespec="seconds")
                self.last_signal_counts["hype_lag"] = len(hype_signals)
                self.last_cycle_times["hype_lag"] = _now_iso
                self.cumulative_signal_counts["hype_lag"] = (
                    self.cumulative_signal_counts.get("hype_lag", 0) + len(hype_signals)
                )
                for signal in hype_signals:
                    await self._execute_sol_lag_signal(signal)
                if hype_signals:
                    logging.info(f"[FAST] Crypto HYPE: {len(hype_signals)} signals")
                else:
                    logging.info("[FAST] Crypto HYPE: No signals this cycle")
        except Exception as e:
            logging.error(f"Crypto HYPE error: {e}", exc_info=True)

        # Strategy: XRP dump-and-hedge (15m up/down)
        try:
            if xrp_cfg.get("enabled", False):
                xrp_signals = await self.xrp_dump_hedge_strategy.scan_and_analyze(
                    markets=xrp_horizon, bankroll=self.bankroll
                )
                _now_iso = datetime.now().isoformat(timespec="seconds")
                self.last_signal_counts["xrp_dump_hedge"] = len(xrp_signals)
                self.last_cycle_times["xrp_dump_hedge"] = _now_iso
                self.cumulative_signal_counts["xrp_dump_hedge"] = (
                    self.cumulative_signal_counts.get("xrp_dump_hedge", 0)
                    + len(xrp_signals)
                )
                for signal in xrp_signals:
                    await self._execute_xrp_dump_hedge_signal(signal)
                if xrp_signals:
                    logging.info(f"[FAST] Crypto XRP dump-hedge: {len(xrp_signals)} signals")
                else:
                    logging.info("[FAST] Crypto XRP dump-hedge: No signals this cycle")
        except Exception as e:
            logging.error(f"Crypto XRP dump-hedge error: {e}", exc_info=True)

        # ── Resolution check for crypto positions ──
        # 15-min candle markets resolve FAST — we need to settle them
        # promptly so the budget frees up for the next candle window.
        try:
            async with self._execution_lock:
                self._run_resolution_check(label="[FAST]")
        except Exception as e:
            logging.error(f"Crypto resolution check error: {e}")

        log_ops_pulse(self, "crypto")

    def _get_exposure_manager_for(self, strategy: str):
        """Return the correct exposure manager for a given strategy."""
        if strategy == "bitcoin":
            return self.btc_exposure_manager
        elif strategy == "sol_lag":
            return self.sol_exposure_manager
        elif strategy == "eth_lag":
            return self.eth_exposure_manager
        elif strategy == "hype_lag":
            return self.hype_exposure_manager
        elif strategy == "xrp_dump_hedge":
            return self.xrp_exposure_manager
        return self.event_exposure_manager

    def _run_resolution_check(self, label: str = ""):
        """Shared resolution check — routes settlements to the correct exposure manager."""
        # We pass exposure_manager=None so the tracker doesn't call record_trade.
        # We'll route it ourselves afterward.
        settled = self.resolution_tracker.check_and_settle(
            journal=self.journal,
            risk_manager=self.risk_manager,
            exposure_manager=None,  # we route manually below
            bankroll=self.bankroll,
            ctf_redeemer=self.ctf_redeemer,
        )
        if settled:
            total_pnl = sum(s["pnl"] for s in settled)
            self.bankroll += total_pnl
            self.risk_manager.update_pnl(total_pnl)   # keeps daily_pnl in sync for dashboard
            self.risk_manager.bankroll = self.bankroll  # keep loss-limit checks against real bankroll

            # Route each settlement to the correct exposure manager
            for s in settled:
                strat = s.get("strategy", "")
                em = self._get_exposure_manager_for(strat)
                em.record_trade(
                    pnl=s["pnl"], strategy=strat, market_id=s.get("market_id", "")
                )

            crypto_settled = [
                s
                for s in settled
                if s.get("strategy")
                in ("bitcoin", "sol_lag", "eth_lag", "hype_lag", "xrp_dump_hedge")
            ]
            event_settled = [
                s
                for s in settled
                if s.get("strategy")
                not in ("bitcoin", "sol_lag", "eth_lag", "hype_lag", "xrp_dump_hedge")
            ]
            if crypto_settled:
                crypto_pnl = sum(s["pnl"] for s in crypto_settled)
                logging.info(
                    f"{label} Settled {len(crypto_settled)} crypto positions, "
                    f"PnL=${crypto_pnl:+.2f}, bankroll=${self.bankroll:,.2f}"
                )
            if event_settled:
                event_pnl = sum(s["pnl"] for s in event_settled)
                logging.info(
                    f"{label} Settled {len(event_settled)} event positions, "
                    f"PnL=${event_pnl:+.2f}, bankroll=${self.bankroll:,.2f}"
                )

        # Update live prices on open positions
        updated = self.resolution_tracker.check_price_updates(
            self.journal, self.bankroll
        )
        if updated:
            logging.info(f"{label} Updated prices on {updated} open positions")

        # Snapshot
        self.journal.take_snapshot(self.bankroll)

    def _kill_switch_active(self) -> bool:
        """Return True if the kill switch file exists (do not place new trades)."""
        return KILL_SWITCH_FILE.exists()

    async def _handle_exit_decision(self, exit_decision: ExitDecision) -> None:
        """Exit order + journal + risk updates (serialized with other execution)."""
        async with self._execution_lock:
            try:
                order = await self.clob_client.place_order(
                    token_id=exit_decision.token_id,
                    side=exit_decision.action,
                    price=exit_decision.exit_price,
                    size=exit_decision.size,
                    market_id=exit_decision.market_id,
                    dry_run=self.config.get("trading", {}).get("dry_run", True),
                )
                if order:
                    logging.info(
                        f"EXIT {exit_decision.reason}: {exit_decision.position_id[:12]} "
                        f"PnL=${exit_decision.unrealized_pnl:+.2f}"
                    )
                    pos = self.risk_manager.active_positions.get(
                        exit_decision.position_id
                    )
                    strat = getattr(pos, "strategy", "unknown") if pos else "unknown"
                    mq = getattr(pos, "market_question", "N/A") if pos else "N/A"
                    em = self._get_exposure_manager_for(strat)
                    em.record_trade(
                        pnl=exit_decision.unrealized_pnl,
                        strategy=strat,
                        market_id=exit_decision.market_id,
                    )
                    window = _detect_window_from_question(mq)
                    self.kelly_sizer.record_outcome(strat, exit_decision.unrealized_pnl > 0, window)
                    exit_pnl = exit_decision.unrealized_pnl
                    self.bankroll += exit_pnl
                    self.risk_manager.update_pnl(exit_pnl)
                    self.risk_manager.bankroll = self.bankroll
                    self.journal.log_exit(
                        trade_id=exit_decision.position_id,
                        exit_price=exit_decision.exit_price,
                        bankroll=self.bankroll,
                        reason=exit_decision.reason,
                    )
                    if exit_decision.position_id in self.risk_manager.active_positions:
                        del self.risk_manager.active_positions[
                            exit_decision.position_id
                        ]
                    await self.notifier.notify_exit(
                        {
                            "question": mq,
                            "strategy": strat,
                            "pnl": exit_pnl,
                            "reason": exit_decision.reason,
                            "price": exit_decision.exit_price,
                            "side": exit_decision.action,
                            "size": exit_decision.size,
                            "market_id": exit_decision.market_id,
                        }
                    )
            except Exception as e:
                logging.error(
                    f"Exit order failed for {exit_decision.position_id}: {e}"
                )

    async def _trading_cycle(self):
        """Execute one trading cycle.

        NOTE: We do NOT do a blanket can_trade() check at the top anymore.
        Each strategy execution handler checks can_trade() individually so that
        hitting the daily trade limit mid-cycle doesn't block ALL strategies —
        only the ones that actually try to place a new trade.
        """
        logging.info("Starting trading cycle...")

        from src.ops_pulse import log_ops_pulse

        if self._kill_switch_active():
            logging.warning(
                "Kill switch active (data/KILL_SWITCH present). Skipping trading cycle."
            )
            log_ops_pulse(self, "main")
            return

        # Scan markets
        opportunities = await self.market_scanner.scan_for_opportunities()
        high_liquidity = opportunities.get("high_liquidity", [])

        if not high_liquidity:
            logging.info("No high liquidity markets found")
            log_ops_pulse(self, "main")
            return

        # Filter out markets we already have positions in (avoid duplicates)
        held_market_ids = set()
        for pos in self.risk_manager.active_positions.values():
            held_market_ids.add(pos.market_id)
        for pos in self.journal.get_open_positions():
            held_market_ids.add(pos.get("market_id", ""))

        # Check active positions for exit conditions (TP/SL/time)
        try:
            market_prices = {m.id: m.yes_price for m in high_liquidity}
            market_token_ids = {
                m.id: (m.token_id_yes, m.token_id_no) for m in high_liquidity
            }
            exits = self.exit_manager.check_exits(
                self.risk_manager.active_positions, market_prices, market_token_ids
            )
            for exit_decision in exits:
                await self._handle_exit_decision(exit_decision)
        except Exception as e:
            logging.error(f"Exit check error: {e}")

        available_markets = [m for m in high_liquidity if m.id not in held_market_ids]
        short_horizon = _filter_short_horizon(available_markets, self.config)
        logging.info(
            f"Markets: {len(high_liquidity)} total, {len(held_market_ids)} held, {len(available_markets)} available, {len(short_horizon)} in resolution window"
        )

        # Strategy 1: Arbitrage (short-horizon) — off by default; not in current live scope
        if self.arbitrage_strategy.enabled:
            try:
                arbitrage_signals = await self.arbitrage_strategy.scan_and_analyze(
                    markets=short_horizon, bankroll=self.bankroll
                )
                _now_iso = datetime.now().isoformat(timespec="seconds")
                self.last_signal_counts["arbitrage"] = len(arbitrage_signals)
                self.last_cycle_times["arbitrage"] = _now_iso
                self.cumulative_signal_counts["arbitrage"] = (
                    self.cumulative_signal_counts.get("arbitrage", 0)
                    + len(arbitrage_signals)
                )
                for signal in arbitrage_signals:
                    if signal.auto_execute:
                        await self._execute_arbitrage_signal(signal)
                    else:
                        logging.info(
                            f"Arb signal below threshold: {signal.market_question[:40]}... edge={signal.edge:.3f}"
                        )
                if arbitrage_signals:
                    logging.info(f"Arbitrage: {len(arbitrage_signals)} signals generated")
            except Exception as e:
                logging.error(f"Arbitrage strategy error: {e}")

        # Strategy 2: Fade — off by default; not in current live scope
        if self.fade_strategy.enabled:
            try:
                fade_signals = await self.fade_strategy.scan_and_analyze(
                    markets=short_horizon, bankroll=self.bankroll
                )
                _now_iso = datetime.now().isoformat(timespec="seconds")
                self.last_signal_counts["fade"] = len(fade_signals)
                self.last_cycle_times["fade"] = _now_iso
                self.cumulative_signal_counts["fade"] = (
                    self.cumulative_signal_counts.get("fade", 0) + len(fade_signals)
                )
                for signal in fade_signals:
                    await self._execute_fade_signal(signal)
                if fade_signals:
                    logging.info(f"Fade: {len(fade_signals)} signals generated")
            except Exception as e:
                logging.error(f"Fade strategy error: {e}")

        # Strategy 4: NEH — off for crypto-only live; enable in YAML when re-testing long-dated book
        if self.neh_strategy.enabled:
            try:
                _open_neh = sum(
                    1 for p in self.journal.get_open_positions()
                    if p.get("strategy") == "neh"
                )
                neh_signals = await self.neh_strategy.scan_and_analyze(
                    markets=available_markets,
                    bankroll=self.bankroll,
                    current_neh_count=_open_neh,
                )
                _now_iso = datetime.now().isoformat(timespec="seconds")
                self.last_signal_counts["neh"] = len(neh_signals)
                self.last_cycle_times["neh"] = _now_iso
                self.cumulative_signal_counts["neh"] = (
                    self.cumulative_signal_counts.get("neh", 0) + len(neh_signals)
                )
                _neh_can_trade, _neh_reason = self.risk_manager.can_trade()
                if not _neh_can_trade and neh_signals:
                    logging.info(f"NEH: {len(neh_signals)} signals blocked ({_neh_reason})")
                else:
                    for signal in neh_signals:
                        await self._execute_neh_signal(signal)
                if neh_signals:
                    logging.info(f"NEH: {len(neh_signals)} signals generated")
            except Exception as e:
                logging.error(f"NEH strategy error: {e}")

        # NOTE: Bitcoin and SOL Lag strategies run in _crypto_fast_loop() only.
        # They were removed from the main loop to avoid double-analyzing the same
        # markets and burning 2x the API tokens. The fast loop handles them on its
        # own 5-minute cadence.

        # Check for market resolutions and settle positions with REAL outcomes
        try:
            async with self._execution_lock:
                self._run_resolution_check(label="[MAIN]")
        except Exception as e:
            logging.error(f"Resolution tracking error: {e}")

        # Take portfolio snapshot for charting
        self.journal.take_snapshot(self.bankroll)

        # Summary
        positions = len(self.risk_manager.active_positions)
        daily = self.risk_manager.daily_trades
        logging.info(
            f"Cycle complete. Positions: {positions}, Daily trades: {daily}/{self.risk_manager.max_trades_per_day}"
        )
        log_ops_pulse(self, "main")

    async def _execute_arbitrage_signal(self, signal: TradeSignal):
        """Execute an arbitrage trade signal"""
        async with self._execution_lock:
            await self._execute_arbitrage_signal_impl(signal)

    async def _execute_arbitrage_signal_impl(self, signal: TradeSignal):
        """Arbitrage entry (holds _execution_lock via caller)."""
        # Check term-based risk
        can_trade, term_size, reason = self.risk_manager.evaluate_entry(
            end_date=signal.end_date, current_edge=signal.edge, bankroll=self.bankroll
        )
        if not can_trade:
            logging.warning(f"Arbitrage trade risk check failed: {reason}")
            self.journal.log_skip(
                signal.market_id,
                signal.market_question,
                "arbitrage",
                reason,
                self.bankroll,
            )
            return

        # Final position size is the minimum of the Kelly size and the term-based budget
        final_size = min(signal.size, term_size)

        # ── T1-1: Unsellable token guard ─────────────────────────────────────
        # For arbitrage: test that we can sell the token we're buying (exit path).
        token_to_test = (
            signal.token_id_yes if signal.action == "BUY_YES"
            else signal.token_id_no if signal.action == "BUY_NO"
            else signal.token_id_yes  # SELL_YES → selling YES token
        )
        if not await self.clob_client.can_sell_token(token_to_test, signal.market_id):
            logging.warning(
                f"Arbitrage unsellable-token skip '{signal.market_question[:40]}' "
                f"— token={token_to_test[:20]} has no bids"
            )
            self.journal.log_skip(
                signal.market_id,
                signal.market_question,
                "arbitrage",
                "unsellable_token",
                self.bankroll,
            )
            return

        logging.info(
            f"Executing trade: {signal.action} {final_size:.2f} @ {signal.price}"
        )

        # Determine token ID and side based on action
        if signal.action == "BUY_YES":
            token_id = signal.token_id_yes
            side = "BUY"
        elif signal.action == "BUY_NO":
            token_id = signal.token_id_no
            side = "BUY"
        else:
            token_id = signal.token_id_yes
            side = "SELL"

        # Polymarket: BUY = dollars, SELL = shares. Strategies return dollars.
        order_size = (final_size / max(0.01, 1.0 - signal.price)) if side == "SELL" and signal.price > 0 else final_size

        # Place order
        order = await self.clob_client.place_order(
            token_id=token_id,
            side=side,
            price=signal.price,
            size=order_size,
            market_id=signal.market_id,
            dry_run=self.config.get("trading", {}).get("dry_run", True),
        )

        if order:
            outcome = signal.action.replace("BUY_", "")
            # Position stores shares (cost=size*price). BUY: pass $, get shares. SELL: pass shares.
            pos_size = final_size / max(0.01, 1.0 - signal.price) if side == "SELL" and signal.price > 0 else final_size
            position = Position(
                position_id=order.order_id,
                market_id=signal.market_id,
                market_question=signal.market_question,
                outcome=outcome,
                size=pos_size,
                entry_price=signal.price,
                current_price=signal.price,
                pnl=0.0,
                opened_at=datetime.now(),
                end_date=signal.end_date,
                strategy="arbitrage",
            )
            self.risk_manager.add_position(position)

            # Persistent journal entry
            self.journal.log_entry(
                trade_id=order.order_id,
                market_id=signal.market_id,
                market_question=signal.market_question,
                strategy="arbitrage",
                action=signal.action,
                side=side,
                outcome=outcome,
                size=pos_size,
                entry_price=signal.price,
                bankroll=self.bankroll,
                edge=signal.edge,
                confidence=getattr(signal, "confidence", 0),
                reason=f"edge={signal.edge:.3f}",
                market_end_at=signal.end_date,
            )

            await self.notifier.notify_trade(
                {
                    "question": signal.market_question,
                    "side": side,
                    "outcome": outcome,
                    "size": final_size,
                    "price": signal.price,
                    "auto_execute": True,
                    "strategy": "arbitrage",
                }
            )

    async def _execute_fade_signal(self, signal: FadeSignal):
        """Execute a fade trade signal"""
        async with self._execution_lock:
            await self._execute_fade_signal_impl(signal)

    async def _execute_fade_signal_impl(self, signal: FadeSignal):
        """Fade entry (holds _execution_lock via caller)."""
        can_trade, term_size, reason = self.risk_manager.evaluate_entry(
            end_date=signal.end_date,
            current_edge=signal.implied_probability_gap,
            bankroll=self.bankroll,
        )
        if not can_trade:
            logging.warning(f"Fade trade term risk check failed: {reason}")
            self.journal.log_skip(
                signal.market_id,
                signal.market_question,
                "fade",
                f"term_risk: {reason}",
                self.bankroll,
            )
            return

        can_trade, reason = self.risk_manager.check_strategy_risk(
            strategy_name="fade", trade_size=signal.size, bankroll=self.bankroll
        )
        if not can_trade:
            logging.warning(f"Fade trade strategy risk check failed: {reason}")
            self.journal.log_skip(
                signal.market_id,
                signal.market_question,
                "fade",
                f"strategy_risk: {reason}",
                self.bankroll,
            )
            return

        final_size = min(signal.size, term_size)

        # ── T1-1: Unsellable token guard ─────────────────────────────────────
        token_to_test = signal.token_id_yes if signal.action == "BUY_YES" else signal.token_id_yes
        if not await self.clob_client.can_sell_token(token_to_test, signal.market_id):
            logging.warning(
                f"Fade unsellable-token skip '{signal.market_question[:40]}' "
                f"— token={token_to_test[:20]} has no bids"
            )
            self.journal.log_skip(
                signal.market_id,
                signal.market_question,
                "fade",
                "unsellable_token",
                self.bankroll,
            )
            return

        logging.info(
            f"Executing FADE trade: {signal.action} {final_size:.2f} @ {signal.price}"
        )

        token_id = signal.token_id_yes
        side = "BUY" if signal.action == "BUY_YES" else "SELL"
        order_size = (final_size / max(0.01, 1.0 - signal.price)) if side == "SELL" and signal.price > 0 else final_size
        pos_size = final_size / max(0.01, 1.0 - signal.price) if side == "SELL" and signal.price > 0 else final_size

        order = await self.clob_client.place_order(
            token_id=token_id,
            side=side,
            price=signal.price,
            size=order_size,
            market_id=signal.market_id,
            post_only=True,
            dry_run=self.config.get("trading", {}).get("dry_run", True),
        )

        if order:
            outcome = signal.action.replace("BUY_", "").replace("SELL_", "")
            position = Position(
                position_id=order.order_id,
                market_id=signal.market_id,
                market_question=signal.market_question,
                outcome=outcome,
                size=pos_size,
                entry_price=signal.price,
                current_price=signal.price,
                pnl=0.0,
                opened_at=datetime.now(),
                end_date=signal.end_date,
                strategy="fade",
            )
            self.risk_manager.add_position(position)

            self.journal.log_entry(
                trade_id=order.order_id,
                market_id=signal.market_id,
                market_question=signal.market_question,
                strategy="fade",
                action=signal.action,
                side=side,
                outcome=outcome,
                size=pos_size,
                entry_price=signal.price,
                bankroll=self.bankroll,
                edge=signal.implied_probability_gap,
                confidence=getattr(signal, "confidence", 0),
                reason=f"IPG={signal.implied_probability_gap:.3f}",
                market_end_at=signal.end_date,
            )

            await self.notifier.notify_trade(
                {
                    "question": signal.market_question,
                    "side": side,
                    "outcome": outcome,
                    "size": final_size,
                    "price": signal.price,
                    "auto_execute": True,
                    "strategy": "fade",
                }
            )

    async def _execute_neh_signal(self, signal: NEHSignal):
        """Execute a Nothing Ever Happens trade signal."""
        async with self._execution_lock:
            await self._execute_neh_signal_impl(signal)

    async def _execute_neh_signal_impl(self, signal: NEHSignal):
        """NEH entry (holds _execution_lock via caller)."""
        can_trade, reason = self.risk_manager.can_trade()
        if not can_trade:
            logging.debug(f"NEH trade skipped: {reason}")
            self.journal.log_skip(
                signal.market_id, signal.market_question, "neh", reason, self.bankroll
            )
            return

        # ── T1-1: Unsellable token guard ─────────────────────────────────────
        # NEH sells YES tokens — verify we can buy them back (orderbook has bids).
        if not await self.clob_client.can_sell_token(signal.token_id_yes, signal.market_id):
            logging.warning(
                f"NEH unsellable-token skip '{signal.market_question[:40]}' "
                f"— token={signal.token_id_yes[:20]} has no bids"
            )
            self.journal.log_skip(
                signal.market_id,
                signal.market_question,
                "neh",
                "unsellable_token",
                self.bankroll,
            )
            return

        logging.info(
            f"Executing NEH trade: {signal.action} {signal.size:.2f} of {signal.market_question[:40]}... @ {signal.price}"
        )

        token_id = signal.token_id_yes
        side = "SELL"
        # Polymarket SELL_YES: risk per share = (1.0 - entry_price), NOT entry_price.
        # At price=0.01, risk is $0.99/share. Divide by risk to get correct share count.
        # OLD BUG: divided by price (0.01) → 500 shares → $495 risk on a $5 bet
        # FIX: divide by (1 - price) → 5.05 shares → $5 risk as intended
        risk_per_share = max(0.01, 1.0 - signal.price)
        order_size = signal.size / risk_per_share
        pos_size = order_size

        order = await self.clob_client.place_order(
            token_id=token_id,
            side=side,
            price=signal.price,
            size=order_size,
            market_id=signal.market_id,
            post_only=True,
            dry_run=self.config.get("trading", {}).get("dry_run", True),
        )

        if order and hasattr(order, "order_id"):
            position = Position(
                position_id=order.order_id,
                market_id=signal.market_id,
                market_question=signal.market_question,
                outcome="NO",
                size=pos_size,
                entry_price=signal.price,
                current_price=signal.price,
                pnl=0.0,
                opened_at=datetime.now(),
                end_date=signal.end_date,
                strategy="neh",
            )
            self.risk_manager.add_position(position)

            self.journal.log_entry(
                trade_id=order.order_id,
                market_id=signal.market_id,
                market_question=signal.market_question,
                strategy="neh",
                action=signal.action,
                side=side,
                outcome="NO",
                size=pos_size,
                entry_price=signal.price,
                bankroll=self.bankroll,
                edge=getattr(signal, "edge", 0),
                confidence=getattr(signal, "confidence", 0),
                reason=f"yes_price={signal.price:.3f}",
                market_end_at=signal.end_date,
            )

            await self.notifier.notify_trade(
                {
                    "question": signal.market_question,
                    "side": side,
                    "outcome": "NO",
                    "size": signal.size,
                    "price": signal.price,
                    "auto_execute": True,
                    "strategy": "neh",
                }
            )

    async def _execute_bitcoin_signal(self, signal: BitcoinSignal):
        """Execute a Bitcoin Up/Down trade signal."""
        async with self._execution_lock:
            await self._execute_bitcoin_signal_impl(signal)

    async def _execute_bitcoin_signal_impl(self, signal: BitcoinSignal):
        """Bitcoin entry (holds _execution_lock via caller)."""
        can_trade, reason = self.risk_manager.can_trade(strategy="bitcoin")
        if not can_trade:
            logging.warning(f"Bitcoin trade risk check failed: {reason}")
            self.journal.log_skip(
                signal.market_id,
                signal.market_question,
                "bitcoin",
                reason,
                self.bankroll,
            )
            return

        # Term-based risk check (crypto-isolated budget)
        can_trade, term_size, reason = self.risk_manager.evaluate_entry(
            end_date=signal.end_date,
            current_edge=signal.edge,
            bankroll=self.bankroll,
            strategy="bitcoin",
        )
        if not can_trade:
            logging.warning(f"Bitcoin trade term risk check failed: {reason}")
            self.journal.log_skip(
                signal.market_id,
                signal.market_question,
                "bitcoin",
                f"term_risk: {reason}",
                self.bankroll,
            )
            return

        final_size = min(signal.size, term_size)

        token_id = signal.token_id_yes
        side = "BUY" if signal.action == "BUY_YES" else "SELL"

        # ── T1-1: Unsellable token guard ─────────────────────────────────────
        # Before placing any order, verify the position can be exited.
        # BTC signals are BUY_YES / SELL_YES — both operate on the YES token,
        # so we test that the YES token has resting bids before entering.
        token_to_test = signal.token_id_yes
        if not await self.clob_client.can_sell_token(token_to_test, signal.market_id):
            logging.warning(
                f"Bitcoin unsellable-token skip '{signal.market_question[:40]}' "
                f"— token={token_to_test[:20]} has no bids"
            )
            self.journal.log_skip(
                signal.market_id,
                signal.market_question,
                "bitcoin",
                "unsellable_token",
                self.bankroll,
            )
            return

        logging.info(
            f"Executing BITCOIN trade: {signal.action} {final_size:.2f} @ {signal.price} ({signal.direction})"
        )
        order_size = (final_size / max(0.01, 1.0 - signal.price)) if side == "SELL" and signal.price > 0 else final_size
        pos_size = final_size / max(0.01, 1.0 - signal.price) if side == "SELL" and signal.price > 0 else final_size

        order = await self.clob_client.place_order(
            token_id=token_id,
            side=side,
            price=signal.price,
            size=order_size,
            market_id=signal.market_id,
            post_only=True,
            dry_run=self.config.get("trading", {}).get("dry_run", True),
        )

        if order and hasattr(order, "order_id"):
            outcome = "YES" if signal.action == "BUY_YES" else "NO"
            position = Position(
                position_id=order.order_id,
                market_id=signal.market_id,
                market_question=signal.market_question,
                outcome=outcome,
                size=pos_size,
                entry_price=signal.price,
                current_price=signal.price,
                pnl=0.0,
                opened_at=datetime.now(),
                end_date=signal.end_date,
                strategy="bitcoin",
            )
            self.risk_manager.add_position(position)

            self.journal.log_entry(
                trade_id=order.order_id,
                market_id=signal.market_id,
                market_question=signal.market_question,
                strategy="bitcoin",
                action=signal.action,
                side=side,
                outcome=outcome,
                size=pos_size,
                entry_price=signal.price,
                bankroll=self.bankroll,
                edge=signal.edge,
                confidence=signal.confidence,
                reason=f"btc_{signal.direction} ai={signal.ai_used}",
                extra={
                    "hour_utc": signal.hour_utc,
                    "window_size": signal.window_size,
                    "htf_bias": signal.htf_bias,
                    "ai_used": signal.ai_used,
                    "ai_confidence": signal.confidence if signal.ai_used else None,
                    "yes_price": signal.price,
                    "btc_price": signal.btc_current,
                    "edge": signal.edge,
                    # Learning context: direction, threshold, and full signal reason
                    # so exit records can explain why a trade was entered.
                    "direction": signal.direction,
                    "btc_threshold": signal.btc_threshold,
                    "signal_reason": signal.reason,
                },
                market_end_at=signal.end_date,
            )

            await self.notifier.notify_trade(
                {
                    "question": signal.market_question,
                    "side": side,
                    "outcome": outcome,
                    "size": final_size,
                    "price": signal.price,
                    "auto_execute": True,
                    "strategy": "bitcoin",
                }
            )

    async def _execute_sol_lag_signal(self, signal: SOLLagSignal):
        """Execute a SOL or ETH lag trade signal (same execution path)."""
        async with self._execution_lock:
            await self._execute_sol_lag_signal_impl(signal)

    async def _execute_sol_lag_signal_impl(self, signal: SOLLagSignal):
        """SOL/ETH lag entry (holds _execution_lock via caller)."""
        strat = signal.strategy_name
        can_trade, reason = self.risk_manager.can_trade(strategy=strat)
        if not can_trade:
            logging.warning(f"{strat} trade risk check failed: {reason}")
            self.journal.log_skip(
                signal.market_id,
                signal.market_question,
                strat,
                reason,
                self.bankroll,
            )
            return

        # Term-based risk check (crypto-isolated budget)
        can_trade, term_size, reason = self.risk_manager.evaluate_entry(
            end_date=signal.end_date,
            current_edge=signal.edge,
            bankroll=self.bankroll,
            strategy=strat,
        )
        if not can_trade:
            logging.warning(f"{strat} trade term risk check failed: {reason}")
            self.journal.log_skip(
                signal.market_id,
                signal.market_question,
                strat,
                f"term_risk: {reason}",
                self.bankroll,
            )
            return

        final_size = min(signal.size, term_size)

        token_id = signal.token_id_yes
        side = "BUY" if signal.action == "BUY_YES" else "SELL"

        # ── T1-1: Unsellable token guard ─────────────────────────────────────
        # SOL/ETH/HYPE lag signals are BUY_YES / SELL_YES — both operate on the
        # YES token, so we verify YES has bids before committing to an entry.
        token_to_test = signal.token_id_yes
        if not await self.clob_client.can_sell_token(token_to_test, signal.market_id):
            logging.warning(
                f"{strat} unsellable-token skip '{signal.market_question[:40]}' "
                f"— token={token_to_test[:20]} has no bids"
            )
            self.journal.log_skip(
                signal.market_id,
                signal.market_question,
                strat,
                "unsellable_token",
                self.bankroll,
            )
            return

        logging.info(
            f"Executing {strat} trade: {signal.action} {final_size:.2f} @ {signal.price} ({signal.direction})"
        )
        order_size = (final_size / max(0.01, 1.0 - signal.price)) if side == "SELL" and signal.price > 0 else final_size
        pos_size = final_size / max(0.01, 1.0 - signal.price) if side == "SELL" and signal.price > 0 else final_size

        order = await self.clob_client.place_order(
            token_id=token_id,
            side=side,
            price=signal.price,
            size=order_size,
            market_id=signal.market_id,
            post_only=True,
            dry_run=self.config.get("trading", {}).get("dry_run", True),
        )

        if order and hasattr(order, "order_id"):
            outcome = "YES" if signal.action == "BUY_YES" else "NO"
            position = Position(
                position_id=order.order_id,
                market_id=signal.market_id,
                market_question=signal.market_question,
                outcome=outcome,
                size=pos_size,
                entry_price=signal.price,
                current_price=signal.price,
                pnl=0.0,
                opened_at=datetime.now(),
                end_date=signal.end_date,
                strategy=strat,
            )
            self.risk_manager.add_position(position)

            self.journal.log_entry(
                trade_id=order.order_id,
                market_id=signal.market_id,
                market_question=signal.market_question,
                strategy=strat,
                action=signal.action,
                side=side,
                outcome=outcome,
                size=pos_size,
                entry_price=signal.price,
                bankroll=self.bankroll,
                edge=signal.edge,
                confidence=signal.confidence,
                reason=f"{strat}_{signal.direction} lag={signal.lag_magnitude} side={signal.action} ai={signal.ai_used} | {signal.reason[:120]}",
                extra={
                    "hour_utc": signal.hour_utc,
                    "window_size": signal.window_size,
                    "htf_bias": signal.htf_bias,
                    "ai_used": signal.ai_used,
                    "ai_confidence": signal.confidence if signal.ai_used else None,
                    "yes_price": signal.price,
                    "sol_price": signal.sol_current,
                    "btc_price": signal.btc_current,
                    "lag_magnitude": signal.lag_magnitude,
                    "edge": signal.edge,
                    # Learning context: direction and full signal reason
                    "direction": signal.direction,
                    "signal_reason": signal.reason,
                },
                market_end_at=signal.end_date,
            )

            await self.notifier.notify_trade(
                {
                    "question": signal.market_question,
                    "side": side,
                    "outcome": outcome,
                    "size": final_size,
                    "price": signal.price,
                    "auto_execute": True,
                    "strategy": strat,
                }
            )

    async def _execute_xrp_dump_hedge_signal(self, signal: XRPDumpHedgeSignal):
        """Execute XRP dump (leg1 YES) or hedge (leg2 NO). Quant-only strategy."""
        async with self._execution_lock:
            await self._execute_xrp_dump_hedge_signal_impl(signal)

    async def _execute_xrp_dump_hedge_signal_impl(self, signal: XRPDumpHedgeSignal):
        strat = "xrp_dump_hedge"
        can_trade, reason = self.risk_manager.can_trade(strategy=strat)
        if not can_trade:
            logging.warning(f"{strat} trade risk check failed: {reason}")
            self.journal.log_skip(
                signal.market_id,
                signal.market_question,
                strat,
                reason,
                self.bankroll,
            )
            return

        can_trade, term_size, reason = self.risk_manager.evaluate_entry(
            end_date=signal.end_date,
            current_edge=signal.edge,
            bankroll=self.bankroll,
            strategy=strat,
        )
        if not can_trade:
            logging.warning(f"{strat} term risk check failed: {reason}")
            self.journal.log_skip(
                signal.market_id,
                signal.market_question,
                strat,
                f"term_risk: {reason}",
                self.bankroll,
            )
            return

        final_size = min(signal.size, term_size)

        # ── T1-1: Unsellable token guard ─────────────────────────────────────
        token_to_test = signal.token_id_no if signal.action == "BUY_YES" else signal.token_id_yes
        if not await self.clob_client.can_sell_token(token_to_test, signal.market_id):
            logging.warning(
                f"{strat} unsellable-token skip '{signal.market_question[:40]}' "
                f"— token={token_to_test[:20]} has no bids"
            )
            self.journal.log_skip(
                signal.market_id,
                signal.market_question,
                strat,
                "unsellable_token",
                self.bankroll,
            )
            return

        logging.info(
            f"Executing {strat} leg{signal.leg}: {signal.action} {final_size:.2f} @ {signal.price}"
        )

        if signal.action == "BUY_YES":
            token_id = signal.token_id_yes
            side = "BUY"
        elif signal.action == "BUY_NO":
            token_id = signal.token_id_no
            side = "BUY"
        else:
            logging.warning(f"{strat}: unsupported action {signal.action}")
            return

        order_size = (
            (final_size / max(0.01, 1.0 - signal.price))
            if side == "SELL" and signal.price > 0
            else final_size
        )
        pos_size = (
            final_size / max(0.01, 1.0 - signal.price)
            if side == "SELL" and signal.price > 0
            else final_size
        )

        order = await self.clob_client.place_order(
            token_id=token_id,
            side=side,
            price=signal.price,
            size=order_size,
            market_id=signal.market_id,
            post_only=True,
            dry_run=self.config.get("trading", {}).get("dry_run", True),
        )

        if order and hasattr(order, "order_id"):
            outcome = "YES" if signal.action == "BUY_YES" else "NO"
            position = Position(
                position_id=order.order_id,
                market_id=signal.market_id,
                market_question=signal.market_question,
                outcome=outcome,
                size=pos_size,
                entry_price=signal.price,
                current_price=signal.price,
                pnl=0.0,
                opened_at=datetime.now(),
                end_date=signal.end_date,
                strategy=strat,
            )
            self.risk_manager.add_position(position)

            self.journal.log_entry(
                trade_id=order.order_id,
                market_id=signal.market_id,
                market_question=signal.market_question,
                strategy=strat,
                action=signal.action,
                side=side,
                outcome=outcome,
                size=pos_size,
                entry_price=signal.price,
                bankroll=self.bankroll,
                edge=signal.edge,
                confidence=signal.confidence,
                reason=f"{strat} leg={signal.leg} | {signal.reason[:160]}",
                market_end_at=signal.end_date,
            )

            await self.notifier.notify_trade(
                {
                    "question": signal.market_question,
                    "side": side,
                    "outcome": outcome,
                    "size": final_size,
                    "price": signal.price,
                    "auto_execute": True,
                    "strategy": strat,
                }
            )

    async def shutdown(self):
        """Shutdown the bot gracefully"""
        logging.info("Shutting down PolyBot...")
        self.running = False

        await self.market_scanner.close()
        await self.ws_client.disconnect()
        await self.notifier.close()

        # Stop dashboard server
        if self._dashboard_server:
            self._dashboard_server.should_exit = True

        logging.info("PolyBot shutdown complete")

    def stop(self):
        """Stop the bot"""
        self.running = False
        if self._dashboard_server:
            self._dashboard_server.should_exit = True


def start_dashboard(bot: Optional["PolyBot"]):
    """Starts the Uvicorn server in a separate thread if enabled in config.

    ``bot`` may be ``None`` during Railway/bootstrap: bind HTTP + /health before
    ``PolyBot()`` journal I/O. Call ``set_bot_instance(bot)`` after the bot is ready.
    """
    import time
    import socket as _socket

    dashboard_config = bot.config.get("dashboard", {})
    if not dashboard_config.get("enabled", False):
        logging.info("Dashboard is disabled in the configuration.")
        return

    # PaaS (Railway, Render, etc.) sets PORT — bind 0.0.0.0 and ignore dashboard_port.
    if os.environ.get("PORT"):
        port = int(os.environ["PORT"])
        host = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
    else:
        host = dashboard_config.get("host", "127.0.0.1")
        port = int(dashboard_config.get("dashboard_port", 8080))

    # Local socket checks / browser must target a real address, not 0.0.0.0.
    connect_host = "127.0.0.1" if host == "0.0.0.0" else host

    if os.environ.get("RAILWAY_PUBLIC_DOMAIN"):
        pd = os.environ["RAILWAY_PUBLIC_DOMAIN"].strip()
        display_url = pd if pd.startswith("http") else f"https://{pd}"
    elif host == "0.0.0.0":
        display_url = f"http://127.0.0.1:{port}"
    else:
        display_url = f"http://{host}:{port}"

    skip_browser = (
        os.environ.get("PORT") is not None
        or os.environ.get("RAILWAY_ENVIRONMENT") is not None
        or os.environ.get("DASHBOARD_OPEN_BROWSER", "").lower() in ("0", "false", "no")
    )

    def _open_browser(target_url: str):
        """Open browser using the most reliable method for the current OS."""
        import subprocess
        import sys as _sys
        try:
            if _sys.platform == "win32":
                subprocess.Popen(
                    ["cmd", "/c", "start", "", target_url],
                    shell=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            elif _sys.platform == "darwin":
                subprocess.Popen(["open", target_url])
            else:
                subprocess.Popen(["xdg-open", target_url])
        except Exception:
            import webbrowser
            webbrowser.open(target_url)

    def _port_in_use() -> bool:
        """Return True if something is already listening on host:port."""
        try:
            with _socket.create_connection((connect_host, port), timeout=0.5):
                return True
        except (ConnectionRefusedError, OSError):
            return False

    def _wait_until_port_accepts(timeout: float = 90.0) -> bool:
        """Block until TCP accepts on connect_host:port (Railway health checks need this)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with _socket.create_connection((connect_host, port), timeout=0.75):
                    logging.info(
                        "Dashboard is accepting connections on %s:%s — probes can pass",
                        connect_host,
                        port,
                    )
                    return True
            except (ConnectionRefusedError, OSError):
                time.sleep(0.2)
        logging.error(
            "Dashboard did not accept on %s:%s within %.0fs — "
            "platform health checks may hang in Initializing (see thread/import errors above).",
            connect_host,
            port,
            timeout,
        )
        return False

    # If something is already on the port, kill it so THIS bot instance
    # takes over as the server and set_bot_instance(bot) is properly called.
    # A stale --dashboard-only process has no bot reference and will show zeros.
    # On PaaS (PORT set), never run fuser/taskkill: ephemeral port reuse / sidecars can
    # cause false positives; binding is the source of truth.
    if _port_in_use() and not os.environ.get("PORT"):
        logging.info(f"Stale process on {port} — evicting so bot can own the dashboard")
        try:
            import subprocess as _sp
            if sys.platform == "win32":
                result = _sp.run(
                    f'netstat -ano | findstr ":{port} " | findstr LISTENING',
                    shell=True, capture_output=True, text=True
                )
                for line in result.stdout.strip().splitlines():
                    parts = line.split()
                    if parts:
                        pid = parts[-1]
                        _sp.run(
                            f"taskkill /PID {pid} /F /T",
                            shell=True,
                            stdout=_sp.DEVNULL,
                            stderr=_sp.DEVNULL,
                        )
            else:
                _sp.run(f"fuser -k {port}/tcp", shell=True,
                        stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        except Exception as e:
            logging.warning(f"Could not evict stale dashboard: {e}")
        # Wait for port to free
        for _ in range(10):
            time.sleep(0.5)
            if not _port_in_use():
                break

    def run_server():
        logging.info(f"Starting dashboard server (bind {host}:{port}) — open: {display_url}")
        from src.dashboard.server import app, set_bot_instance, register_dashboard_uvicorn_server

        set_bot_instance(bot)
        server = uvicorn.Server(
            uvicorn.Config(app, host=host, port=port, log_level="warning")
        )
        register_dashboard_uvicorn_server(server)
        if bot is not None:
            bot._dashboard_server = server
        server.run()

    def open_when_ready():
        for _ in range(40):
            try:
                with _socket.create_connection((connect_host, port), timeout=1):
                    print(f"  Dashboard ready -> {display_url}")
                    if not skip_browser:
                        _open_browser(display_url)
                    return
            except (ConnectionRefusedError, OSError):
                time.sleep(0.5)
        logging.warning(
            f"Dashboard did not start within 20s -- open manually: {display_url}"
        )

    def log_when_ready_no_browser():
        for _ in range(40):
            try:
                with _socket.create_connection((connect_host, port), timeout=1):
                    logging.info(f"Dashboard listening — {display_url}")
                    return
            except (ConnectionRefusedError, OSError):
                time.sleep(0.5)
        logging.warning(f"Dashboard did not start within 20s: {display_url}")

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    if skip_browser:
        # Before heavy PolyBot() / journal replay, ensure HTTP is up so Railway
        # /health succeeds (otherwise Initializing can time out while main hogs CPU/GIL).
        if os.environ.get("PORT") or os.environ.get("RAILWAY_ENVIRONMENT"):
            _wait_until_port_accepts(timeout=95.0)
        threading.Thread(target=log_when_ready_no_browser, daemon=True).start()
    else:
        threading.Thread(target=open_when_ready, daemon=True).start()

def _parse_run_args():
    """Parse --paper, --live, --confirm-live, --emergency-stop, --resume-trading. Returns (dry_run, run_bot)."""
    argv = sys.argv[1:]
    if "--emergency-stop" in argv:
        KILL_SWITCH_FILE.parent.mkdir(parents=True, exist_ok=True)
        KILL_SWITCH_FILE.touch()
        print(
            "Kill switch enabled: data/KILL_SWITCH created. Bot will not place new trades until you run with --resume-trading."
        )
        return None, False
    if "--resume-trading" in argv:
        if KILL_SWITCH_FILE.exists():
            KILL_SWITCH_FILE.unlink()
            print("Kill switch removed. Trading can resume.")
        else:
            print("Kill switch file was not present. No change.")
        return None, False

    live = "--live" in argv
    paper = "--paper" in argv
    confirm_live = "--confirm-live" in argv

    if live and not confirm_live:
        print("Live trading requires confirmation. Run with: --live --confirm-live")
        sys.exit(1)
    if confirm_live and not live:
        print("--confirm-live has no effect without --live.")
    if live and confirm_live:
        try:
            ans = input("Type YES (exactly) to enable live trading: ").strip()
        except EOFError:
            ans = ""
        if ans != "YES":
            print("Confirmation failed. Exiting.")
            sys.exit(1)
        dry_run = False
    elif paper or not live:
        dry_run = True
    else:
        dry_run = True
    return dry_run, True


async def main():
    """Main entry point"""
    load_project_dotenv(Path(__file__).resolve().parent.parent)

    dry_run, run_bot = _parse_run_args()
    if not run_bot:
        return

    if "--backtest" in sys.argv:
        # Run the backtester (PolymarketData-based engine)
        import subprocess

        script_path = (
            Path(__file__).resolve().parent.parent / "scripts" / "run_backtest.py"
        )
        backtest_args = [a for a in sys.argv[1:] if a != "--backtest"]
        args = [sys.executable, str(script_path)] + backtest_args
        sys.exit(subprocess.run(args).returncode)

    def _bootstrap_config() -> Dict[str, Any]:
        """Lightweight settings load so the dashboard can bind before PolyBot journal I/O."""
        config_path = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
        try:
            with open(config_path, "r") as f:
                config = yaml.safe_load(f)
        except Exception as e:
            print(f"WARNING: Bootstrap could not load settings.yaml: {e}")
            return {"dashboard": {"enabled": True}, "notifications": {}}
        if not isinstance(config, dict):
            return {"dashboard": {"enabled": True}, "notifications": {}}
        notifications = config.setdefault("notifications", {})
        if not notifications.get("discord_webhook") and os.getenv("DISCORD_WEBHOOK_URL"):
            notifications["discord_webhook"] = os.getenv("DISCORD_WEBHOOK_URL")
        return config

    class _DashboardConfigShim:
        __slots__ = ("config", "_dashboard_server")

        def __init__(self, config: Dict[str, Any]):
            self.config = config
            self._dashboard_server = None

    # Bind HTTP + /health before PolyBot() (journal replay can take minutes on large sessions).
    _dash_holder = _DashboardConfigShim(_bootstrap_config())
    start_dashboard(_dash_holder)

    # Now that environment is loaded, we can initialize the bot
    bot = PolyBot()
    if dry_run is not None:
        bot.config.setdefault("trading", {})["dry_run"] = dry_run

    # Load API keys before dashboard so /api/status shows correct AI readiness (incl. dashboard-only).
    api_keys = {
        "PRIVATE_KEY": os.getenv("PRIVATE_KEY") or os.getenv("POLYMARKET_PRIVATE_KEY"),
        "POLYMARKET_API_KEY": os.getenv("POLYMARKET_API_KEY"),
        "POLYMARKET_API_SECRET": os.getenv("POLYMARKET_API_SECRET"),
        "POLYMARKET_API_PASSPHRASE": os.getenv("POLYMARKET_API_PASSPHRASE"),
        "ETHERSCAN_API_KEY": os.getenv("ETHERSCAN_API_KEY"),
        "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY"),
        "OPENROUTER_API_KEY": os.getenv("OPENROUTER_API_KEY"),
        "GOOGLE_API_KEY": os.getenv("GOOGLE_API_KEY"),
        "MINIMAX_API_KEY": os.getenv("MINIMAX_API_KEY"),
        "ANTHROPIC_API_KEY": os.getenv("ANTHROPIC_API_KEY"),
        "GROQ_API_KEY": os.getenv("GROQ_API_KEY"),
        "GOOGLE_PROJECT_ID": os.getenv("GOOGLE_PROJECT_ID"),
        "GOOGLE_LOCATION": os.getenv("GOOGLE_LOCATION"),
    }
    api_keys = {k: v for k, v in api_keys.items() if v is not None}

    _paper = bot.config.get("trading", {}).get("dry_run", True)
    if not api_keys.get("PRIVATE_KEY"):
        if _paper:
            logging.info(
                "Paper mode: PRIVATE_KEY / POLYMARKET_PRIVATE_KEY not set — OK until you enable live trading."
            )
        else:
            logging.critical(
                "CRITICAL: PRIVATE_KEY or POLYMARKET_PRIVATE_KEY is required when dry_run is false."
            )

    bot.set_api_keys(api_keys=api_keys)

    from src.ai_status import compute_ai_status, format_ai_log_line

    _ai_st = compute_ai_status(bot.config, bot.ai_agent.api_keys)
    logging.info(format_ai_log_line(_ai_st))
    if not _ai_st.get("ready"):
        logging.warning(
            "LLM calls are disabled until AI is ready — check ai.enabled, "
            "provider_chain, and secrets in .env or config/secrets.env (see AI STATUS log above)."
        )

    from src.dashboard.server import set_bot_instance, take_dashboard_uvicorn_server

    set_bot_instance(bot)
    if bot.config.get("dashboard", {}).get("enabled", False):
        srv = take_dashboard_uvicorn_server()
        if srv:
            bot._dashboard_server = srv
        else:
            logging.warning(
                "Dashboard enabled but Uvicorn server handle missing — shutdown may not stop dashboard thread."
            )

    # Dashboard-only mode: serve dashboard + backtests, no trading loop
    if "--dashboard-only" in sys.argv:
        logging.info("Dashboard-only mode — trading disabled. Run backtests from the dashboard.")
        try:
            while True:
                await asyncio.sleep(30)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        await bot.shutdown()
        return

    def signal_handler(sig, frame):
        logging.info("Received shutdown signal — cancelling tasks...")
        bot.stop()
        # Cancel all running asyncio tasks so sleeping loops wake up immediately
        for task in asyncio.all_tasks():
            task.cancel()

    signal.signal(signal.SIGINT, signal_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, signal_handler)

    try:
        await bot.start()
    except asyncio.CancelledError:
        pass
    finally:
        await bot.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
