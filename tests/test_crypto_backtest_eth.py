"""Tests for ETH crypto updown backtest plumbing (no live Binance calls)."""

import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from src.backtest.ohlcv_loader import OHLCVLoader


def _load_run_backtest_crypto_module():
    root = Path(__file__).resolve().parent.parent
    path = root / "scripts" / "run_backtest_crypto.py"
    spec = importlib.util.spec_from_file_location("run_backtest_crypto", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestOHLCVLoaderETH(unittest.TestCase):
    def test_load_all_eth_uses_same_intervals_as_sol(self):
        """ETH shares SOL's HTF (1h) structure in UpdownBacktestEngine."""
        captured = []

        def fake_load(self, symbol, interval, start_date, end_date):
            captured.append((symbol, interval))
            return pd.DataFrame(
                columns=[
                    "open_time",
                    "close_time",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                ]
            )

        with patch.object(OHLCVLoader, "load", new=fake_load):
            OHLCVLoader(no_cache=True).load_all("ETH", "2025-01-01", "2025-01-07")

        self.assertEqual(
            captured,
            [
                ("ETHUSDT", "1m"),
                ("ETHUSDT", "5m"),
                ("ETHUSDT", "15m"),
                ("ETHUSDT", "1h"),
            ],
        )


class TestBacktestReportStrategyKey(unittest.TestCase):
    def test_save_report_maps_eth_to_eth_macro(self):
        save_report = _load_run_backtest_crypto_module().save_report
        from src.backtest.updown_engine import UpdownBacktestResult

        r = UpdownBacktestResult(
            symbol="ETH",
            window_size=15,
            start_date="2025-01-01",
            end_date="2025-01-07",
            initial_bankroll=500.0,
            final_bankroll=500.0,
            trades=[],
            windows_scanned=1,
            windows_entered=0,
            wins=0,
            losses=0,
        )
        path = save_report(r, {"1m": 0})
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload.get("strategy"), "eth_macro_15m")
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
