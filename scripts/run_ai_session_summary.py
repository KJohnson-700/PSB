"""Off-cycle orchestrator that runs all AI narrators against the latest session
and appends a markdown block to the configured output file.

Invoke manually after a paper-trading session, or schedule on a cron:
    python3 scripts/run_ai_session_summary.py

Each narrator is independently gated by ai.session_summary in config/settings.yaml.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.analysis.ai_agent import AIAgent  # noqa: E402
from src.analysis.ai_narrators import (  # noqa: E402
    aggregate_skip_exit_distributions,
    detect_calibration_drift,
    explain_strategy_conflict,
    load_closed_trades_from_summary,
    load_shadow_records,
    summarize_skip_exit_reasons,
    summarize_underperformance,
)
from src.execution.trade_journal import TradeJournal  # noqa: E402

logger = logging.getLogger("ai_session_summary")


def _load_config(repo_root: Path) -> Dict[str, Any]:
    cfg_path = repo_root / "config" / "settings.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _latest_session_dir(repo_root: Path) -> Path | None:
    journal_dir = repo_root / "data" / "paper_trades"
    if not journal_dir.exists():
        return None
    sessions = sorted(
        [p for p in journal_dir.iterdir() if p.is_dir() and TradeJournal.session_dir_has_activity(p)],
        key=lambda p: p.name,
        reverse=True,
    )
    return sessions[0] if sessions else None


def _load_underperformance_report(repo_root: Path) -> Dict[str, Any] | None:
    """Best-effort: find a recent saved underperformance report markdown.
    If none exists, return None and the narrator will be skipped."""
    candidates = sorted(
        (repo_root / "docs" / "session_reports").glob("*underperformance*.md"),
        reverse=True,
    )
    if not candidates:
        return None
    try:
        return {"_raw_markdown": candidates[0].read_text(encoding="utf-8", errors="replace")}
    except OSError:
        return None


async def _run(repo_root: Path) -> int:
    cfg = _load_config(repo_root)
    summary_cfg = ((cfg.get("ai") or {}).get("session_summary") or {})
    if not summary_cfg.get("enabled", False):
        logger.warning("ai.session_summary.enabled is false — nothing to do.")
        return 0

    out_path = Path(summary_cfg.get(
        "output_path", "projects/polymarket-bot/strategy-log/_ai_summary.md"
    ))
    if not out_path.is_absolute():
        out_path = repo_root / out_path

    timeout = float(summary_cfg.get("timeout_seconds", 30))

    ai_agent = AIAgent(cfg)
    if not ai_agent.is_available():
        logger.error("AIAgent is not available (check api keys / provider_chain).")
        return 1

    session_dir = _latest_session_dir(repo_root)
    if session_dir is None:
        logger.warning("No active session directory found; some narrators will be skipped.")

    blocks: List[str] = []
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    blocks.append(f"\n## AI session summary — {timestamp}\n")

    # 1. Underperformance
    if summary_cfg.get("include_underperformance", True):
        report = _load_underperformance_report(repo_root)
        if report:
            text = await summarize_underperformance(report, ai_agent, timeout=timeout)
            if text:
                blocks.append("### Underperformance\n" + text + "\n")

    # 2. Skip / exit reasons
    if summary_cfg.get("include_skip_exit_reasons", True) and session_dir:
        dist = aggregate_skip_exit_distributions(session_dir / "entries.jsonl")
        text = await summarize_skip_exit_reasons(
            dist.get("skip", {}), dist.get("exit", {}), ai_agent, timeout=timeout
        )
        if text:
            blocks.append("### Skip / exit reasons\n" + text + "\n")

    # 3. Calibration drift
    if summary_cfg.get("include_calibration_drift", True) and session_dir:
        shadow_path = repo_root / "data" / "logs" / "ai_pipeline" / "shadow_pipeline.jsonl"
        shadow = load_shadow_records(shadow_path)
        closed = load_closed_trades_from_summary(session_dir / "summary.json")
        text = await detect_calibration_drift(shadow, closed, ai_agent, timeout=timeout)
        if text:
            blocks.append("### Calibration drift\n" + text + "\n")

    # 4. Strategy conflict
    if summary_cfg.get("include_strategy_conflict", True) and session_dir:
        scan_summaries_path = session_dir / "scan_summaries.json"
        scan_summaries: Dict[str, Any] = {}
        if scan_summaries_path.exists():
            import json as _json
            try:
                with open(scan_summaries_path, encoding="utf-8") as f:
                    scan_summaries = _json.load(f) or {}
            except (OSError, ValueError):
                scan_summaries = {}
        if scan_summaries:
            text = await explain_strategy_conflict(scan_summaries, ai_agent, timeout=timeout)
            if text:
                blocks.append("### Strategy conflict\n" + text + "\n")

    if len(blocks) <= 1:
        logger.info("No narrators produced output; nothing appended.")
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "a", encoding="utf-8") as f:
        f.write("\n".join(blocks))
    logger.info("Appended %d narrator block(s) to %s", len(blocks) - 1, out_path)
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return asyncio.run(_run(REPO_ROOT))


if __name__ == "__main__":
    sys.exit(main())
