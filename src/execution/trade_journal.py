"""
Paper Trade Journal
Persistent, append-only trade log with portfolio snapshots.
Every trade decision, price update, and exit is recorded to disk.
"""

import json
import logging
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any

from ..journal_features import enrich_entry_extra, enrich_exit_extra

logger = logging.getLogger(__name__)

JOURNAL_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "paper_trades"

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

    is_token_flip = (
        leg == "YES"
        and entry_price > 0
        and abs(entry_price + current_price - 1.0) < 0.02
    )
    return is_token_flip or abs(pnl) > max_plausible_pnl


@dataclass
class JournalEntry:
    """Single trade journal entry — immutable once written."""

    timestamp: str
    event: str  # ENTRY, PRICE_UPDATE, EXIT, SNAPSHOT, SKIP, ERROR
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

        if pos["side"] == "BUY":
            pnl = (exit_price - pos["entry_price"]) * pos["size"]
        else:
            pnl = (pos["entry_price"] - exit_price) * pos["size"]

        # Phantom exit guard: token-ordering bug (YES exit price logged against YES entry)
        # produces exit_price ≈ 1 - entry_price. Only apply in YES-quote coordinates;
        # long-NO legs often have entry_no + exit_no ≈ 1 legitimately.
        _ep = pos["entry_price"]
        leg = infer_entry_leg(pos)
        _is_token_flip = (
            leg == "YES"
            and _ep > 0
            and abs(_ep + exit_price - 1.0) < 0.02
        )
        _is_oversized = abs(pnl) > 200.0
        if _is_token_flip or _is_oversized:
            logger.warning(
                f"PHANTOM EXIT blocked: {pos['strategy']} ep={_ep:.4f} exit={exit_price:.4f} pnl={pnl:+.2f} | {pos['market_question'][:50]}"
            )
            return

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

        snap = PortfolioSnapshot(
            timestamp=datetime.now().isoformat(),
            bankroll=bankroll,
            total_exposure=exposure,
            open_positions=len(self.open_positions),
            total_trades=self.total_entries,
            realized_pnl=round(self.realized_pnl, 4),
            unrealized_pnl=round(unrealized, 4),
            strategies=strats,
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
            is_yes_flip = (
                leg == "YES"
                and ep > 0
                and abs(ep + xv - 1.0) < 0.02
            )
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
            "strategy_stats": closed["strategy_stats"],
        }
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
        if not entries_file.exists():
            return None, None
        first: Optional[str] = None
        last: Optional[str] = None
        try:
            with open(entries_file, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = e.get("timestamp")
                    if ts:
                        if first is None:
                            first = str(ts)
                        last = str(ts)
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
    def list_sessions() -> List[Dict]:
        """List all past paper trade sessions from both active and archive directories."""
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
                summary_file = d / "summary.json"
                if summary_file.exists():
                    try:
                        with open(summary_file) as f:
                            data = json.load(f)
                            is_current = src == "active" and d.name == current_session_id
                            data["_source"] = src
                            data["_is_current"] = is_current
                            data["_status"] = (
                                "active" if is_current else "completed" if src == "active" else "archived"
                            )
                            data["_path"] = str(d)
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
                        is_current = source == "active" and d.name == current_session_id
                        sessions.append(
                            {
                                "session_id": d.name,
                                "_source": source,
                                "_is_current": is_current,
                                "_status": (
                                    "active" if is_current else "completed" if source == "active" else "archived"
                                ),
                                "_path": str(d),
                                **TradeJournal.session_time_meta_for_dir(
                                    d, d.name, source
                                ),
                            }
                        )
                else:
                    is_current = source == "active" and d.name == current_session_id
                    time_meta = TradeJournal.session_time_meta_for_dir(d, d.name, source)
                    if not is_current and not time_meta.get("ended_at"):
                        time_meta["ended_at"] = time_meta.get("last_activity_at")
                    sessions.append(
                        {
                            "session_id": d.name,
                            "_source": source,
                            "_is_current": is_current,
                            "_status": (
                                "active" if is_current else "completed" if source == "active" else "archived"
                            ),
                            "_path": str(d),
                            **time_meta,
                        }
                    )

        # Sort by session_id descending (newest first)
        sessions.sort(key=lambda s: s.get("session_id", ""), reverse=True)
        return sessions

    # ── INTERNAL ──────────────────────────────────────────────────

    def _append_entry(self, entry: JournalEntry):
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
        with open(self._summary_file, "w", encoding="utf-8") as f:
            json.dump(self.get_summary(), f, indent=2)

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
            self.total_exits = exits_count
            self.realized_pnl = rpnl
            self.closed_trades = closed

            # Cross-reference: remove any positions.json entries that already have
            # an EXIT event in entries.jsonl.  This prevents re-settlement after a
            # crash that left positions.json stale.
            exited_ids = {ct["trade_id"] for ct in closed if ct.get("trade_id")}
            stale = [tid for tid in exited_ids if tid in self.open_positions]
            if stale:
                logger.warning(
                    f"Removing {len(stale)} stale open-position(s) that already have EXIT events: {stale}"
                )
                for tid in stale:
                    del self.open_positions[tid]
                self._save_positions()  # Write corrected state back to disk

            self.total_entries = len(self.open_positions) + len(self.closed_trades)
        else:
            self.total_entries = len(self.open_positions)

        # Only flush a summary when the session has meaningful activity.
        # This suppresses empty stub sessions from being promoted into history.
        if self.total_entries > 0 or self.total_exits > 0 or self.open_positions:
            self._save_summary()

