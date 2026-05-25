#!/usr/bin/env python3
"""Analyze crypto up/down exit counterfactuals by lane.

This is a read-only research tool. It reconstructs each closed journal trade's
token-price path from journal marks and, when available, cached 1m OHLCV proxy
marks through the market window. The output is intended to answer one question:
are profitable exits capturing most of the available trade path, or are they
leaving systematic lane-level upside behind?
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.execution.trade_journal import JOURNAL_DIR, TradeJournal, infer_entry_leg


SUPPORTED_OHLCV_SYMBOLS = frozenset({"BTC", "SOL", "ETH", "XRP", "HYPE"})
SYMBOL_ALIASES = {
    "BITCOIN": "BTC",
    "BTC": "BTC",
    "SOLANA": "SOL",
    "SOL": "SOL",
    "ETHEREUM": "ETH",
    "ETH": "ETH",
    "XRP": "XRP",
    "HYPE": "HYPE",
    "HYPERLIQUID": "HYPE",
}


@dataclass(frozen=True)
class PathPoint:
    ts: str
    token_price: float
    pnl: float
    source: str
    event: str


@dataclass(frozen=True)
class TradeAnalysis:
    trade_id: str
    strategy: str
    market_id: str
    market_question: str
    lane: str
    window_size: str
    action: str
    entry_leg: str
    outcome: str
    entry_price: float
    size: float
    actual_exit_reason: str
    actual_pnl: float
    actual_exit_price: float
    hold_pnl: float
    hold_token_price: float
    hold_source: str
    mfe: float
    mae: float
    max_possible_profit: float
    profit_capture_ratio: Optional[float]
    regret: float
    winner_exit_class: str
    triple_barrier_label: str
    path_points: int
    reconstructed_points: int
    has_post_exit_path: bool


def _default_entries_file() -> Optional[Path]:
    chosen = TradeJournal.newest_resumable_session_dir()
    if chosen is None:
        return None
    entries = chosen / "entries.jsonl"
    return entries if entries.is_file() else None


def _load_rows(entries_path: Path) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with entries_path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("event") not in {"ENTRY", "PRICE_UPDATE", "EXIT"}:
                continue
            trade_id = str(row.get("trade_id") or "")
            if trade_id:
                grouped[trade_id].append(row)
    return grouped


def _parse_ts(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        out = datetime.fromisoformat(text)
    except ValueError:
        return None
    if out.tzinfo is None:
        out = out.replace(tzinfo=timezone.utc)
    return out.astimezone(timezone.utc)


def _is_crypto_updown(entry: dict[str, Any]) -> bool:
    return "up or down" in str(entry.get("market_question") or "").lower()


def _infer_symbol(question: str) -> Optional[str]:
    first = str(question or "").strip().split(" ", 1)[0].upper()
    if first in SYMBOL_ALIASES:
        return SYMBOL_ALIASES[first]
    upper = str(question or "").upper()
    for name, symbol in SYMBOL_ALIASES.items():
        if name in upper:
            return symbol
    return None


def _infer_window_minutes(question: str, fallback: str = "") -> Optional[int]:
    text = str(fallback or "").strip().lower()
    if text.endswith("m"):
        try:
            return int(text[:-1])
        except ValueError:
            pass
    m = re.search(
        r"(\d{1,2}):(\d{2})\s*(AM|PM)\s*[-–]\s*(\d{1,2}):(\d{2})\s*(AM|PM)",
        question or "",
        re.IGNORECASE,
    )
    if not m:
        return None

    def to_minutes(hour: str, minute: str, ampm: str) -> int:
        h = int(hour)
        mi = int(minute)
        ap = ampm.upper()
        if ap == "AM" and h == 12:
            h = 0
        elif ap == "PM" and h != 12:
            h += 12
        return h * 60 + mi

    start = to_minutes(m.group(1), m.group(2), m.group(3))
    end = to_minutes(m.group(4), m.group(5), m.group(6))
    delta = end - start
    if delta <= 0:
        delta += 24 * 60
    return delta


def _infer_window_bounds(question: str, entry_ts: datetime, fallback: str = "") -> tuple[datetime, datetime] | None:
    m = re.search(
        r"([A-Za-z]+)\s+(\d{1,2}),\s+(\d{1,2}):(\d{2})\s*(AM|PM)\s*[-–]\s*(\d{1,2}):(\d{2})\s*(AM|PM)\s*ET",
        question or "",
        re.IGNORECASE,
    )
    if not m:
        return None
    month_name = m.group(1)
    try:
        month = datetime.strptime(month_name[:3], "%b").month
    except ValueError:
        return None
    day = int(m.group(2))
    year = entry_ts.astimezone(ZoneInfo("America/New_York")).year

    def local_dt(hour: str, minute: str, ampm: str) -> datetime:
        h = int(hour)
        mi = int(minute)
        ap = ampm.upper()
        if ap == "AM" and h == 12:
            h = 0
        elif ap == "PM" and h != 12:
            h += 12
        return datetime(year, month, day, h, mi, tzinfo=ZoneInfo("America/New_York"))

    start = local_dt(m.group(3), m.group(4), m.group(5))
    end = local_dt(m.group(6), m.group(7), m.group(8))
    if end <= start:
        end = end + timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _pnl_for_token_price(
    *,
    token_price: float,
    entry_price: float,
    size: float,
    side: str,
    outcome: str,
    entry_leg: str,
) -> float:
    if str(side or "").upper() == "SELL" and str(outcome or "").upper() == "NO" and entry_leg != "NO":
        return (entry_price - token_price) * size
    return (token_price - entry_price) * size


def _cost_basis(*, entry_price: float, size: float, side: str, outcome: str, entry_leg: str) -> float:
    if str(side or "").upper() == "SELL" and str(outcome or "").upper() == "NO" and entry_leg != "NO":
        return max(0.0, (1.0 - entry_price) * size)
    return max(0.0, entry_price * size)


def _token_price_from_yes(yes_price: float, *, entry_leg: str, outcome: str) -> float:
    if entry_leg == "NO":
        return 1.0 - yes_price
    return yes_price


def _proxy_yes_price(asset_open: float, asset_current: float, window_minutes: int) -> float:
    if asset_open <= 0:
        return 0.50
    move_pct = (asset_current - asset_open) / asset_open
    scale_pct = 0.0015 if window_minutes == 5 else 0.0025
    score = move_pct / max(scale_pct, 1e-6)
    return max(0.01, min(0.99, 0.50 + 0.45 * math.tanh(score)))


def _ohlcv_cache_path(symbol: str, interval: str = "1m") -> Path:
    key = "HYPE" if symbol == "HYPE" else f"{symbol}USDT"
    return Path(__file__).resolve().parent.parent / "data" / "backtest" / "ohlcv" / f"{key}_{interval}.parquet"


def _load_cached_ohlcv_points(
    *,
    symbol: str,
    window_minutes: int,
    window_open: datetime,
    window_close: datetime,
    entry_ts: datetime,
    entry_leg: str,
    outcome: str,
    entry_price: float,
    size: float,
    side: str,
    fetch_missing: bool = False,
    memory_cache: Optional[dict[tuple[str, str, str], Any]] = None,
) -> list[PathPoint]:
    if symbol not in SUPPORTED_OHLCV_SYMBOLS:
        return []
    try:
        import pandas as pd
    except ImportError:
        return []
    path = _ohlcv_cache_path(symbol)
    df = None
    if path.is_file():
        try:
            df = pd.read_parquet(path)
        except Exception:
            df = None
    start_date = window_open.date().isoformat()
    end_date = window_close.date().isoformat()
    key = (symbol, start_date, end_date)
    if (df is None or df.empty) and fetch_missing:
        cache = memory_cache if memory_cache is not None else {}
        if key in cache:
            df = cache[key]
        else:
            try:
                from src.data.ohlcv_loader import OHLCVLoader

                loader_symbol = "HYPE" if symbol == "HYPE" else f"{symbol}USDT"
                df = OHLCVLoader(no_cache=True).load(loader_symbol, "1m", start_date, end_date)
                cache[key] = df
            except Exception:
                df = None
    if df is None or df.empty or "open_time" not in df.columns or "close" not in df.columns:
        return []
    df = df.copy()
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    start_ts = pd.Timestamp(window_open)
    close_ts = pd.Timestamp(window_close)
    w = df[(df["open_time"] >= start_ts) & (df["open_time"] < close_ts)].sort_values("open_time")
    if w.empty and fetch_missing:
        cache = memory_cache if memory_cache is not None else {}
        if key in cache:
            df = cache[key]
        else:
            try:
                from src.data.ohlcv_loader import OHLCVLoader

                loader_symbol = "HYPE" if symbol == "HYPE" else f"{symbol}USDT"
                df = OHLCVLoader(no_cache=True).load(loader_symbol, "1m", start_date, end_date)
                cache[key] = df
            except Exception:
                df = None
        if df is not None and not df.empty and "open_time" in df.columns and "close" in df.columns:
            df = df.copy()
            df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
            w = df[(df["open_time"] >= start_ts) & (df["open_time"] < close_ts)].sort_values("open_time")
    if w.empty:
        return []
    asset_open = float(w.iloc[0].get("open", w.iloc[0]["close"]) or w.iloc[0]["close"])
    if asset_open <= 0:
        return []
    entry_cutoff = pd.Timestamp(entry_ts)
    points: list[PathPoint] = []
    for _, row in w[w["open_time"] >= entry_cutoff].iterrows():
        ts = row["open_time"].to_pydatetime().astimezone(timezone.utc)
        yes = _proxy_yes_price(asset_open, float(row["close"]), window_minutes)
        token = _token_price_from_yes(yes, entry_leg=entry_leg, outcome=outcome)
        pnl = _pnl_for_token_price(
            token_price=token,
            entry_price=entry_price,
            size=size,
            side=side,
            outcome=outcome,
            entry_leg=entry_leg,
        )
        points.append(
            PathPoint(
                ts=ts.isoformat(),
                token_price=round(token, 6),
                pnl=round(pnl, 6),
                source="ohlcv_proxy",
                event="PROXY_MARK",
            )
        )
    last_close = float(w.iloc[-1]["close"])
    yes_won = last_close >= asset_open
    token_final = 1.0 if ((yes_won and entry_leg != "NO") or ((not yes_won) and entry_leg == "NO")) else 0.0
    if str(side or "").upper() == "SELL" and str(outcome or "").upper() == "NO" and entry_leg != "NO":
        token_final = 1.0 if yes_won else 0.0
    final_pnl = _pnl_for_token_price(
        token_price=token_final,
        entry_price=entry_price,
        size=size,
        side=side,
        outcome=outcome,
        entry_leg=entry_leg,
    )
    points.append(
        PathPoint(
            ts=window_close.isoformat(),
            token_price=round(token_final, 6),
            pnl=round(final_pnl, 6),
            source="ohlcv_settlement",
            event="COUNTERFACTUAL_SETTLEMENT",
        )
    )
    return points


def _journal_points(
    rows: Iterable[dict[str, Any]],
    *,
    entry_price: float,
    size: float,
    side: str,
    outcome: str,
    entry_leg: str,
) -> list[PathPoint]:
    points: list[PathPoint] = []
    for row in rows:
        ts = _parse_ts(row.get("timestamp"))
        if ts is None:
            continue
        if row.get("event") == "ENTRY":
            token_price = float(row.get("entry_price") or entry_price)
        else:
            token_price = float(row.get("current_price") or row.get("entry_price") or entry_price)
        pnl = _pnl_for_token_price(
            token_price=token_price,
            entry_price=entry_price,
            size=size,
            side=side,
            outcome=outcome,
            entry_leg=entry_leg,
        )
        if row.get("event") == "EXIT" and row.get("pnl") is not None:
            pnl = float(row.get("pnl") or 0.0)
        points.append(
            PathPoint(
                ts=ts.isoformat(),
                token_price=round(token_price, 6),
                pnl=round(pnl, 6),
                source="journal",
                event=str(row.get("event") or ""),
            )
        )
    return sorted(points, key=lambda p: p.ts)


def _triple_barrier_label(points: list[PathPoint], cost_basis: float, tp_pct: float, sl_pct: float) -> str:
    if cost_basis <= 0:
        return "invalid_cost_basis"
    for point in sorted(points, key=lambda p: p.ts):
        if point.event == "ENTRY":
            continue
        pnl_pct = point.pnl / cost_basis
        if pnl_pct >= tp_pct:
            return "profit_barrier"
        if sl_pct > 0 and pnl_pct <= -sl_pct:
            return "stop_barrier"
    return "time_or_resolution_barrier"


def _classify_exit(
    *,
    actual_pnl: float,
    hold_pnl: float,
    max_possible_profit: float,
    capture_ratio: Optional[float],
    actual_exit_reason: str,
    has_post_exit_path: bool,
) -> str:
    if not has_post_exit_path:
        return "insufficient_post_exit_path"
    if actual_pnl > 0:
        if hold_pnl > actual_pnl and max_possible_profit > actual_pnl and (capture_ratio or 0.0) < 0.70:
            return "premature_take_profit"
        return "good_capture"
    if max_possible_profit > 0:
        return "gave_back_winner"
    if actual_exit_reason in {"updown_stop_loss", "updown_time_stop", "stop_loss"}:
        if hold_pnl < actual_pnl:
            return "stop_saved_trade"
        if hold_pnl > actual_pnl:
            return "stop_hurt_trade"
    return "no_winner_path"


def analyze_trade(
    rows: list[dict[str, Any]],
    *,
    tp_pct: float = 0.50,
    sl_pct: float = 0.20,
    fetch_missing_ohlcv: bool = False,
    memory_cache: Optional[dict[tuple[str, str, str], Any]] = None,
) -> Optional[TradeAnalysis]:
    rows = sorted(rows, key=lambda r: str(r.get("timestamp") or ""))
    entry = next((r for r in rows if r.get("event") == "ENTRY"), None)
    exit_row = next((r for r in rows if r.get("event") == "EXIT"), None)
    if not entry or not exit_row or not _is_crypto_updown(entry):
        return None

    entry_ts = _parse_ts(entry.get("timestamp"))
    exit_ts = _parse_ts(exit_row.get("timestamp"))
    if entry_ts is None or exit_ts is None:
        return None

    extra = entry.get("extra") or {}
    strategy = str(entry.get("strategy") or exit_row.get("strategy") or "?")
    question = str(entry.get("market_question") or exit_row.get("market_question") or "")
    entry_price = float(entry.get("entry_price") or 0.0)
    size = float(entry.get("size") or 0.0)
    if entry_price <= 0 or size <= 0:
        return None

    entry_leg = str(extra.get("entry_leg") or infer_entry_leg(entry)).upper()
    if entry_leg not in {"YES", "NO"}:
        entry_leg = "YES"
    side = str(entry.get("side") or "BUY").upper()
    outcome = str(entry.get("outcome") or "YES").upper()
    window_size = str(extra.get("window_size") or "").lower()
    lane = str(extra.get("lane_id") or "").strip()
    if not lane:
        lane = "|".join(
            [
                strategy,
                window_size or "?",
                str(extra.get("lane_side") or entry.get("action") or "?"),
                str(extra.get("lane_regime") or "?"),
                str(extra.get("entry_family") or "?"),
            ]
        )

    journal = _journal_points(
        rows,
        entry_price=entry_price,
        size=size,
        side=side,
        outcome=outcome,
        entry_leg=entry_leg,
    )
    proxy: list[PathPoint] = []
    symbol = _infer_symbol(question)
    window_minutes = _infer_window_minutes(question, window_size)
    bounds = _infer_window_bounds(question, entry_ts, window_size)
    if symbol and window_minutes and bounds:
        proxy = _load_cached_ohlcv_points(
            symbol=symbol,
            window_minutes=window_minutes,
            window_open=bounds[0],
            window_close=bounds[1],
            entry_ts=entry_ts,
            entry_leg=entry_leg,
            outcome=outcome,
            entry_price=entry_price,
            size=size,
            side=side,
            fetch_missing=fetch_missing_ohlcv,
            memory_cache=memory_cache,
        )

    all_points_by_key = {(p.ts, p.event, p.source): p for p in journal}
    for point in proxy:
        all_points_by_key[(point.ts, point.event, point.source)] = point
    path = sorted(all_points_by_key.values(), key=lambda p: p.ts)
    if not path:
        return None

    post_exit = [p for p in path if _parse_ts(p.ts) and _parse_ts(p.ts) > exit_ts]
    terminal = path[-1]
    actual_exit_price = float(exit_row.get("current_price") or entry_price)
    actual_pnl = float(exit_row.get("pnl") or 0.0)
    max_pnl = max(p.pnl for p in path)
    min_pnl = min(p.pnl for p in path)
    max_possible_profit = max(0.0, max_pnl)
    capture_ratio = None
    if actual_pnl > 0 and max_possible_profit > 0:
        capture_ratio = max(0.0, min(1.0, actual_pnl / max_possible_profit))
    cost = _cost_basis(
        entry_price=entry_price,
        size=size,
        side=side,
        outcome=outcome,
        entry_leg=entry_leg,
    )
    label = _triple_barrier_label(path, cost, tp_pct, sl_pct)
    cls = _classify_exit(
        actual_pnl=actual_pnl,
        hold_pnl=terminal.pnl,
        max_possible_profit=max_possible_profit,
        capture_ratio=capture_ratio,
        actual_exit_reason=str(exit_row.get("reason") or ""),
        has_post_exit_path=bool(post_exit),
    )
    return TradeAnalysis(
        trade_id=str(entry.get("trade_id") or ""),
        strategy=strategy,
        market_id=str(entry.get("market_id") or ""),
        market_question=question,
        lane=lane,
        window_size=window_size or "?",
        action=str(entry.get("action") or ""),
        entry_leg=entry_leg,
        outcome=outcome,
        entry_price=round(entry_price, 6),
        size=round(size, 6),
        actual_exit_reason=str(exit_row.get("reason") or ""),
        actual_pnl=round(actual_pnl, 6),
        actual_exit_price=round(actual_exit_price, 6),
        hold_pnl=round(terminal.pnl, 6),
        hold_token_price=round(terminal.token_price, 6),
        hold_source=terminal.source,
        mfe=round(max_pnl, 6),
        mae=round(abs(min(0.0, min_pnl)), 6),
        max_possible_profit=round(max_possible_profit, 6),
        profit_capture_ratio=round(capture_ratio, 4) if capture_ratio is not None else None,
        regret=round(terminal.pnl - actual_pnl, 6),
        winner_exit_class=cls,
        triple_barrier_label=label,
        path_points=len(path),
        reconstructed_points=sum(1 for p in path if p.source.startswith("ohlcv")),
        has_post_exit_path=bool(post_exit),
    )


def _median(values: list[float]) -> float:
    return round(statistics.median(values), 6) if values else 0.0


def _recommend(group: list[TradeAnalysis]) -> str:
    n = len(group)
    if n < 5:
        return "collect_more_samples"
    classes = Counter(t.winner_exit_class for t in group)
    premature_share = classes["premature_take_profit"] / n
    gave_back_share = classes["gave_back_winner"] / n
    stop_hurt_share = classes["stop_hurt_trade"] / n
    total_regret = sum(t.regret for t in group)
    if premature_share >= 0.30 and total_regret > 0:
        return "replay_higher_tp_or_trailing_profit"
    if gave_back_share >= 0.25:
        return "test_trailing_exit_after_mfe"
    if stop_hurt_share >= 0.25 and total_regret > 0:
        return "replay_wider_or_delayed_stop"
    return "keep_current_collect_more"


def _summarize_group(group: list[TradeAnalysis]) -> dict[str, Any]:
    captures = [t.profit_capture_ratio for t in group if t.profit_capture_ratio is not None]
    return {
        "trades": len(group),
        "actual_pnl": round(sum(t.actual_pnl for t in group), 4),
        "hold_pnl": round(sum(t.hold_pnl for t in group), 4),
        "regret": round(sum(t.regret for t in group), 4),
        "median_mfe": _median([t.mfe for t in group]),
        "median_mae": _median([t.mae for t in group]),
        "median_capture_ratio": round(statistics.median(captures), 4) if captures else None,
        "classes": dict(Counter(t.winner_exit_class for t in group)),
        "triple_barriers": dict(Counter(t.triple_barrier_label for t in group)),
        "post_exit_coverage": round(sum(1 for t in group if t.has_post_exit_path) / len(group), 4)
        if group
        else 0.0,
        "recommended_exit_experiment": _recommend(group),
    }


def build_report(
    entries_path: Path,
    *,
    tp_pct: float = 0.50,
    sl_pct: float = 0.20,
    fetch_missing_ohlcv: bool = False,
) -> dict[str, Any]:
    trades: list[TradeAnalysis] = []
    memory_cache: dict[tuple[str, str, str], Any] = {}
    for rows in _load_rows(entries_path).values():
        analysis = analyze_trade(
            rows,
            tp_pct=tp_pct,
            sl_pct=sl_pct,
            fetch_missing_ohlcv=fetch_missing_ohlcv,
            memory_cache=memory_cache,
        )
        if analysis is not None:
            trades.append(analysis)
    by_lane: dict[str, list[TradeAnalysis]] = defaultdict(list)
    for trade in trades:
        by_lane[trade.lane].append(trade)
    return {
        "entries_file": str(entries_path.resolve()),
        "tp_pct": tp_pct,
        "sl_pct": sl_pct,
        "fetch_missing_ohlcv": fetch_missing_ohlcv,
        "eligible_trades": len(trades),
        "overall": _summarize_group(trades),
        "lanes": {lane: _summarize_group(group) for lane, group in sorted(by_lane.items())},
        "trades": [asdict(t) for t in trades],
    }


def _markdown_table(report: dict[str, Any]) -> str:
    lines = [
        "## Lane Exit Counterfactual Analysis",
        "",
        f"**Entries:** `{report['entries_file']}`",
        f"**Eligible trades:** {report['eligible_trades']}",
        f"**Triple-barrier params:** TP `{report['tp_pct']:.2f}`, SL `{report['sl_pct']:.2f}`",
        f"**Fetch missing OHLCV:** `{report['fetch_missing_ohlcv']}`",
        "",
        "### Overall",
        (
            f"- **Actual PnL:** {report['overall']['actual_pnl']:+.2f} | "
            f"**Hold PnL:** {report['overall']['hold_pnl']:+.2f} | "
            f"**Regret:** {report['overall']['regret']:+.2f} | "
            f"**Post-exit coverage:** {report['overall']['post_exit_coverage']:.1%}"
        ),
        f"- **Winner classes:** `{report['overall']['classes']}`",
        "",
        "### Lane Table",
        "| lane | trades | actual PnL | hold PnL | regret | med MFE | med MAE | capture | classes | recommendation |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for lane, stats in report["lanes"].items():
        capture = stats["median_capture_ratio"]
        capture_txt = "" if capture is None else f"{capture:.2f}"
        lines.append(
            f"| {lane} | {stats['trades']} | {stats['actual_pnl']:+.2f} | "
            f"{stats['hold_pnl']:+.2f} | {stats['regret']:+.2f} | "
            f"{stats['median_mfe']:+.2f} | {stats['median_mae']:+.2f} | "
            f"{capture_txt} | `{stats['classes']}` | {stats['recommended_exit_experiment']} |"
        )
    lines.extend(
        [
            "",
            "### Notes",
            "- **MFE/MAE** are measured in realized PnL dollars from the traded token path.",
            "- **Hold PnL** uses cached OHLCV proxy settlement when available; otherwise it falls back to the final journal mark.",
            "- **Do not change live exit settings** from this report alone unless lane sample size and post-exit coverage are adequate.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_outputs(report: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"exit_counterfactuals_{stamp}.json"
    md_path = output_dir / f"exit_counterfactuals_{stamp}.md"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    md_path.write_text(_markdown_table(report), encoding="utf-8")
    return json_path, md_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entries", type=Path, default=None)
    parser.add_argument("--tp-pct", type=float, default=0.50)
    parser.add_argument("--sl-pct", type=float, default=0.20)
    parser.add_argument("--output-dir", type=Path, default=Path("docs/session_reports"))
    parser.add_argument(
        "--fetch-missing-ohlcv",
        action="store_true",
        help="Fetch uncached 1m OHLCV in no-cache mode for post-exit proxy reconstruction",
    )
    parser.add_argument("--json", action="store_true", help="Print report JSON to stdout and skip writing files")
    args = parser.parse_args()

    entries = args.entries or _default_entries_file()
    if not entries or not entries.is_file():
        print("No entries.jsonl found. Pass --entries /path/to/entries.jsonl", file=sys.stderr)
        return 1

    report = build_report(
        entries,
        tp_pct=float(args.tp_pct),
        sl_pct=float(args.sl_pct),
        fetch_missing_ohlcv=bool(args.fetch_missing_ohlcv),
    )
    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    json_path, md_path = write_outputs(report, args.output_dir)
    print(_markdown_table(report))
    print(f"Wrote JSON: {json_path.resolve()}")
    print(f"Wrote Markdown: {md_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
