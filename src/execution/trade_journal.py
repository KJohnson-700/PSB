"""
Paper Trade Journal
Persistent, append-only trade log with portfolio snapshots.
Every trade decision, price update, and exit is recorded to disk.
"""

import json
import logging
import os
import sys

# 2026-07-16: the --dashboard-only process reads the journal read-only. It must NOT
# write summary.json (that is the bot's file -> write contention/mid-write reads) and
# the get_summary()->entry_log_first_last rescan on every load was the dashboard's top
# CPU cost (py-spy). The live bot (no --dashboard-only) is unaffected.
_JOURNAL_READONLY = ("--dashboard-only" in sys.argv)
import shutil
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any

from ..journal_features import enrich_entry_extra, enrich_exit_extra

# 2026-07-16 JOURNAL-SLIM (operator GO, staged next-restart): diagnostic-noise events were
# ~90% of a 50MB entries.jsonl (BUY_NO_SKIP 36% + ANNOTATION 34% + PRICE_UPDATE 15% + SKIP 5%)
# and no trading path reads them back (PRICE_UPDATE has 0 consumers; BUY_NO_SKIP is redundant
# with rejected_candidates.jsonl; ANNOTATION is skipped on load). Keeping ENTRY/EXIT/SNAPSHOT/
# ERROR makes every dashboard parse ~9x cheaper. Escape hatch: set JOURNAL_LOG_ALL_EVENTS=1.
_JOURNAL_NOISE_EVENTS = frozenset({"PRICE_UPDATE", "BUY_NO_SKIP", "SKIP", "ANNOTATION"})
_JOURNAL_LOG_ALL_EVENTS = os.getenv("JOURNAL_LOG_ALL_EVENTS", "").strip().lower() in ("1", "true", "yes")

logger = logging.getLogger(__name__)

JOURNAL_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "paper_trades"
MIN_COMPLETED_SESSION_TRADES_FOR_LISTING = 50

# Append-only log of actual CLOB fill prices for updown markets.
# Used by updown_engine to replace N(0.50, 0.06) with empirical distribution.
ENTRY_PRICE_LOG = Path(__file__).resolve().parent.parent.parent / "data" / "entry_prices" / "updown_fills.jsonl"
_UPDOWN_STRATEGIES = frozenset(
    {
        "bitcoin",
        "sol_macro",
        "xrp_macro",
        "eth_macro",
        "hype_macro",
        "doge_macro",
        "bnb_macro",
    }
)


def infer_entry_leg(pos: Dict[str, Any]) -> str:
    """Whether ``entry_price`` quotes the YES or NO token (``\"YES\"`` | ``\"NO\"``).

    Prefer explicit ``entry_leg`` on journal rows; else infer from ``action`` / ``side`` /
    ``outcome`` for older sessions.
    """
    leg = pos.get("entry_leg")
    if leg in ("YES", "NO"):
        return leg
    action = (pos.get("action") or "").strip().upper()
    if action == "BUY_NO":
        return "NO"
    if action == "SELL_YES":
        return "YES"
    side = (pos.get("side") or "").upper()
    out = (pos.get("outcome") or "").upper()
    if out == "NO" and side == "BUY":
        return "NO"
    if out == "NO" and side == "SELL":
        return "YES"
    return "YES"


def _is_yes_token_flip(leg: str, entry_price: float, exit_price: float) -> bool:
    """True when a YES-leg exit looks like a token-ordering bug (exit ≈ 1 - entry).

    NEAR-EVEN EXEMPTION (2026-07-29): entries in [0.42, 0.58] are exempt. There a legit
    small-move exit (e.g. 0.50→0.49) also sums to ~1.0, so the flip signature is a false
    positive AND a real flip is ~$0 / undetectable by price alone. Only skewed entries
    carry a distinguishable, material flip signature. Explicit < / > bounds (not
    abs(ep-0.5)>0.08) to avoid float-boundary asymmetry at 0.42/0.58 (Codex 2026-07-29).
    Centralized so log_exit / is_phantom_exit_row / _build_closed_stats stay in lock-step.
    51 false blocks on 2026-07-29 (all ep 0.50–0.54, -$10.13 hidden) drove this.
    """
    return (
        leg == "YES"
        and entry_price > 0
        and (entry_price < 0.42 or entry_price > 0.58)
        and abs(entry_price + exit_price - 1.0) < 0.02
    )


def is_phantom_exit_row(row: Dict[str, Any], max_plausible_pnl: float = 200.0) -> bool:
    """Detect legacy phantom exits without dropping valid long-NO closes."""
    try:
        entry_price = float(row.get("entry_price") or 0)
        current_price = float(row.get("current_price") or 0)
        pnl = float(row.get("pnl") or 0)
    except (TypeError, ValueError):
        return False

    extra = row.get("extra") or {}
    leg_row = dict(row)
    if isinstance(extra, dict) and extra.get("entry_leg") in ("YES", "NO"):
        leg_row["entry_leg"] = extra["entry_leg"]
    leg = infer_entry_leg(leg_row)

    is_token_flip = _is_yes_token_flip(leg, entry_price, current_price)
    return is_token_flip or abs(pnl) > max_plausible_pnl


@dataclass
class JournalEntry:
    """Single trade journal entry — immutable once written."""

    timestamp: str
    event: str  # ENTRY, PRICE_UPDATE, EXIT, SNAPSHOT, SKIP, ERROR, RECONCILE_DROP
    trade_id: str
    market_id: str
    market_question: str
    strategy: str
    action: str  # BUY_YES, BUY_NO, SELL_YES, etc.
    side: str  # BUY or SELL
    outcome: str  # YES or NO
    size: float
    entry_price: float
    current_price: float
    pnl: float
    bankroll: float
    edge: float = 0.0
    confidence: float = 0.0
    reason: str = ""  # Why entered, why skipped, why exited
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PortfolioSnapshot:
    """Point-in-time portfolio state for charting."""

    timestamp: str
    bankroll: float
    total_exposure: float
    open_positions: int
    total_trades: int
    realized_pnl: float
    unrealized_pnl: float
    strategies: Dict[str, Dict[str, Any]]  # per-strategy breakdown
    # True total account equity, computed unambiguously at write time (2026-07-29). The
    # `bankroll` field is CASH in paper but venue EQUITY in live, so the equity-history
    # trace can't tell them apart from `bankroll` alone; `equity` removes the ambiguity.
    # Default 0.0 keeps old rows deserializable (they fall back to legacy trace handling).
    equity: float = 0.0


