"""Tests that pre-resolver-reject records get tagged with the dedicated
lane_family so they bucket separately from real standard-family attempts.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.analysis.rejected_candidate_log import log_rejected_candidate


@dataclass
class _FakeMarket:
    id: str = "test_market"
    question: str = "Will X happen?"
    slug: str = "test-x"
    end_date: Optional[datetime] = None
    token_id_yes: str = "tyes"
    token_id_no: str = "tno"
    no_price: float = 0.5


def _read_records(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_pre_resolver_reject_gets_tagged(tmp_path: Path) -> None:
    """Reject with no resolver_path / side_source / lane_family →
    written with lane_family='pre_resolver_reject'."""
    log_path = tmp_path / "rej.jsonl"
    end = datetime(2026, 6, 1, tzinfo=timezone.utc)
    market = _FakeMarket(end_date=end)
    ok = log_rejected_candidate(
        strategy="eth_macro",
        window="15m",
        side="DOWN",
        action="BUY_NO",
        reason="iql_15m_reject",
        market=market,
        yes_price=0.48,
        est_prob_up=0.55,
        htf_bias="BEARISH",
        context={"eth_5m_adj": 0.01, "min_required": 0.02},
        log_path=log_path,
    )
    assert ok
    rows = _read_records(log_path)
    assert len(rows) == 1
    assert rows[0]["lane_family"] == "pre_resolver_reject"


def test_resolver_classified_record_not_tagged(tmp_path: Path) -> None:
    """Reject WITH resolver_path → lane_family flows from resolver, not
    the pre-resolver fallback."""
    log_path = tmp_path / "rej.jsonl"
    end = datetime(2026, 6, 1, tzinfo=timezone.utc)
    market = _FakeMarket(end_date=end)
    ok = log_rejected_candidate(
        strategy="bitcoin",
        window="5m",
        side="DOWN",
        action="BUY_NO",
        reason="lane_min_edge",
        market=market,
        yes_price=0.34,
        est_prob_up=0.52,
        htf_bias="BEARISH",
        resolver_path="htf_bearish__side_short",
        side_source="btc_htf_bias",
        lane_family="htf_bearish_side_short",
        log_path=log_path,
    )
    assert ok
    rows = _read_records(log_path)
    assert len(rows) == 1
    assert rows[0]["lane_family"] == "htf_bearish_side_short"
    assert rows[0]["lane_family"] != "pre_resolver_reject"


def test_explicit_lane_family_overrides_pre_resolver(tmp_path: Path) -> None:
    """If caller passes lane_family explicitly, the pre-resolver guard
    never fires even when resolver_path/side_source are absent."""
    log_path = tmp_path / "rej.jsonl"
    end = datetime(2026, 6, 1, tzinfo=timezone.utc)
    market = _FakeMarket(end_date=end)
    ok = log_rejected_candidate(
        strategy="sol_macro",
        window="5m",
        side="DOWN",
        action="BUY_NO",
        reason="lane_min_edge",
        market=market,
        yes_price=0.4,
        est_prob_up=0.5,
        lane_family="bearish_dip_default",
        log_path=log_path,
    )
    assert ok
    rows = _read_records(log_path)
    assert rows[0]["lane_family"] == "bearish_dip_default"
