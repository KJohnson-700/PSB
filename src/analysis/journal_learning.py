"""
Journal-driven learning loop: aggregate closed trades across sessions, segment outcomes,
optionally compare to backtest expectations (drift), and emit YAML-oriented param proposals.

Nothing here mutates ``settings.yaml`` — operators apply changes after review.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from src.execution.live_testing import DriftReport
from src.execution.trade_journal import JOURNAL_DIR, TradeJournal, is_phantom_exit_row

logger = logging.getLogger(__name__)

_MAX_PLAUSIBLE_PNL = 200.0

# Repo root: src/analysis/journal_learning.py -> parents[2]
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_VAULT_REL = Path("projects") / "polymarket-bot" / "strategy-log" / "learning-loop.md"
_DEFAULT_PROPOSAL_DIR = _REPO_ROOT / "data" / "learning" / "proposals"

_STRATEGY_CONFIG_KEYS = (
    "bitcoin",
    "sol_macro",
    "eth_macro",
    "hype_macro",
    "xrp_macro",
    "doge_macro",
    "bnb_macro",
)


def _phantom_exit(row: Dict[str, Any]) -> bool:
    return is_phantom_exit_row(row, _MAX_PLAUSIBLE_PNL)


def _iter_session_dirs() -> Iterator[Tuple[Path, str]]:
    archive = JOURNAL_DIR.parent / "paper_trades_archive"
    for base, source in ((JOURNAL_DIR, "active"), (archive, "archived")):
        if not base.exists():
            continue
        for d, src in TradeJournal._iter_session_dirs(base, source):
            yield d, src


def iter_exit_events(
    *,
    session_ids: Optional[set[str]] = None,
    include_archive: bool = True,
) -> Iterator[Dict[str, Any]]:
    """Yield EXIT journal rows from paper trade sessions (phantom-filtered)."""
    for session_dir, _src in _iter_session_dirs():
        if session_ids is not None and session_dir.name not in session_ids:
            continue
        if not include_archive and "paper_trades_archive" in str(session_dir.resolve()):
            continue
        path = session_dir / "entries.jsonl"
        if not path.exists():
            continue
        yield from iter_exit_events_from_file(path, session_id=session_dir.name)


_exit_events_cache: Dict[str, Any] = {}


def iter_exit_events_from_file(
    path: Path,
    *,
    session_id: str = "",
) -> Iterator[Dict[str, Any]]:
    # 2026-07-27 MEM-CHURN FIX: cache the FILTERED exit rows per-path keyed on
    # file identity (mtime+size). Callers on the learning cadence re-read+re-parsed
    # the whole (growing) journal every time — native-RSS churn. Parse once per
    # file version; the (cheap) session_id tag is still applied per-yield, so the
    # yielded rows are identical to the streaming version.
    try:
        st = path.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError as e:
        logger.debug("journal_learning: skip %s: %s", path, e)
        return
    prev = _exit_events_cache.get(str(path))
    if prev is not None and prev[0] == key:
        rows = prev[1]
    else:
        rows = []
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if row.get("event") != "EXIT":
                        continue
                    if _phantom_exit(row):
                        continue
                    rows.append(row)
        except OSError as e:
            logger.debug("journal_learning: skip %s: %s", path, e)
            return
        _exit_events_cache[str(path)] = (key, rows)
    for row in rows:
        yield ({**row, "_session_id": session_id} if session_id else row)


def _extra_blob(row: Dict[str, Any]) -> Dict[str, Any]:
    ex = row.get("extra")
    return ex if isinstance(ex, dict) else {}


def trade_ai_used(row: Dict[str, Any]) -> bool:
    ex = _extra_blob(row)
    v = ex.get("ai_used", row.get("ai_used"))
    if v is True:
        return True
    if isinstance(v, str) and v.lower() in ("true", "1", "yes"):
        return True
    return False


def trade_predicted_edge(row: Dict[str, Any]) -> float:
    ex = _extra_blob(row)
    v = ex.get("entry_edge")
    if v is None:
        v = row.get("edge")
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0


def trade_realized_return_per_notional(row: Dict[str, Any]) -> Optional[float]:
    """PnL / (size * entry_price) when denominator is safe."""
    try:
        size = float(row.get("size") or 0)
        ep = float(row.get("entry_price") or 0)
        pnl = float(row.get("pnl") or 0)
    except (TypeError, ValueError):
        return None
    denom = size * ep
    if denom <= 1e-6:
        return None
    return pnl / denom


def trade_rsi(row: Dict[str, Any]) -> Optional[float]:
    ex = _extra_blob(row)
    for key in ("rsi",):
        if ex.get(key) is not None:
            try:
                return float(ex[key])
            except (TypeError, ValueError):
                pass
    return None


def trade_window_size(row: Dict[str, Any]) -> str:
    ex = _extra_blob(row)
    w = ex.get("window_size")
    if w in ("5m", "15m"):
        return str(w)
    return "unknown"


def trade_htf_bias(row: Dict[str, Any]) -> str:
    ex = _extra_blob(row)
    b = ex.get("htf_bias")
    if not b:
        return "unknown"
    return str(b).upper()


def rsi_bucket(rsi: Optional[float]) -> str:
    if rsi is None:
        return "unknown"
    if rsi >= 60:
        return "rsi_ge_60"
    if rsi <= 40:
        return "rsi_le_40"
    return "rsi_mid_40_60"


@dataclass
class SegmentStats:
    key: str
    trades: int = 0
    wins: int = 0
    total_pnl: float = 0.0
    sum_predicted_edge: float = 0.0
    sum_realized_ret: float = 0.0
    realized_count: int = 0

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0

    @property
    def avg_pnl(self) -> float:
        return self.total_pnl / self.trades if self.trades else 0.0

    @property
    def avg_predicted_edge(self) -> float:
        return self.sum_predicted_edge / self.trades if self.trades else 0.0

    @property
    def avg_realized_ret(self) -> float:
        return self.sum_realized_ret / self.realized_count if self.realized_count else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "trades": self.trades,
            "wins": self.wins,
            "win_rate": round(self.win_rate, 4),
            "total_pnl": round(self.total_pnl, 2),
            "avg_pnl": round(self.avg_pnl, 4),
            "avg_predicted_edge": round(self.avg_predicted_edge, 6),
            "avg_realized_return_on_notional": round(self.avg_realized_ret, 6),
        }


@dataclass
class StrategyLearningStats:
    strategy: str
    trades: int = 0
    wins: int = 0
    total_pnl: float = 0.0
    sum_predicted_edge: float = 0.0
    sum_realized_ret: float = 0.0
    realized_count: int = 0
    # Subset where LLM was consulted (journal ``extra.ai_used``)
    ai_trades: int = 0
    ai_wins: int = 0
    ai_total_pnl: float = 0.0
    ai_sum_predicted_edge: float = 0.0
    ai_sum_realized_ret: float = 0.0
    ai_realized_count: int = 0
    segments: Dict[str, SegmentStats] = field(default_factory=dict)

    def _touch_segment(self, key: str) -> SegmentStats:
        if key not in self.segments:
            self.segments[key] = SegmentStats(key=key)
        return self.segments[key]

    def add_trade(self, row: Dict[str, Any]) -> None:
        pnl = float(row.get("pnl") or 0)
        self.trades += 1
        if pnl > 0:
            self.wins += 1
        self.total_pnl += pnl
        self.sum_predicted_edge += trade_predicted_edge(row)
        rr = trade_realized_return_per_notional(row)
        if rr is not None:
            self.sum_realized_ret += rr
            self.realized_count += 1

        if trade_ai_used(row):
            self.ai_trades += 1
            if pnl > 0:
                self.ai_wins += 1
            self.ai_total_pnl += pnl
            self.ai_sum_predicted_edge += trade_predicted_edge(row)
            air = trade_realized_return_per_notional(row)
            if air is not None:
                self.ai_sum_realized_ret += air
                self.ai_realized_count += 1

        rsi_b = rsi_bucket(trade_rsi(row))
        self._record_segment(rsi_b, row)
        self._record_segment(f"window_{trade_window_size(row)}", row)
        hb = trade_htf_bias(row)
        if hb != "unknown":
            self._record_segment(f"htf_{hb}", row)

    @property
    def ai_win_rate(self) -> float:
        return self.ai_wins / self.ai_trades if self.ai_trades else 0.0

    @property
    def ai_avg_pnl(self) -> float:
        return self.ai_total_pnl / self.ai_trades if self.ai_trades else 0.0

    @property
    def ai_avg_realized_ret(self) -> float:
        return (
            self.ai_sum_realized_ret / self.ai_realized_count if self.ai_realized_count else 0.0
        )

    def _record_segment(self, key: str, row: Dict[str, Any]) -> None:
        seg = self._touch_segment(key)
        pnl = float(row.get("pnl") or 0)
        seg.trades += 1
        if pnl > 0:
            seg.wins += 1
        seg.total_pnl += pnl
        seg.sum_predicted_edge += trade_predicted_edge(row)
        rr = trade_realized_return_per_notional(row)
        if rr is not None:
            seg.sum_realized_ret += rr
            seg.realized_count += 1

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy,
            "trades": self.trades,
            "wins": self.wins,
            "win_rate": round(self.win_rate, 4),
            "total_pnl": round(self.total_pnl, 2),
            "avg_predicted_edge": round(
                self.sum_predicted_edge / self.trades if self.trades else 0.0, 6
            ),
            "avg_realized_return_on_notional": round(
                self.sum_realized_ret / self.realized_count if self.realized_count else 0.0, 6
            ),
            "edge_calibration_gap": round(
                (self.sum_predicted_edge / self.trades if self.trades else 0.0)
                - (self.sum_realized_ret / self.realized_count if self.realized_count else 0.0),
                6,
            ),
            "ai_involved_trades": self.ai_trades,
            "ai_win_rate": round(self.ai_win_rate, 4),
            "ai_total_pnl": round(self.ai_total_pnl, 2),
            "ai_avg_pnl": round(self.ai_avg_pnl, 4),
            "ai_avg_realized_return_on_notional": round(self.ai_avg_realized_ret, 6),
            "segments": {k: v.to_dict() for k, v in sorted(self.segments.items())},
        }


def aggregate_by_strategy(
    rows: List[Dict[str, Any]],
    *,
    strategies: Optional[set[str]] = None,
) -> Dict[str, StrategyLearningStats]:
    out: Dict[str, StrategyLearningStats] = {}
    for row in rows:
        s = str(row.get("strategy") or "unknown")
        if strategies is not None and s not in strategies:
            continue
        if s not in out:
            out[s] = StrategyLearningStats(strategy=s)
        out[s].add_trade(row)
    return out


def check_drift_from_expectations(
    rows: List[Dict[str, Any]],
    backtest_expectations: Dict[str, Dict[str, float]],
) -> List[DriftReport]:
    """Reuse drift semantics from ``live_testing`` with a preloaded EXIT list."""
    live_trades = [r for r in rows if not _phantom_exit(r)]
    reports: List[DriftReport] = []
    from src.execution.backtest_expectations import live_trades_for_expectation

    for strategy, bt_exp in (backtest_expectations or {}).items():
        strat_trades = live_trades_for_expectation(live_trades, strategy)
        if not strat_trades:
            continue
        wins = sum(1 for t in strat_trades if float(t.get("pnl") or 0) > 0)
        live_win_rate = wins / len(strat_trades) if strat_trades else 0
        live_edges = [trade_predicted_edge(t) for t in strat_trades]
        live_avg_edge = sum(live_edges) / len(live_edges) if live_edges else 0

        timestamps = [t.get("timestamp", "") for t in strat_trades if t.get("timestamp")]
        if len(timestamps) >= 2:
            parsed_ts = sorted(
                datetime.fromisoformat(ts.replace("Z", "+00:00")) for ts in timestamps
            )
            elapsed_secs = (parsed_ts[-1] - parsed_ts[0]).total_seconds()
            live_trades_per_day = (
                len(strat_trades) * 86400 / elapsed_secs if elapsed_secs > 0 else 0
            )
        else:
            live_trades_per_day = 0

        bt_win_rate = float(bt_exp.get("win_rate", 0) or 0)
        bt_avg_edge = float(bt_exp.get("avg_edge", 0) or 0)
        bt_trades_per_day = float(bt_exp.get("trades_per_day", 0) or 0)

        report = DriftReport(
            strategy=strategy,
            bt_win_rate=bt_win_rate,
            live_win_rate=live_win_rate,
            win_rate_drift=live_win_rate - bt_win_rate,
            bt_avg_edge=bt_avg_edge,
            live_avg_edge=live_avg_edge,
            edge_drift=live_avg_edge - bt_avg_edge,
            bt_trades_per_day=bt_trades_per_day,
            live_trades_per_day=live_trades_per_day,
            trade_freq_drift=live_trades_per_day - bt_trades_per_day,
            live_sample_size=len(strat_trades),
        )
        min_drift_sample = 15
        if len(strat_trades) < min_drift_sample:
            report.is_diverging = False
            report.verdict = f"INSUFFICIENT_DATA ({len(strat_trades)}/{min_drift_sample})"
        else:
            win_rate_bad = report.win_rate_drift < -0.15
            edge_bad = bt_avg_edge > 0 and report.edge_drift < -bt_avg_edge * 0.5
            report.is_diverging = win_rate_bad or edge_bad
            if report.is_diverging:
                reasons = []
                if win_rate_bad:
                    reasons.append(
                        f"win rate {live_win_rate:.0%} vs BT {bt_win_rate:.0%}"
                    )
                if edge_bad:
                    reasons.append(f"edge {live_avg_edge:.4f} vs BT {bt_avg_edge:.4f}")
                report.verdict = f"DIVERGING: {', '.join(reasons)}"
            else:
                report.verdict = "OK"
        reports.append(report)
    return reports


def _get_cfg_path(cfg: Dict[str, Any], strategy: str, key: str) -> Tuple[str, Any]:
    """Return dotted path and current value from loaded config."""
    block = (cfg.get("strategies") or {}).get(strategy)
    if not isinstance(block, dict):
        return f"strategies.{strategy}.{key}", None
    return f"strategies.{strategy}.{key}", block.get(key)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def propose_param_updates(
    cfg: Dict[str, Any],
    by_strategy: Dict[str, StrategyLearningStats],
    drift_reports: List[DriftReport],
    *,
    learning_cfg: Optional[Dict[str, Any]] = None,
    min_trades: int = 15,
    min_segment_trades: int = 15,
) -> List[Dict[str, Any]]:
    """Heuristic proposals only; every item requires human review before edit."""
    lc = learning_cfg or {}
    proposals: List[Dict[str, Any]] = []
    drift_by = {r.strategy.lower(): r for r in drift_reports}

    ai_min = int(lc.get("ai_proposal_min_trades", 75) or 75)
    ai_max_raw = lc.get("ai_proposal_max_trades")
    ai_max = int(ai_max_raw) if ai_max_raw is not None else None
    ai_wr_cap = float(lc.get("ai_proposal_win_rate_below", 0.63) or 0.63)
    ai_neg_pnl = bool(lc.get("ai_proposal_require_negative_pnl", True))
    ai_ret_min_n = int(lc.get("ai_proposal_margin_min_realized_n", 15) or 15)
    ai_weak_ret = bool(lc.get("ai_proposal_require_negative_avg_return_on_notional", False))
    ai_conf_bump = float(lc.get("ai_confidence_bump", 0.02) or 0.02)

    for strat, stats in by_strategy.items():
        if strat not in _STRATEGY_CONFIG_KEYS:
            continue
        stl = strat.lower()
        base_wr = stats.win_rate
        # 1) RSI high bucket weak
        seg = stats.segments.get("rsi_ge_60")
        if (
            seg
            and seg.trades >= min_segment_trades
            and seg.win_rate < base_wr - 0.12
            and seg.total_pnl < 0
        ):
            path, cur = _get_cfg_path(cfg, strat, "neutral_rsi_extra_min_edge")
            bump = 0.01
            cur_f = float(cur or 0.0)
            proposals.append(
                {
                    "strategy": strat,
                    "param": "neutral_rsi_extra_min_edge",
                    "config_path": path,
                    "current": cur_f,
                    "proposed": _clamp(cur_f + bump, 0.0, 0.08),
                    "reason": (
                        f"Last {seg.trades} trades with RSI>=60: win_rate={seg.win_rate:.1%} vs "
                        f"baseline {base_wr:.1%}, PnL=${seg.total_pnl:.2f} — consider extra min-edge for elevated RSI"
                    ),
                    "requires_human_review": True,
                    "evidence": seg.to_dict(),
                }
            )

        # 2) LLM-involved trades: mature sample, win rate under target, poor margin
        if stats.ai_trades >= ai_min and (ai_max is None or stats.ai_trades <= ai_max):
            ai_wr = stats.ai_win_rate
            margin_parts: List[bool] = []
            if ai_neg_pnl:
                margin_parts.append(stats.ai_total_pnl < 0)
            if ai_weak_ret:
                if stats.ai_realized_count >= ai_ret_min_n:
                    margin_parts.append(stats.ai_avg_realized_ret < 0)
                else:
                    margin_parts.append(False)
            margin_bad = all(margin_parts) if margin_parts else (stats.ai_total_pnl < 0)
            if (
                ai_wr < ai_wr_cap
                and margin_bad
            ):
                path, cur = _get_cfg_path(cfg, strat, "ai_confidence_threshold")
                cur_f = float(cur or 0.6)
                proposals.append(
                    {
                        "strategy": strat,
                        "param": "ai_confidence_threshold",
                        "config_path": path,
                        "current": cur_f,
                        "proposed": _clamp(cur_f + ai_conf_bump, 0.50, 0.92),
                        "reason": (
                            f"AI-touched trades n={stats.ai_trades}: win_rate={ai_wr:.1%} "
                            f"(threshold {ai_wr_cap:.0%}), total_PnL=${stats.ai_total_pnl:.2f} "
                            f"— consider raising AI confidence bar; optional: min_edge_5m_ai_override in same review"
                        ),
                        "requires_human_review": True,
                        "evidence": {
                            "ai_trades": stats.ai_trades,
                            "ai_win_rate": ai_wr,
                            "ai_total_pnl": stats.ai_total_pnl,
                            "ai_avg_pnl": stats.ai_avg_pnl,
                            "ai_avg_realized_return_on_notional": stats.ai_avg_realized_ret,
                        },
                    }
                )

        # 3) Edge calibration: predicted edge >> realized return on notional
        if stats.trades >= min_trades and stats.realized_count >= min_trades:
            pred = stats.sum_predicted_edge / stats.trades
            real = stats.sum_realized_ret / stats.realized_count
            if pred - real > 0.04:
                path, cur = _get_cfg_path(cfg, strat, "min_edge")
                cur_f = float(cur or 0.08)
                proposals.append(
                    {
                        "strategy": strat,
                        "param": "min_edge",
                        "config_path": path,
                        "current": cur_f,
                        "proposed": _clamp(cur_f + 0.005, 0.03, 0.22),
                        "reason": (
                            f"Avg predicted edge {pred:.4f} vs avg realized return/on-notional {real:.4f} "
                            f"across n={stats.trades} — tighten primary min_edge slightly"
                        ),
                        "requires_human_review": True,
                        "evidence": {
                            "avg_predicted_edge": pred,
                            "avg_realized_return": real,
                            "trades": stats.trades,
                        },
                    }
                )

        # 4) Poor overall win rate — modest Kelly reduction (skip if mostly an AI-path issue already flagged)
        kelly_ai_cutoff = int(lc.get("kelly_rule_max_ai_trades", 50) or 50)
        if (
            stats.trades >= 25
            and base_wr < 0.45
            and stats.total_pnl < 0
            and stats.ai_trades < kelly_ai_cutoff
        ):
            path, cur = _get_cfg_path(cfg, strat, "kelly_fraction")
            cur_f = float(cur or 0.15)
            proposals.append(
                {
                    "strategy": strat,
                    "param": "kelly_fraction",
                    "config_path": path,
                    "current": cur_f,
                    "proposed": _clamp(round(cur_f * 0.9, 4), 0.05, 0.5),
                    "reason": (
                        f"Win rate {base_wr:.1%} over n={stats.trades} with negative PnL "
                        f"(AI-touched n={stats.ai_trades} < {kelly_ai_cutoff}) — consider reducing Kelly"
                    ),
                    "requires_human_review": True,
                    "evidence": {"win_rate": base_wr, "trades": stats.trades},
                }
            )

        # 5) Drift vs backtest
        dr = drift_by.get(stl)
        if dr and dr.is_diverging:
            path, cur = _get_cfg_path(cfg, strat, "min_edge")
            cur_f = float(cur or 0.08)
            proposals.append(
                {
                    "strategy": strat,
                    "param": "min_edge",
                    "config_path": path,
                    "current": cur_f,
                    "proposed": _clamp(cur_f + 0.005, 0.03, 0.22),
                    "reason": f"Live vs backtest drift flagged: {dr.verdict}",
                    "requires_human_review": True,
                    "evidence": {
                        "win_rate_drift": dr.win_rate_drift,
                        "edge_drift": dr.edge_drift,
                    },
                }
            )

    # Dedupe same param/strategy (keep first)
    seen: set[Tuple[str, str]] = set()
    deduped: List[Dict[str, Any]] = []
    for p in proposals:
        k = (p["strategy"], p["param"])
        if k in seen:
            continue
        seen.add(k)
        deduped.append(p)
    return deduped


def default_vault_learning_path() -> Path:
    return _REPO_ROOT / _DEFAULT_VAULT_REL


def run_learning_cycle(
    cfg: Dict[str, Any],
    *,
    include_archive: bool = True,
    write_files: bool = True,
    vault_path: Optional[Path] = None,
    proposal_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Run full aggregation + drift + proposals; optionally persist JSON + vault markdown."""
    learning_cfg = (cfg.get("learning_loop") or {}) if isinstance(cfg.get("learning_loop"), dict) else {}
    if not include_archive:
        include_archive = bool(learning_cfg.get("include_archive", True))

    min_trades = int(learning_cfg.get("min_trades_for_rules", 15) or 15)
    min_seg = int(learning_cfg.get("min_trades_per_segment", 15) or 15)
    expectations = learning_cfg.get("backtest_expectations") or {}

    rows = list(iter_exit_events(include_archive=include_archive))
    by_strat = aggregate_by_strategy(rows)
    drift = check_drift_from_expectations(rows, expectations) if expectations else []
    proposals = propose_param_updates(
        cfg,
        by_strat,
        drift,
        learning_cfg=learning_cfg,
        min_trades=min_trades,
        min_segment_trades=min_seg,
    )

    out: Dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "exit_events_used": len(rows),
        "strategies": {k: v.to_dict() for k, v in sorted(by_strat.items())},
        "drift_reports": [asdict(r) for r in drift],
        "proposals": proposals,
        "notes": (
            "Proposals are suggestions only. Review and edit config/settings.yaml manually; "
            "do not apply automated patches without validation."
        ),
    }

    if write_files and bool(learning_cfg.get("persist_artifacts", True)):
        prop_dir = proposal_dir or Path(
            learning_cfg.get("proposal_dir", str(_DEFAULT_PROPOSAL_DIR))
        )
        prop_dir = prop_dir if prop_dir.is_absolute() else _REPO_ROOT / prop_dir
        prop_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        json_path = prop_dir / f"learning_proposal_{ts}.json"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
        out["proposal_json_path"] = str(json_path)

        if bool(learning_cfg.get("write_vault_log", True)):
            vp = vault_path or default_vault_learning_path()
            if not vp.is_absolute():
                vp = _REPO_ROOT / vp
            _append_vault_markdown(vp, out)

    return out