class TradeJournal:
    """Persistent trade journal with append-only log and periodic snapshots."""

    @staticmethod
    def _summary_has_activity(summary_file: Path) -> bool:
        """True when summary.json reflects real session activity."""
        if not summary_file.exists():
            return False
        try:
            with open(summary_file, encoding="utf-8", errors="replace") as f:
                data = json.load(f) or {}
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return False
        if int(data.get("total_entries", 0) or 0) > 0:
            return True
        if int(data.get("total_exits", 0) or 0) > 0:
            return True
        if int(data.get("open_positions", 0) or 0) > 0:
            return True
        if data.get("strategy_stats"):
            return True
        return False

    @staticmethod
    def session_dir_has_activity(session_dir: Path) -> bool:
        """True when a session directory contains actual trade/journal activity."""
        ent = session_dir / "entries.jsonl"
        pos = session_dir / "positions.json"
        summ = session_dir / "summary.json"
        try:
            if ent.exists() and ent.stat().st_size > 0:
                return True
        except OSError:
            pass
        if pos.exists():
            try:
                with open(pos, encoding="utf-8", errors="replace") as f:
                    data = json.load(f) or {}
                if isinstance(data, dict) and len(data) > 0:
                    return True
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                pass
        return TradeJournal._summary_has_activity(summ)

    @staticmethod
    def session_trade_count(session_dir: Path) -> int:
        """Best-effort count of fills for deciding whether a completed run is worth listing."""
        summary_file = session_dir / "summary.json"
        try:
            if summary_file.exists():
                with open(summary_file, encoding="utf-8", errors="replace") as f:
                    data = json.load(f) or {}
                total = int(data.get("total_entries", 0) or 0)
                if total > 0:
                    return total
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass

        entries_file = session_dir / "entries.jsonl"
        total = 0
        try:
            if entries_file.exists():
                with open(entries_file, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if not line.strip():
                            continue
                        try:
                            row = json.loads(line)
                        except (json.JSONDecodeError, TypeError, ValueError):
                            continue
                        if row.get("event") == "ENTRY":
                            total += 1
        except OSError:
            return 0
        return total

    @staticmethod
    def newest_resumable_session_dir(journal_dir: Optional[Path] = None) -> Optional[Path]:
        """Newest (lexicographic) session directory that has resumable journal artifacts.

        Matches ``__init__``/``resume_latest`` selection: empty stub directories
        (e.g. crash or pre-write restart) are skipped so the bot, dashboard, and
        any disk-only reader agree on which folder holds the current test run.
        """
        root = journal_dir or JOURNAL_DIR
        if not root.exists():
            return None
        existing = sorted(
            [d for d in root.iterdir() if d.is_dir()], reverse=True
        )
        for d in existing:
            if TradeJournal.session_dir_has_activity(d):
                return d
        return None

    def __init__(self, session_id: str = None, resume_latest: bool = True):
        JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
        if session_id:
            self.session_id = session_id
            # Check active dir first, then archive (flat or nested under ui_reset_* subdirs)
            archived_path = TradeJournal._find_archive_session_path(session_id)
            if (JOURNAL_DIR / session_id).exists():
                self.session_dir = JOURNAL_DIR / session_id
            elif archived_path:
                self.session_dir = archived_path
            else:
                self.session_dir = JOURNAL_DIR / session_id
                self.session_dir.mkdir(parents=True, exist_ok=True)
        elif resume_latest:
            # Resume the newest session directory that actually has journal data.
            # (Skip empty stub dirs left by crashes or aborted starts — avoids "empty
            # journal after restart" while older folders still hold trades/charts.)
            chosen = TradeJournal.newest_resumable_session_dir()
            if chosen is not None:
                self.session_id = chosen.name
                logger.info(f"Resuming existing session: {self.session_id}")
            else:
                self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.session_dir = JOURNAL_DIR / self.session_id
            self.session_dir.mkdir(parents=True, exist_ok=True)
        else:
            self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.session_dir = JOURNAL_DIR / self.session_id
            self.session_dir.mkdir(parents=True, exist_ok=True)

        self._entries_file = self.session_dir / "entries.jsonl"
        self._snapshots_file = self.session_dir / "snapshots.jsonl"
        self._positions_file = self.session_dir / "positions.json"
        self._summary_file = self.session_dir / "summary.json"

        # In-memory state (rebuilt from disk on resume)
        self.open_positions: Dict[str, Dict[str, Any]] = {}
        self.closed_trades: List[Dict[str, Any]] = []
        self.total_entries = 0
        self.total_exits = 0
        # Venue-absent positions dropped without PnL booking. Kept VISIBLE in the summary
        # (2026-07-29) so drops stop silently vanishing from total_entries/win_rate — a
        # reconcile-drop is an integrity signal (position gone from venue), not a no-op.
        self.reconcile_drops = 0
        self.reconcile_drop_strats: Dict[str, int] = {}
        self.realized_pnl = 0.0
        self._last_snapshot_time = 0.0
        self._last_summary_save_time = 0.0
        # get_summary() cache — invalidated on every ENTRY or EXIT
        self._summary_cache: Optional[Dict] = None

        # Resume from existing session
        self._load_state()
        logger.info(
            f"TradeJournal session={self.session_id} | open={len(self.open_positions)} | closed={len(self.closed_trades)}"
        )

    # ── CORE LOGGING ─────────────────────────────────────────────

    def log_entry(
        self,
        trade_id: str,
        market_id: str,
        market_question: str,
        strategy: str,
        action: str,
        side: str,
        outcome: str,
        size: float,
        entry_price: float,
        bankroll: float,
        edge: float = 0.0,
        confidence: float = 0.0,
        reason: str = "",
        extra: Dict = None,
        market_end_at: Optional[datetime] = None,
        entry_leg: Optional[str] = None,
        token_id_yes: Optional[str] = None,
        token_id_no: Optional[str] = None,
        condition_id: Optional[str] = None,
        market_slug: Optional[str] = None,
    ):
        """Log a new trade entry."""
        if isinstance(entry_leg, str) and entry_leg.strip().upper() in ("YES", "NO"):
            entry_leg_resolved = entry_leg.strip().upper()
        else:
            entry_leg_resolved = infer_entry_leg(
                {"action": action, "side": side, "outcome": outcome}
            )
        entry_leg = entry_leg_resolved
        merged_extra = enrich_entry_extra(extra, market_end_at=market_end_at)
        merged_extra["entry_leg"] = entry_leg
        window_size = str(merged_extra.get("window_size") or "")
        entry = JournalEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            event="ENTRY",
            trade_id=trade_id,
            market_id=market_id,
            market_question=market_question,
            strategy=strategy,
            action=action,
            side=side,
            outcome=outcome,
            size=size,
            entry_price=entry_price,
            current_price=entry_price,
            pnl=0.0,
            bankroll=bankroll,
            edge=edge,
            confidence=confidence,
            reason=reason,
            extra=merged_extra,
        )
        self._append_entry(entry)

        self.open_positions[trade_id] = {
            "trade_id": trade_id,
            "market_id": market_id,
            "market_question": market_question,
            "strategy": strategy,
            "action": action,
            "side": side,
            "outcome": outcome,
            "size": size,
            "entry_price": entry_price,
            "current_price": entry_price,
            "pnl": 0.0,
            "edge": edge,
            "confidence": confidence,
            "entry_reason": reason,
            "opened_at": entry.timestamp,
            "peak_token_price": entry_price,
            # Preserve full signal context so exits can reference entry conditions
            "entry_signal": merged_extra,
            "entry_leg": entry_leg,
            "window_size": window_size,
        }
        if market_end_at is not None:
            self.open_positions[trade_id]["market_end_at"] = str(market_end_at)
        _ty = (token_id_yes or "").strip()
        _tn = (token_id_no or "").strip()
        if _ty:
            self.open_positions[trade_id]["token_id_yes"] = _ty
        if _tn:
            self.open_positions[trade_id]["token_id_no"] = _tn
        _cid = (condition_id or "").strip()
        _slug = (market_slug or "").strip()
        if _cid:
            self.open_positions[trade_id]["condition_id"] = _cid
        if _slug:
            self.open_positions[trade_id]["market_slug"] = _slug
        self.total_entries = len(self.open_positions) + len(self.closed_trades)
        self._summary_cache = None  # invalidate on new entry
        self._save_positions()
        self._save_summary()
        logger.info(
            f"JOURNAL ENTRY: {strategy}/{action} {outcome} ${size:.0f} @ {entry_price:.3f} | {market_question[:50]}"
        )
        # Record actual fill price for updown strategies so backtest can use
        # the empirical distribution instead of synthetic N(0.50, 0.06).
        if strategy in _UPDOWN_STRATEGIES and 0.0 < entry_price < 1.0:
            try:
                ENTRY_PRICE_LOG.parent.mkdir(parents=True, exist_ok=True)
                yes_for_log = (1.0 - entry_price) if entry_leg == "NO" else entry_price
                with ENTRY_PRICE_LOG.open("a") as _f:
                    _f.write(
                        json.dumps(
                            {"ts": entry.timestamp, "strategy": strategy, "yes_price": yes_for_log}
                        )
                        + "\n"
                    )
            except OSError:
                pass

    def log_price_update(self, trade_id: str, current_price: float, bankroll: float):
        """Log a price update on an open position."""
        pos = self.open_positions.get(trade_id)
        if not pos:
            return

        leg = infer_entry_leg(pos)
        if leg == "NO":
            mark_no = 1.0 - current_price
            pnl = (mark_no - pos["entry_price"]) * pos["size"]
            pos["current_price"] = mark_no
            current_token_price = mark_no
        elif pos["side"] == "BUY":
            pnl = (current_price - pos["entry_price"]) * pos["size"]
            pos["current_price"] = current_price
            current_token_price = current_price
        else:
            pnl = (pos["entry_price"] - current_price) * pos["size"]
            pos["current_price"] = current_price
            current_token_price = current_price
        pos["pnl"] = round(pnl, 4)
        peak_token_price = float(pos.get("peak_token_price", pos["entry_price"]) or pos["entry_price"])
        if current_token_price > peak_token_price:
            pos["peak_token_price"] = current_token_price

        # Carry entry diagnostics forward — raw defaults (0 / "") made PRICE_UPDATE rows look like "empty" trades.
        _edge = float(pos.get("edge", 0.0) or 0.0)
        _conf = float(pos.get("confidence", 0.0) or 0.0)
        _why = pos.get("entry_reason") or ""
        entry = JournalEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            event="PRICE_UPDATE",
            trade_id=trade_id,
            market_id=pos["market_id"],
            market_question=pos["market_question"],
            strategy=pos["strategy"],
            action=pos["action"],
            side=pos["side"],
            outcome=pos["outcome"],
            size=pos["size"],
            entry_price=pos["entry_price"],
            current_price=pos["current_price"],
            pnl=pnl,
            bankroll=bankroll,
            edge=_edge,
            confidence=_conf,
            reason=f"mark_to_market|{_why}" if _why else "mark_to_market",
        )
        self._append_entry(entry)
        self._save_positions()

        # Flush summary to disk every 60 seconds so dashboard stays current
        import time as _time
        now = _time.time()
        if now - self._last_summary_save_time >= 60:
            self._save_summary()
            self._last_summary_save_time = now

    def log_exit(
        self,
        trade_id: str,
        exit_price: float,
        bankroll: float,
        reason: str = "manual",
        exit_telemetry: Optional[dict] = None,
        realized_pnl: Optional[float] = None,
    ):
        """Log a trade exit with realized PnL.

        exit_telemetry: optional exit-calibration fields (mae_pct, mfe_pct,
        pnl_pct_at_exit, effective_stop_loss_pct) recorded onto the exit row so the
        stop can be tuned from data — see src/analysis/taken_exit_settler.py.
        """
        pos = self.open_positions.get(trade_id)
        if not pos:
            logger.warning(f"Cannot exit unknown trade: {trade_id}")
            return

        # GROSS price-delta PnL — used only for the phantom guards below (token-flip /
        # oversized price-bug detection), NOT necessarily what gets booked (see truth fix).
        if pos["side"] == "BUY":
            gross_pnl = (exit_price - pos["entry_price"]) * pos["size"]
        else:
            gross_pnl = (pos["entry_price"] - exit_price) * pos["size"]

        # Phantom exit guard: token-ordering bug (YES exit price logged against YES entry)
        # produces exit_price ≈ 1 - entry_price. Only apply in YES-quote coordinates;
        # long-NO legs often have entry_no + exit_no ≈ 1 legitimately. Near-even entries
        # are exempt — see _is_yes_token_flip (shared with the summary/display paths so a
        # real near-even exit is never written here yet hidden by stats). Runs on GROSS
        # price delta on purpose — it detects price bugs, not fee/fill drift.
        _ep = pos["entry_price"]
        leg = infer_entry_leg(pos)
        _is_token_flip = _is_yes_token_flip(leg, _ep, exit_price)
        _is_oversized = abs(gross_pnl) > 200.0
        if _is_token_flip or _is_oversized:
            logger.warning(
                f"PHANTOM EXIT blocked: {pos['strategy']} ep={_ep:.4f} exit={exit_price:.4f} pnl={gross_pnl:+.2f} | {pos['market_question'][:50]}"
            )
            return

        # 2026-07-30 PnL TRUTH FIX (Codex debug sweep): book the SAME realized cash delta
        # that main.py already applied to bankroll / exposure / kelly (ExitDecision.
        # unrealized_pnl, passed as realized_pnl) instead of a mark-based price recompute.
        # Root cause of the journal-vs-bankroll gap: log_exit recomputed pnl from the MARK
        # exit_price + ENTRY size, ignoring the actual fill economics the ledger used
        # (live: journal -3.65 vs bankroll credit -2.43; session -7.63 vs -14.11). The
        # realized value already nets fees, so this path does NOT re-subtract fill_fee.
        if realized_pnl is not None:
            pnl = float(realized_pnl)
        else:
            # Paper / no realized economics available: fall back to the gross price
            # recompute, net of the taker fee (prior behaviour, byte-identical).
            pnl = gross_pnl
            _fill_fee = (exit_telemetry or {}).get("fill_fee_usdc")
            if isinstance(_fill_fee, (int, float)) and _fill_fee > 0:
                pnl -= float(_fill_fee)

        # Build exit extra: carry entry signal context + append outcome analysis
        # so every closed trade (win or loss) has full context for pattern learning.
        entry_signal = pos.get("entry_signal", {})
        outcome_won = None
        if "RESOLVED:" in reason:
            # reason format: "RESOLVED:YES (real)" or "RESOLVED:NO (real)"
            try:
                outcome_won = reason.split("RESOLVED:")[1].split(" ")[0].upper()
            except (IndexError, AttributeError):
                pass
        exit_extra = {
            **entry_signal,
            "outcome_won": outcome_won,
            "result": "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "PUSH"),
            "exit_reason": reason,
            "entry_edge": pos.get("edge"),
            "entry_confidence": pos.get("confidence"),
        }
        exit_extra = enrich_exit_extra(exit_extra, pos.get("opened_at"))
        if exit_telemetry:
            # Only stamp keys that carry a value so legacy/reload exits stay clean.
            for _k, _v in exit_telemetry.items():
                if _v is not None:
                    exit_extra[_k] = _v

        entry = JournalEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            event="EXIT",
            trade_id=trade_id,
            market_id=pos["market_id"],
            market_question=pos["market_question"],
            strategy=pos["strategy"],
            action=pos["action"],
            side=pos["side"],
            outcome=pos["outcome"],
            size=pos["size"],
            entry_price=pos["entry_price"],
            current_price=exit_price,
            pnl=round(pnl, 4),
            bankroll=bankroll,
            reason=reason,
            extra=exit_extra,
        )
        self._append_entry(entry)

        # Move to closed
        pos["closed_at"] = entry.timestamp
        pos["exit_price"] = exit_price
        pos["pnl"] = round(pnl, 4)
        pos["exit_reason"] = reason
        if exit_telemetry:
            # Carry onto the closed-trade dict so it lands in the calibration row
            # (trades.jsonl) via build_record_from_closed_trade, not just entries.jsonl.
            for _k, _v in exit_telemetry.items():
                if _v is not None:
                    pos[_k] = _v
        # PAPER CALIB Phase 2.5: lift the ENTRY executability proof onto the closed trade
        # so the lane-fillability analyzer is single-source (trades.jsonl) — no fragile
        # entries.jsonl join. None on live / non-fresh-fill entries.
        _entry_sig = pos.get("entry_signal")
        if isinstance(_entry_sig, dict):
            _efq = _entry_sig.get("paper_fill_quality")
            if isinstance(_efq, dict):
                pos["entry_paper_fill_quality"] = _efq
            # 2026-08-14 Same lift for the MAKER-vs-TAKER execution path, stamped onto
            # Order.execution by clob_client._lc() and carried in via log_entry(extra=).
            # Deliberately separate from paper_fill_quality: that block is None on LIVE
            # (it is the paper sim-fill record), and this field must survive a LIVE run —
            # it is the only entry-execution fact that does. See calibration_log for why
            # (fill_fee_rate is a flat 0.07 taker, but ~50% of live entries fill as maker).
            _eex = _entry_sig.get("entry_execution")
            if isinstance(_eex, dict):
                pos["entry_execution"] = _eex
        self.closed_trades.append(pos)
        del self.open_positions[trade_id]
        self.total_entries = len(self.open_positions) + len(self.closed_trades)
        self.total_exits += 1
        self.realized_pnl += pnl
        self._summary_cache = None  # invalidate on exit
        self._save_positions()
        self._save_summary()
        logger.info(
            f"JOURNAL EXIT: {pos['strategy']}/{pos['action']} PnL=${pnl:+.2f} | reason={reason} | {pos['market_question'][:50]}"
        )

    def log_reconcile_drop(
        self,
        trade_id: str,
        bankroll: float,
        reason: str = "venue_absent_reconcile_drop",
        extra: Optional[dict] = None,
    ) -> bool:
        """Drop a venue-absent open position without booking realized PnL."""
        pos = self.open_positions.get(trade_id)
        if not pos:
            logger.warning(f"Cannot reconcile-drop unknown trade: {trade_id}")
            return False

        entry_signal = pos.get("entry_signal", {})
        drop_extra = {
            **entry_signal,
            "reconcile_action": "drop_open_position_without_exit",
            "entry_edge": pos.get("edge"),
            "entry_confidence": pos.get("confidence"),
        }
        if extra:
            drop_extra.update(extra)

        entry = JournalEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            event="RECONCILE_DROP",
            trade_id=trade_id,
            market_id=pos["market_id"],
            market_question=pos["market_question"],
            strategy=pos["strategy"],
            action=pos["action"],
            side=pos["side"],
            outcome=pos["outcome"],
            size=pos["size"],
            entry_price=pos["entry_price"],
            current_price=pos.get("current_price", pos["entry_price"]),
            pnl=0.0,
            bankroll=bankroll,
            edge=pos.get("edge") or 0.0,
            confidence=pos.get("confidence") or 0.0,
            reason=reason,
            extra=drop_extra,
        )
        self._append_entry(entry)
        del self.open_positions[trade_id]
        self.reconcile_drops += 1
        _rd_strat = pos.get("strategy") or "?"
        self.reconcile_drop_strats[_rd_strat] = self.reconcile_drop_strats.get(_rd_strat, 0) + 1
        self.total_entries = len(self.open_positions) + len(self.closed_trades)
        self._summary_cache = None
        self._save_positions()
        self._save_summary()
        logger.info(
            "JOURNAL RECONCILE_DROP: %s/%s removed without PnL booking | reason=%s | %s",
            pos["strategy"],
            pos["action"],
            reason,
            pos["market_question"][:50],
        )
        return True

    def log_skip(
        self,
        market_id: str,
        market_question: str,
        strategy: str,
        reason: str,
        bankroll: float,
        extra: Optional[Dict[str, Any]] = None,
    ):
        """Log a trade that was considered but skipped (risk check, etc.)."""
        entry = JournalEntry(
            timestamp=datetime.now().isoformat(),
            event="SKIP",
            trade_id="",
            market_id=market_id,
            market_question=market_question,
            strategy=strategy,
            action="",
            side="",
            outcome="",
            size=0,
            entry_price=0,
            current_price=0,
            pnl=0,
            bankroll=bankroll,
            reason=reason,
            extra=dict(extra or {}),
        )
        self._append_entry(entry)

    def log_buy_no_skip(
        self,
        *,
        market_id: str,
        market_question: str,
        strategy: str,
        bankroll: float,
        skip_reason: str,
        window_size: str,
        yes_price: float,
        edge: float,
        effective_min_edge: float,
        rsi: float,
        htf_bias: str,
        signal_reason: str,
        alt_1h_trend: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Persist a post-side-selection BUY_NO suppression event."""
        payload: Dict[str, Any] = {
            "strategy": strategy,
            "market_id": market_id,
            "window_size": window_size,
            "skip_reason": skip_reason,
            "yes_price": float(yes_price),
            "edge": float(edge),
            "effective_min_edge": float(effective_min_edge),
            "rsi": float(rsi),
            "htf_bias": htf_bias,
            "signal_reason": signal_reason,
        }
        if alt_1h_trend:
            payload["alt_1h_trend"] = alt_1h_trend
        if extra:
            payload.update(extra)
        entry = JournalEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            event="BUY_NO_SKIP",
            trade_id="",
            market_id=market_id,
            market_question=market_question,
            strategy=strategy,
            action="BUY_NO",
            side="",
            outcome="NO",
            size=0,
            entry_price=max(0.0, min(1.0, 1.0 - float(yes_price))),
            current_price=max(0.0, min(1.0, 1.0 - float(yes_price))),
            pnl=0,
            bankroll=bankroll,
            edge=float(edge),
            confidence=0.0,
            reason=skip_reason,
            extra=payload,
        )
        self._append_entry(entry)

    # ── SNAPSHOTS ─────────────────────────────────────────────────

    def take_snapshot(self, bankroll: float):
        """Take a point-in-time portfolio snapshot (call every cycle)."""
        now = time.time()
        # Limit to once per 30 seconds
        if now - self._last_snapshot_time < 30:
            return
        self._last_snapshot_time = now

        unrealized = sum(p.get("pnl", 0) for p in self.open_positions.values())
        exposure = sum(p["size"] for p in self.open_positions.values())

        # Per-strategy breakdown
        strats = {}
        for p in self.open_positions.values():
            s = p["strategy"]
            if s not in strats:
                strats[s] = {"open": 0, "exposure": 0, "unrealized_pnl": 0}
            strats[s]["open"] += 1
            strats[s]["exposure"] += p["size"]
            strats[s]["unrealized_pnl"] += p.get("pnl", 0)
        for ct in self.closed_trades:
            s = ct["strategy"]
            if s not in strats:
                strats[s] = {"open": 0, "exposure": 0, "unrealized_pnl": 0}
            strats[s].setdefault("closed", 0)
            strats[s]["closed"] = strats[s].get("closed", 0) + 1
            strats[s].setdefault("realized_pnl", 0)
            strats[s]["realized_pnl"] = strats[s].get("realized_pnl", 0) + ct.get(
                "pnl", 0
            )

        # Equity = true account value, disambiguating the paper/live `bankroll` meaning.
        # In live, _live_pnl_override is set (bankroll is already venue equity), so equity
        # = bankroll. In paper, bankroll is cash, so equity = cash + unrealized. Same
        # invariant the summary/ops/server split uses (bankroll_source=="live_wallet").
        _is_live_equity = getattr(self, "_live_pnl_override", None) is not None
        _equity = round(float(bankroll) if _is_live_equity else float(bankroll) + unrealized, 4)
        snap = PortfolioSnapshot(
            timestamp=datetime.now().isoformat(),
            bankroll=bankroll,
            total_exposure=exposure,
            open_positions=len(self.open_positions),
            total_trades=self.total_entries,
            realized_pnl=round(self.realized_pnl, 4),
            unrealized_pnl=round(unrealized, 4),
            strategies=strats,
            equity=_equity,
        )
        self._append_snapshot(snap)

    # ── QUERIES ───────────────────────────────────────────────────

    def get_open_positions(self) -> List[Dict]:
        return list(self.open_positions.values())

    def get_closed_trades(self) -> List[Dict]:
        return self.closed_trades

    def get_all_entries(self, limit: int = 200) -> List[Dict]:
        """Read last N entries from the JSONL log."""
        if not self._entries_file.exists():
            return []
        entries = []
        with open(self._entries_file, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
        return entries[-limit:]

    def get_snapshots(self, limit: int = 500) -> List[Dict]:
        """Read portfolio snapshots for charting."""
        if not self._snapshots_file.exists():
            return []
        snaps = []
        with open(self._snapshots_file, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    snaps.append(json.loads(line))
        return snaps[-limit:]

    def last_bankroll_from_entries_log(self, tail_bytes: int = 2_000_000) -> Optional[float]:
        """Last real trade ``bankroll`` field in entries.jsonl (tail scan for large logs)."""
        if not self._entries_file.exists():
            return None
        try:
            with open(self._entries_file, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - tail_bytes))
                chunk = f.read().decode("utf-8", errors="replace")
        except OSError:
            return None
        last_br: Optional[float] = None
        for line in chunk.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Diagnostic annotations are logged with bankroll=0.0 by design. They are
            # not portfolio snapshots and must not hide the last real trade bankroll.
            if str(e.get("event") or "").upper() == "ANNOTATION":
                continue
            b = e.get("bankroll")
            if b is None:
                continue
            try:
                last_br = float(b)
            except (TypeError, ValueError):
                pass
        return last_br

    def _build_closed_stats(self) -> Dict:
        """Compute closed-trade stats from self.closed_trades. Called once per ENTRY/EXIT,
        cached in self._summary_cache between events."""
        real_trades = []
        for ct in self.closed_trades:
            ep = float(ct.get("entry_price") or 0)
            xv = ct.get("exit_price", ct.get("current_price", 0))
            try:
                xv = float(xv or 0)
            except (TypeError, ValueError):
                xv = 0.0
            pnl = float(ct.get("pnl") or 0)
            leg = infer_entry_leg(ct)
            is_yes_flip = _is_yes_token_flip(leg, ep, xv)
            oversized = abs(pnl) > 200.0
            if not is_yes_flip and not oversized:
                real_trades.append(ct)
        wins = sum(1 for ct in real_trades if ct.get("pnl", 0) > 0)
        losses = sum(1 for ct in real_trades if ct.get("pnl", 0) <= 0)
        strat_stats: Dict = {}
        real_pnl = 0.0
        for ct in real_trades:
            s = ct["strategy"]
            if s not in strat_stats:
                strat_stats[s] = {"trades": 0, "wins": 0, "pnl": 0, "avg_pnl": 0}
            strat_stats[s]["trades"] += 1
            strat_stats[s]["pnl"] += ct.get("pnl", 0)
            real_pnl += ct.get("pnl", 0)
            if ct.get("pnl", 0) > 0:
                strat_stats[s]["wins"] += 1
        for s in strat_stats.values():
            s["win_rate"] = round(s["wins"] / s["trades"], 3) if s["trades"] else 0
            s["avg_pnl"] = round(s["pnl"] / s["trades"], 2) if s["trades"] else 0
            s["pnl"] = round(s["pnl"], 2)

        def _notional(d: Dict) -> float:
            try:
                return float(d.get("size", 0) or 0) * float(d.get("entry_price", 0) or 0)
            except (TypeError, ValueError):
                return 0.0

        return {
            "total_exits": len(real_trades),
            "realized_pnl": round(real_pnl, 2),
            "win_rate_parts": (wins, losses),
            "strategy_stats": strat_stats,
            "closed_notional": round(sum(_notional(ct) for ct in real_trades), 2),
        }

    def get_summary(self) -> Dict:
        """Get current session summary.

        Closed-trade stats are cached between ENTRY/EXIT events (they can only
        change then). Open-position stats (unrealized, total_cost) are always
        recomputed since they change on price updates.
        """
        if self._summary_cache is None:
            self._summary_cache = self._build_closed_stats()
        closed = self._summary_cache

        wins, losses = closed["win_rate_parts"]
        unrealized = sum(p.get("pnl", 0) for p in self.open_positions.values())
        total_cost = sum(
            p.get("size", 0) * p.get("entry_price", 0)
            for p in self.open_positions.values()
        )

        def _notional(d: Dict) -> float:
            try:
                return float(d.get("size", 0) or 0) * float(d.get("entry_price", 0) or 0)
            except (TypeError, ValueError):
                return 0.0

        session_staked_notional = round(
            sum(_notional(p) for p in self.open_positions.values()) + closed["closed_notional"],
            2,
        )
        # Fills / closed totals must match journal state (same as log_entry assigns:
        # open + len(closed_trades)), not len(real_trades). The phantom heuristic in
        # _build_closed_stats mirrors pre-fix buggy EXIT rows across all legs while
        # log_exit blocks it only for YES-quote rows — excluding real closes from stats
        # under-counted fills on the dashboard.
        n_closed = len(self.closed_trades)
        entry_fill_count = len(self.open_positions) + n_closed
        realized_raw = float(closed["realized_pnl"])
        unreal_rounded = round(unrealized, 2)
        out = {
            "session_id": self.session_id,
            "total_entries": entry_fill_count,
            "total_exits": n_closed,
            "open_positions": len(self.open_positions),
            "total_cost": round(total_cost, 2),
            "session_staked_notional": session_staked_notional,
            "realized_pnl": closed["realized_pnl"],
            "unrealized_pnl": unreal_rounded,
            "total_pnl": round(realized_raw + unreal_rounded, 2),
            "win_rate": round(wins / (wins + losses), 3) if (wins + losses) > 0 else 0,
            "wins": wins,
            "losses": losses,
            "reconcile_drops": self.reconcile_drops,
            "reconcile_drop_strats": dict(self.reconcile_drop_strats),
            "strategy_stats": closed["strategy_stats"],
        }
        # Live runs: the journal only sees trades IT recorded — it misses manual
        # trades, on-chain resolutions, and the broker's hidden fee — so journal P&L
        # diverges from the real account. When the bot sets _live_pnl_override (=
        # current venue equity − run-start anchor) it becomes the source of truth for
        # P&L across every display (dashboard + ops pulse both read this summary).
        ov = getattr(self, "_live_pnl_override", None)
        if ov is not None:
            try:
                ov = round(float(ov), 2)
                # ov = venue equity − run-start anchor = TOTAL P&L (source of truth).
                # 2026-07-29 FIX: previously this set realized_pnl=ov and unrealized_pnl=0.0,
                # folding open-position mark-to-market INTO realized — so the headline
                # realized number swung every tick an open position moved (operator's
                # long-standing Command Center complaint: e.g. +7.13→+4.76 with NO trade
                # closed, purely the open BTC marking down). Split it: total stays equity-
                # truth, open marks go under unrealized, realized = equity delta − open
                # marks so realized only changes when a trade actually CLOSES.
                # unreal_rounded is the journal's live open-position pnl (positions carry a
                # maintained mark), consistent basis with ov's open-position component.
                out["total_pnl"] = ov
                out["unrealized_pnl"] = unreal_rounded
                out["realized_pnl"] = round(ov - unreal_rounded, 2)
                # 2026-07-29 accounting reconciliation (Codex live readout: headline,
                # strategy, and wallet PnL used different sources of truth and diverged
                # silently — e.g. summary +0.61 vs strategy_stats -3.13 vs wallet -4.10).
                # Surface BOTH sources + the gap so the divergence is VISIBLE, not hidden:
                #   equity_realized  = wallet truth (ov − open marks) — the authoritative #.
                #   journal_realized = sum of per-strategy journal exits (= strategy_stats).
                #   accounting_gap   = equity_realized − journal_realized = unattributed
                #                      execution cost (broker fee, slippage, manual/on-chain
                #                      settle) the per-lane journal can't see. strategy_stats
                #                      stays journal-truth (best per-lane attribution); the
                #                      gap is reported alongside rather than smeared into it.
                out["equity_realized"] = out["realized_pnl"]
                out["journal_realized"] = round(realized_raw, 2)
                out["accounting_gap"] = round(out["realized_pnl"] - realized_raw, 2)
            except (TypeError, ValueError):
                pass
        src = (
            "archived"
            if "paper_trades_archive" in str(self.session_dir.resolve())
            else "active"
        )
        out.update(self.session_time_meta_for_dir(self.session_dir, self.session_id, src))
        return out

    @staticmethod
    def inferred_start_iso(session_id: str) -> Optional[str]:
        """Parse ``YYYYMMDD_HHMMSS`` folder id into ISO-like local timestamp string."""
        try:
            dt = datetime.strptime(session_id, "%Y%m%d_%H%M%S")
            return dt.isoformat()
        except ValueError:
            return None

    @staticmethod
    def entry_log_first_last(entries_file: Path) -> tuple[Optional[str], Optional[str]]:
        """First and last ``timestamp`` values in entries.jsonl (any event)."""
        # 2026-07-16 BOUNDED: only the FIRST and LAST timestamps are needed, so read the
        # head and a tail chunk instead of json.loads-scanning the whole (50MB+) file. Same
        # output; was the dashboard's #1 CPU leaf (full rescan on every _save_summary).
        first: Optional[str] = None
        last: Optional[str] = None
        def _ts(raw: bytes) -> Optional[str]:
            try:
                v = json.loads(raw).get("timestamp")
                return str(v) if v else None
            except Exception:
                return None
        try:
            with open(entries_file, "rb") as f:
                # first non-empty line with a timestamp (scan a few lines from the head)
                for _i, raw in enumerate(f):
                    raw = raw.strip()
                    if raw:
                        first = _ts(raw)
                        if first is not None or _i > 50:
                            break
                # last non-empty line with a timestamp (tail chunk only)
                f.seek(0, 2)
                size = f.tell()
                chunk = min(size, 262144)
                f.seek(size - chunk)
                tail_lines = [ln for ln in f.read().splitlines() if ln.strip()]
                for raw in reversed(tail_lines):
                    last = _ts(raw.strip())
                    if last is not None:
                        break
        except OSError:
            return None, None
        return first, last

    @staticmethod
    def session_time_meta_for_dir(
        session_dir: Path, session_id: str, source: str
    ) -> Dict[str, Optional[str]]:
        """Human-facing bounds for a test run (folder + journal log)."""
        first, last = TradeJournal.entry_log_first_last(session_dir / "entries.jsonl")
        started = first or TradeJournal.inferred_start_iso(session_id)
        archived = source == "archived"
        return {
            "started_at": started,
            "ended_at": last if archived else None,
            "last_activity_at": last,
        }

    @staticmethod
    def _find_archive_session_path(session_id: str) -> Optional[Path]:
        """Find a session dir inside paper_trades_archive (flat or nested under ui_reset_* subdirs)."""
        ARCHIVE_DIR = JOURNAL_DIR.parent / "paper_trades_archive"
        if not ARCHIVE_DIR.exists():
            return None
        flat = ARCHIVE_DIR / session_id
        if flat.is_dir():
            return flat
        for sub in ARCHIVE_DIR.iterdir():
            if sub.is_dir() and (sub / session_id).is_dir():
                return sub / session_id
        return None

    @staticmethod
    def _iter_session_dirs(base_dir: Path, source: str):
        """Yield (session_dir, source) for all session dirs, recursing one level into non-session subdirs."""
        for d in sorted(base_dir.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            if TradeJournal.session_dir_has_activity(d):
                yield d, source
            elif source == "archived":
                # Recurse one level into batch-archive subdirs (e.g. ui_reset_ts/)
                for sub in sorted(d.iterdir(), reverse=True):
                    if sub.is_dir() and TradeJournal.session_dir_has_activity(sub):
                        yield sub, source

    @staticmethod
    def list_sessions(
        min_completed_trades: int = MIN_COMPLETED_SESSION_TRADES_FOR_LISTING,
        include_short_current: bool = True,
    ) -> List[Dict]:
        """List paper sessions, hiding short completed runs by default.

        The active/current session remains visible while it is still accumulating fills;
        completed sessions need ``min_completed_trades`` fills to avoid noisy aborted runs.
        """
        ARCHIVE_DIR = JOURNAL_DIR.parent / "paper_trades_archive"
        current_dir = TradeJournal.newest_resumable_session_dir()
        current_session_id = current_dir.name if current_dir else None
        search_dirs = []
        if JOURNAL_DIR.exists():
            search_dirs.append((JOURNAL_DIR, "active"))
        if ARCHIVE_DIR.exists():
            search_dirs.append((ARCHIVE_DIR, "archived"))

        sessions = []
        seen = set()
        for base_dir, source in search_dirs:
            for d, src in TradeJournal._iter_session_dirs(base_dir, source):
                if d.name in seen:
                    continue
                seen.add(d.name)
                is_current = src == "active" and d.name == current_session_id
                trade_count = TradeJournal.session_trade_count(d)
                if (
                    min_completed_trades > 0
                    and (not is_current or not include_short_current)
                    and trade_count < min_completed_trades
                ):
                    continue
                summary_file = d / "summary.json"
                if summary_file.exists():
                    try:
                        with open(summary_file) as f:
                            data = json.load(f)
                            data["_source"] = src
                            data["_is_current"] = is_current
                            data["_status"] = (
                                "active" if is_current else "completed" if src == "active" else "archived"
                            )
                            data["_path"] = str(d)
                            data["_trade_count_for_listing"] = trade_count
                            data.update(
                                TradeJournal.session_time_meta_for_dir(
                                    d, d.name, src
                                )
                            )
                            if not is_current and not data.get("ended_at"):
                                data["ended_at"] = data.get("last_activity_at")
                            # Apply phantom filter to realized_pnl for display
                            entries_file = d / "entries.jsonl"
                            if entries_file.exists():
                                real_pnl = 0.0
                                real_wins = 0
                                real_trades = 0
                                try:
                                    with open(entries_file) as ef:
                                        for line in ef:
                                            line = line.strip()
                                            if not line:
                                                continue
                                            e = json.loads(line)
                                            if e.get("event") != "EXIT":
                                                continue
                                            pnl = e.get("pnl", 0) or 0
                                            if is_phantom_exit_row(e):
                                                continue
                                            real_pnl += pnl
                                            real_trades += 1
                                            if pnl > 0:
                                                real_wins += 1
                                    data["realized_pnl"] = round(real_pnl, 2)
                                    ur = round(float(data.get("unrealized_pnl", 0) or 0), 2)
                                    data["unrealized_pnl"] = ur
                                    data["total_pnl"] = round(real_pnl + ur, 2)
                                    data["wins"] = real_wins
                                    data["losses"] = real_trades - real_wins
                                    data["win_rate"] = round(real_wins / real_trades, 3) if real_trades else 0
                                    open_n = int(data.get("open_positions", 0) or 0)
                                    data["total_entries"] = open_n + real_trades
                                except Exception:
                                    pass
                            sessions.append(data)
                    except Exception:
                        sessions.append(
                            {
                                "session_id": d.name,
                                "_source": src,
                                "_is_current": is_current,
                                "_status": (
                                    "active" if is_current else "completed" if src == "active" else "archived"
                                ),
                                "_path": str(d),
                                "_trade_count_for_listing": trade_count,
                                **TradeJournal.session_time_meta_for_dir(
                                    d, d.name, src
                                ),
                            }
                        )
                else:
                    time_meta = TradeJournal.session_time_meta_for_dir(d, d.name, src)
                    if not is_current and not time_meta.get("ended_at"):
                        time_meta["ended_at"] = time_meta.get("last_activity_at")
                    sessions.append(
                        {
                            "session_id": d.name,
                            "_source": src,
                            "_is_current": is_current,
                            "_status": (
                                "active" if is_current else "completed" if src == "active" else "archived"
                            ),
                            "_path": str(d),
                            "_trade_count_for_listing": trade_count,
                            **time_meta,
                        }
                    )

        # Sort by session_id descending (newest first)
        sessions.sort(key=lambda s: s.get("session_id", ""), reverse=True)
        return sessions

    @staticmethod
    def prune_short_completed_sessions(
        min_completed_trades: int = MIN_COMPLETED_SESSION_TRADES_FOR_LISTING,
        execute: bool = False,
    ) -> Dict[str, Any]:
        """Delete completed active/archive session dirs below the listing threshold.

        Dry-run by default. The current active session is never selected.
        """
        ARCHIVE_DIR = JOURNAL_DIR.parent / "paper_trades_archive"
        current_dir = TradeJournal.newest_resumable_session_dir()
        current_session_id = current_dir.name if current_dir else None
        search_dirs = []
        if JOURNAL_DIR.exists():
            search_dirs.append((JOURNAL_DIR, "active"))
        if ARCHIVE_DIR.exists():
            search_dirs.append((ARCHIVE_DIR, "archived"))

        candidates: List[Dict[str, Any]] = []
        removed = 0
        for base_dir, source in search_dirs:
            for d, src in TradeJournal._iter_session_dirs(base_dir, source):
                is_current = src == "active" and d.name == current_session_id
                if is_current:
                    continue
                trade_count = TradeJournal.session_trade_count(d)
                if trade_count >= min_completed_trades:
                    continue
                candidates.append(
                    {
                        "session_id": d.name,
                        "source": src,
                        "trades": trade_count,
                        "path": str(d),
                    }
                )
                if execute:
                    shutil.rmtree(d)
                    removed += 1
        return {
            "execute": bool(execute),
            "min_completed_trades": min_completed_trades,
            "candidates": candidates,
            "removed": removed,
        }

    # ── INTERNAL ──────────────────────────────────────────────────

    def _append_entry(self, entry: JournalEntry):
        # JOURNAL-SLIM: drop diagnostic-noise events (see _JOURNAL_NOISE_EVENTS note at top).
        if not _JOURNAL_LOG_ALL_EVENTS and str(getattr(entry, "event", "") or "").upper() in _JOURNAL_NOISE_EVENTS:
            return
        with open(self._entries_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(entry), default=str) + "\n")

    def append_annotation(
        self,
        trade_id: str,
        text: str,
        strategy: str = "",
        market_id: str = "",
        market_question: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Append a free-text annotation for an existing trade.

        Pure side-channel — does NOT mutate open_positions or closed_trades.
        Used by the post-trade AI annotator and correlation warning. Annotation
        events are skipped by ``_load_state`` because that pass only consumes
        ENTRY/EXIT events.
        """
        if not text:
            return
        merged_extra: Dict[str, Any] = {"text": str(text)}
        if extra:
            merged_extra.update(extra)
        annotation = JournalEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            event="ANNOTATION",
            trade_id=trade_id,
            market_id=market_id,
            market_question=market_question,
            strategy=strategy,
            action="",
            side="",
            outcome="",
            size=0.0,
            entry_price=0.0,
            current_price=0.0,
            pnl=0.0,
            bankroll=0.0,
            extra=merged_extra,
        )
        self._append_entry(annotation)

    def _append_snapshot(self, snap: PortfolioSnapshot):
        with open(self._snapshots_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(snap), default=str) + "\n")

    def _save_positions(self):
        with open(self._positions_file, "w", encoding="utf-8") as f:
            json.dump(self.open_positions, f, indent=2, default=str)

    def _save_summary(self):
        # Atomic write so readers never see an empty summary.json. Compute BEFORE
        # truncating, write a temp file, then atomic os.replace. Pure I/O; no behavior
        # change. (2026-07-15 STAGED, Codex GO -- takes effect at NEXT RESTART; the
        # trade_journal instance is not in the code-hot-reload module list.)
        import os as _os
        data = self.get_summary()
        _p = str(self._summary_file)
        _tmp = _p + ".tmp"
        with open(_tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        _os.replace(_tmp, _p)

    def _load_state(self):
        """Resume from disk if session exists."""
        if self._positions_file.exists():
            try:
                with open(self._positions_file) as f:
                    self.open_positions = json.load(f)
            except Exception:
                self.open_positions = {}

        # Rebuild closed trades and counters from entries log
        if self._entries_file.exists():
            # First pass: ENTRY timestamps by trade_id (EXIT replay alone omits opened_at).
            entry_opened_at: Dict[str, str] = {}
            try:
                with open(self._entries_file, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            e = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if e.get("event") == "ENTRY":
                            tid = e.get("trade_id")
                            if tid:
                                entry_opened_at[tid] = str(e.get("timestamp") or "")
            except OSError:
                entry_opened_at = {}

            exits_count = 0
            rpnl = 0.0
            closed = []
            _exited_ids: set[str] = set()
            _recon_drops = 0
            _recon_strats: Dict[str, int] = {}
            _recon_ids: set[str] = set()
            # Phantom exits from the pre-fix token-ordering bug produced PnL of
            # -$26 to -$466 per record on $3-$5 positions.  Cap at $200 to exclude
            # them from the summary so the dashboard shows accurate numbers even
            # when resuming a session that was running on the old code.
            _MAX_PLAUSIBLE_PNL = 200.0
            with open(self._entries_file, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if e.get("event") == "EXIT":
                        pnl = e.get("pnl", 0) or 0
                        ep  = e.get("entry_price", 0) or 0
                        cp  = e.get("current_price", 0) or 0
                        tid = e.get("trade_id")
                        # Phantom-exit detection: token flips are only phantom in
                        # YES-quote coordinates; long-NO exits can legitimately
                        # satisfy entry_price + current_price ~= 1.0.
                        if is_phantom_exit_row(e, _MAX_PLAUSIBLE_PNL):
                            logger.debug(
                                f"_load_state: skipping phantom EXIT "
                                f"pnl={pnl:+.2f} ep={ep} cp={cp} "
                                f"market={e.get('market_id','?')} "
                                f"strategy={e.get('strategy','?')}"
                            )
                            continue  # exclude from closed_trades, rpnl, and exit count
                        _tid = tid or ""
                        if _tid in _exited_ids:
                            continue  # skip duplicate EXIT for same trade_id
                        _exited_ids.add(_tid)
                        exits_count += 1
                        rpnl += pnl
                        closed.append(
                            {
                                "trade_id": tid,
                                "market_id": e.get("market_id"),
                                "market_question": e.get("market_question"),
                                "strategy": e.get("strategy"),
                                "action": e.get("action"),
                                "side": e.get("side"),
                                "outcome": e.get("outcome"),
                                "size": e.get("size"),
                                "entry_price": e.get("entry_price"),
                                "exit_price": e.get("current_price"),
                                "current_price": e.get("current_price"),
                                "pnl": e.get("pnl"),
                                "edge": e.get("edge", 0.0),
                                "opened_at": entry_opened_at.get(tid, ""),
                                "closed_at": e.get("timestamp"),
                                "exit_reason": e.get("reason", ""),
                                "extra": e.get("extra", {}),  # preserve signal features for coach
                            }
                        )
                    elif e.get("event") == "RECONCILE_DROP":
                        _recon_drops += 1
                        _rs = e.get("strategy") or "?"
                        _recon_strats[_rs] = _recon_strats.get(_rs, 0) + 1
                        _rid = e.get("trade_id")
                        if _rid:
                            _recon_ids.add(_rid)
            self.total_exits = exits_count
            self.realized_pnl = rpnl
            self.closed_trades = closed
            self.reconcile_drops = _recon_drops
            self.reconcile_drop_strats = _recon_strats

            # Cross-reference: remove any positions.json entries that already have
            # an EXIT or RECONCILE_DROP event in entries.jsonl.  This prevents
            # re-settlement / re-drop after a crash that left positions.json stale.
            # (RECONCILE_DROP ids included 2026-07-29: a crash between _append_entry and
            # _save_positions would otherwise resurrect the position and drop it again =
            # double-count of reconcile_drops — Codex must-fix.)
            exited_ids = {ct["trade_id"] for ct in closed if ct.get("trade_id")}
            stale = [tid for tid in (exited_ids | _recon_ids) if tid in self.open_positions]
            if stale:
                logger.warning(
                    f"Removing {len(stale)} stale open-position(s) that already have EXIT/RECONCILE_DROP events: {stale}"
                )
                for tid in stale:
                    del self.open_positions[tid]
                self._save_positions()  # Write corrected state back to disk

            self.total_entries = len(self.open_positions) + len(self.closed_trades)
        else:
            self.total_entries = len(self.open_positions)

        # Only flush a summary when the session has meaningful activity.
        # This suppresses empty stub sessions from being promoted into history.
        # reconcile_drops counts too (2026-07-29 Codex must-fix): a drop-only session
        # (0 open, 0 closed, N drops) is the exact "everything vanished" case that must
        # still rewrite summary.json so the drops are visible on resume.
        if (self.total_entries > 0 or self.total_exits > 0 or self.open_positions
                or self.reconcile_drops > 0) \
                and not _JOURNAL_READONLY:
            self._save_summary()
