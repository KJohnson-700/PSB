"""Lane calibration helpers shared by live strategies and updown backtest replay."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from src.analysis.lane_calibration import DEFAULT_CALIBRATION_DIR, LaneCalibrator
from src.analysis.lane_identity import build_lane_metadata
from src.strategies.btc_updown_5m import edge_for_action


def lane_window_label(window_minutes: int) -> str:
    wm = int(window_minutes)
    if wm >= 45:
        return "1h"
    if wm == 30:
        return "30m"
    return f"{wm}m"


def build_lane_calibrator_for_replay(config: Dict[str, Any]) -> Optional[LaneCalibrator]:
    """Mirror ``PolyBot._build_lane_calibrator`` with backtest-safe posteriors path."""
    cal_cfg = config.get("lane_calibration") or {}
    if not bool(cal_cfg.get("enabled", False)):
        return None

    bt_cfg = config.get("backtest", {}) or {}
    bt_cal = bt_cfg.get("lane_calibration") or {}

    shadow = cal_cfg.get("shadow_mode", True)
    if bt_cal.get("shadow_mode") is not None:
        shadow = bool(bt_cal.get("shadow_mode"))

    path: Optional[Path] = None
    raw_path = bt_cal.get("posteriors_path")
    if raw_path:
        path = Path(raw_path)
    elif bool(bt_cal.get("seed_from_live", False)):
        path = DEFAULT_CALIBRATION_DIR / "lane_posteriors.json"
    else:
        path = DEFAULT_CALIBRATION_DIR / "lane_posteriors_backtest.json"

    try:
        return LaneCalibrator(path=path, shadow_mode=bool(shadow))
    except Exception:
        return LaneCalibrator(shadow_mode=True)


def calibrate_updown_est_prob(
    calibrator: Optional[LaneCalibrator],
    raw_est_prob: float,
    *,
    strategy: str,
    window_minutes: int,
    action: str,
    direction: str,
    htf_bias: Optional[str],
    signal_reason: str = "",
) -> Tuple[float, str]:
    if calibrator is None:
        return float(raw_est_prob), ""
    lane_meta = build_lane_metadata(
        strategy=strategy,
        window_size=lane_window_label(window_minutes),
        action=action,
        direction=direction,
        entry_leg=("NO" if action == "BUY_NO" else "YES"),
        side_source="btc_htf_bias",
        ai_used=False,
        reason=signal_reason,
        signal_reason=signal_reason,
        htf_bias=htf_bias,
    )
    lane_id = str(lane_meta.get("lane_id") or "").strip()
    if not lane_id:
        return float(raw_est_prob), ""
    return float(calibrator.calibrate(lane_id, raw_est_prob)), lane_id


def edge_from_raw_est_prob(
    calibrator: Optional[LaneCalibrator],
    raw_est_prob: float,
    yes_price: float,
    allowed_side: str,
    *,
    strategy: str,
    window_minutes: int,
    htf_bias: str,
    signal_reason: str = "",
) -> Tuple[float, str, float]:
    """Return ``(edge, lane_id, calibrated_est_prob)`` — same order as live sizing gates."""
    action = "BUY_YES" if allowed_side == "LONG" else "BUY_NO"
    direction = "UP" if allowed_side == "LONG" else "DOWN"
    cal_p, lane_id = calibrate_updown_est_prob(
        calibrator,
        raw_est_prob,
        strategy=strategy,
        window_minutes=window_minutes,
        action=action,
        direction=direction,
        htf_bias=htf_bias,
        signal_reason=signal_reason,
    )
    edge = edge_for_action(
        estimated_prob=cal_p,
        yes_price=yes_price,
        action=action,
    )
    return edge, lane_id, cal_p


def record_updown_calibration_close(
    calibrator: Optional[LaneCalibrator],
    *,
    lane_id: str,
    stated_est_prob: Optional[float],
    pnl: float,
    size: float,
    outcome: str,
) -> None:
    if calibrator is None or not lane_id:
        return
    notional = float(size)
    realized_pct = (float(pnl) / notional) if notional > 0 else 0.0
    calibrator.record(
        lane_id,
        stated_est_prob=stated_est_prob,
        realized_pct=realized_pct,
        win=(outcome == "WIN"),
    )