def _append_vault_markdown(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    iso = payload.get("generated_at", "")
    lines = [
        "",
        f"## {iso} — Learning loop (automated)",
        "",
        f"- **EXIT rows analyzed:** {payload.get('exit_events_used', 0)}",
        "- **Status:** All proposed parameter changes require human review before applying.",
        "",
        "### Per-strategy aggregates",
        "",
    ]
    for sk, block in sorted((payload.get("strategies") or {}).items()):
        lines.append(f"- **{sk}**: trades={block.get('trades')} win_rate={block.get('win_rate')} "
                     f"PnL=${block.get('total_pnl')} edge_gap={block.get('edge_calibration_gap')}")
    dr = payload.get("drift_reports") or []
    if dr:
        lines.extend(["", "### Drift vs backtest expectations", ""])
        for r in dr:
            lines.append(
                f"- `{r.get('strategy')}`: verdict={r.get('verdict')} "
                f"live_WR={r.get('live_win_rate'):.3f} sample={r.get('live_sample_size')}"
            )
    props = payload.get("proposals") or []
    lines.extend(["", "### Proposed adjustments (pending review)", ""])
    if not props:
        lines.append("- *(none this run)*")
    else:
        for p in props:
            lines.append(
                f"- **{p.get('strategy')}** `{p.get('param')}`: {p.get('current')} → **{p.get('proposed')}** — "
                f"{p.get('reason')}"
            )
    lines.append("")
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines))


def log_learning_summary_to_logger(payload: Dict[str, Any]) -> None:
    """Structured INFO lines for stdout / local logs."""
    logger.info(
        "Learning loop: aggregated %d EXIT rows across sessions",
        payload.get("exit_events_used", 0),
    )
    for sk, block in sorted((payload.get("strategies") or {}).items()):
        logger.info(
            "  [%s] trades=%s win_rate=%s pnl=%s edge_gap=%s | ai_n=%s ai_wr=%s ai_pnl=%s",
            sk,
            block.get("trades"),
            block.get("win_rate"),
            block.get("total_pnl"),
            block.get("edge_calibration_gap"),
            block.get("ai_involved_trades"),
            block.get("ai_win_rate"),
            block.get("ai_total_pnl"),
        )
    for p in payload.get("proposals") or []:
        logger.info(
            "  PROPOSAL (review): %s %s %s -> %s | %s",
            p.get("strategy"),
            p.get("param"),
            p.get("current"),
            p.get("proposed"),
            p.get("reason")[:120],
        )


def learning_loop_enabled(cfg: Dict[str, Any]) -> bool:
    block = cfg.get("learning_loop")
    if isinstance(block, dict) and block.get("enabled"):
        return True
    return False
