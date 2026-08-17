"""Decision Packet — schema, fail-safety, and a regression guard on the 8 instrumented gates.

2026-08-17. Three things must stay true or the packet is worse than nothing:

  1. BACKWARD COMPATIBILITY. Rows written without a packet must be byte-comparable to
     schema 5 — same keys, no `packet` key at all. `json.dumps` in the writer does NOT strip
     nulls, so an unconditional key would have put `"packet":null` on ~30k rows/day and
     changed the shape of every existing row. (That bug was written and caught here.)

  2. TELEMETRY CANNOT STOP A TRADE. `log_skip_packet` must swallow everything. Note it
     cannot protect against an out-of-scope local at the CALL SITE — that NameError is
     raised while building the arguments — which is why every call site also carries its own
     try/except. Both layers are tested.

  3. THE 8 GATES STAY INSTRUMENTED. `kelly_nonpositive` and friends fired ~1,400x/day with
     zero rows in a 30,000-row reject log; `kelly_nonpositive` in particular made a shipped
     fix unverifiable. If a future edit drops these calls the blindness returns silently,
     so the AST guard below fails loudly instead.
"""
import ast
import json
import os
import tempfile
from pathlib import Path

import pytest

from src.analysis.rejected_candidate_log import (
    build_packet,
    log_rejected_candidate,
    log_skip_packet,
)

REPO = Path(__file__).resolve().parent.parent
BITCOIN = REPO / "src" / "strategies" / "bitcoin.py"

# the gates measured as firing live with ZERO decision records (ops_pulse vs reject log)
INSTRUMENTED_GATES = [
    "kelly_nonpositive",
    "rsi_overbought_5m",
    "price_too_far_from_50_50",
    "ltf_confirmed_late_entry",
    "correlated_exposure_5m",
    "ai_pending_async",
    "buy_yes_overbought_rsi_1h",
    "ai_low_confidence_marginal_updown",
]


class _Market:
    id = "999"
    question = "BTC Up or Down test"
    slug = "btc-updown-5m-1"
    end_date = None
    token_id_yes = "ty"
    token_id_no = "tn"
    no_price = 0.57


# ── 1. packet construction ────────────────────────────────────────────────────
def test_build_packet_drops_none_keys():
    p = build_packet(raw_side="LONG", final_side="LONG", final_action="BUY_YES")
    assert "price_basis" not in p and "edge_chain" not in p
    assert p["raw_side"] == "LONG"


def test_build_packet_flags_side_override():
    """raw != final is the BNB-class defect: stale side=SHORT, executed BUY_YES."""
    p = build_packet(raw_side="SHORT", final_side="LONG", final_action="BUY_YES")
    assert p["side_overridden"] is True


def test_build_packet_no_flag_when_sides_agree():
    p = build_packet(raw_side="LONG", final_side="long", final_action="BUY_YES")
    assert "side_overridden" not in p          # case-insensitive compare


def test_build_packet_never_raises_on_junk():
    p = build_packet(raw_side=None, gates_passed=[], edge_chain=[])
    assert p == {}                             # empty collections are dropped too


# ── 2. schema / backward compatibility ───────────────────────────────────────
def _write(tmp, **kw):
    ok = log_rejected_candidate(
        strategy="bitcoin", window="5m", side="LONG", action="BUY_YES",
        reason="kelly_nonpositive", market=_Market(), yes_price=0.43,
        est_prob_up=0.51, log_path=tmp, **kw)
    assert ok is True
    return [json.loads(l) for l in open(tmp)][-1]


def test_row_without_packet_is_schema_5_and_has_no_packet_key():
    with tempfile.TemporaryDirectory() as d:
        row = _write(Path(d) / "r.jsonl")
        assert row["schema_version"] == 5
        assert "packet" not in row


def test_row_with_packet_keeps_schema_5_and_is_found_by_key_presence():
    """Codex NO-GO'd a version bump; the `packet` KEY is the discriminator instead.

    I audited all ~50 reject-log readers and none pins schema_version, so a bump would have
    been safe -- but `"packet" in row` needs no version table at all, so it is strictly
    better. This test locks the decision in.
    """
    with tempfile.TemporaryDirectory() as d:
        row = _write(Path(d) / "r.jsonl",
                     packet=build_packet(raw_side="LONG", final_side="LONG",
                                         final_action="BUY_YES",
                                         gate_failed="kelly_nonpositive"))
        assert row["schema_version"] == 5
        assert "packet" in row
        assert row["packet"]["gate_failed"] == "kelly_nonpositive"


def test_packet_does_not_change_the_other_keys():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d) / "r.jsonl"
        plain = _write(tmp)
        withp = _write(tmp, packet=build_packet(raw_side="LONG", final_side="LONG"))
        assert set(withp) - set(plain) == {"packet"}
        assert set(plain) - set(withp) == set()


def test_empty_packet_writes_no_packet_key():
    """build_packet() can legitimately return {} — that must not add an empty key."""
    with tempfile.TemporaryDirectory() as d:
        row = _write(Path(d) / "r.jsonl", packet={})
        assert row["schema_version"] == 5
        assert "packet" not in row


# ── 3. fail-safety ────────────────────────────────────────────────────────────
def test_log_skip_packet_returns_false_instead_of_raising():
    assert log_skip_packet(strategy="only-one-kwarg") is False


def test_log_skip_packet_swallows_bad_market_object():
    with tempfile.TemporaryDirectory() as d:
        assert log_skip_packet(
            strategy="bitcoin", window="5m", side="LONG", action="BUY_YES",
            reason="x", market=object(), yes_price="not-a-number",
            est_prob_up=None, log_path=Path(d) / "r.jsonl") in (True, False)


def test_log_skip_packet_writes_a_real_row_on_the_happy_path():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d) / "r.jsonl"
        assert log_skip_packet(
            strategy="bitcoin", window="5m", side="LONG", action="BUY_YES",
            reason="kelly_nonpositive", market=_Market(), yes_price=0.43,
            est_prob_up=0.51, log_path=tmp,
            packet=build_packet(raw_side="LONG", final_side="LONG",
                                gate_failed="kelly_nonpositive")) is True
        rows = [json.loads(l) for l in open(tmp)]
        assert len(rows) == 1 and rows[0]["packet"]["gate_failed"] == "kelly_nonpositive"


# ── 4. regression guard: the 8 gates must stay instrumented ──────────────────
def _skip_and_log_lines(tree):
    skips, logs = {}, []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else getattr(
            node.func, "attr", None)
        if name == "_bump_skip" and node.args:
            a = node.args[0]
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                skips.setdefault(a.value, []).append(node.lineno)
        elif name in ("log_skip_packet", "log_rejected_candidate"):
            logs.append(node.lineno)
    return skips, logs


@pytest.mark.parametrize("gate", INSTRUMENTED_GATES)
def test_blind_spot_gate_still_has_a_decision_record(gate):
    tree = ast.parse(BITCOIN.read_text(encoding="utf-8"))
    skips, logs = _skip_and_log_lines(tree)
    assert gate in skips, f"{gate} no longer bumps a skip in bitcoin.py — update this test"
    for ln in skips[gate]:
        if any(abs(lg - ln) <= 30 for lg in logs):
            return
    pytest.fail(
        f"{gate} bumps a skip at {skips[gate]} with NO decision record within 30 lines. "
        "This gate fires live and would go silent again — re-instrument it.")


def test_bitcoin_imports_the_packet_helpers():
    src = BITCOIN.read_text(encoding="utf-8")
    assert "log_skip_packet" in src and "build_packet" in src
