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


class TestBacktestETHLoadsBTCContext(unittest.TestCase):
    def test_eth_run_passes_btc_context_to_engine(self):
        mod = _load_run_backtest_crypto_module()

        fake_df = pd.DataFrame(
            columns=["open_time", "close_time", "open", "high", "low", "close", "volume"]
        )

        def _bars(count, freq):
            ts = pd.date_range("2025-01-01T00:00:00Z", periods=count, freq=freq)
            return pd.DataFrame(
                {
                    "open_time": ts,
                    "close_time": ts,
                    "open": 1.0,
                    "high": 1.0,
                    "low": 1.0,
                    "close": 1.0,
                    "volume": 1.0,
                }
            )

        def fake_load_all(self, symbol, start_date, end_date):
            if symbol == "ETH":
                return {"1m": fake_df, "5m": fake_df, "15m": fake_df, "1h": _bars(60, "1h")}
            return {"1m": fake_df, "5m": fake_df, "15m": fake_df, "4h": _bars(60, "4h")}

        captured = {}

        class FakeEngine:
            def __init__(self, config, initial_bankroll):
                pass

            def run(self, **kwargs):
                captured.update(kwargs)
                from src.backtest.updown_engine import UpdownBacktestResult
                return UpdownBacktestResult(
                    symbol="ETH",
                    window_size=5,
                    start_date="2025-01-01",
                    end_date="2025-01-02",
                    initial_bankroll=500.0,
                    final_bankroll=500.0,
                    trades=[],
                    windows_scanned=1,
                    windows_entered=0,
                    wins=0,
                    losses=0,
                )

        with patch.object(OHLCVLoader, "load_all", new=fake_load_all), \
             patch.object(mod, "UpdownBacktestEngine", new=FakeEngine), \
             patch.object(mod, "save_report", return_value=Path("dummy.json")):
            with patch("sys.argv", ["run_backtest_crypto.py", "--symbol", "ETH", "--window", "5", "--start", "2025-01-01", "--end", "2025-01-02"]):
                rc = mod.main()
        self.assertEqual(rc, 0)
        self.assertEqual(captured.get("symbol"), "ETH")
        self.assertIsNotNone(captured.get("btc_data"))

    def test_eth_run_passes_oracle_history_to_engine(self):
        mod = _load_run_backtest_crypto_module()

        fake_df = pd.DataFrame(
            columns=["open_time", "close_time", "open", "high", "low", "close", "volume"]
        )
        oracle_df = pd.DataFrame(
            {
                "updated_at": pd.to_datetime(["2025-01-01T00:00:00Z"], utc=True),
                "price": [2000.0],
                "round_id": [1],
                "network": ["polygon"],
                "address": ["0xfeed"],
            }
        )

        def _bars(count, freq):
            ts = pd.date_range("2025-01-01T00:00:00Z", periods=count, freq=freq)
            return pd.DataFrame(
                {
                    "open_time": ts,
                    "close_time": ts,
                    "open": 1.0,
                    "high": 1.0,
                    "low": 1.0,
                    "close": 1.0,
                    "volume": 1.0,
                }
            )

        def fake_load_all(self, symbol, start_date, end_date):
            if symbol == "ETH":
                return {"1m": fake_df, "5m": fake_df, "15m": fake_df, "1h": _bars(60, "1h")}
            return {"1m": fake_df, "5m": fake_df, "15m": fake_df, "4h": _bars(60, "4h")}

        captured = {}

        class FakeEngine:
            def __init__(self, config, initial_bankroll):
                pass

            def run(self, **kwargs):
                captured.update(kwargs)
                from src.backtest.updown_engine import UpdownBacktestResult
                return UpdownBacktestResult(
                    symbol="ETH",
                    window_size=5,
                    start_date="2025-01-01",
                    end_date="2025-01-02",
                    initial_bankroll=500.0,
                    final_bankroll=500.0,
                    trades=[],
                    windows_scanned=1,
                    windows_entered=0,
                    wins=0,
                    losses=0,
                )

        with patch.object(OHLCVLoader, "load_all", new=fake_load_all), \
             patch.object(mod, "UpdownBacktestEngine", new=FakeEngine), \
             patch.object(mod, "save_report", return_value=Path("dummy.json")), \
             patch.object(mod.OracleHistoryLoader, "load_history", return_value=oracle_df):
            with patch("sys.argv", ["run_backtest_crypto.py", "--symbol", "ETH", "--window", "5", "--start", "2025-01-01", "--end", "2025-01-02"]):
                rc = mod.main()
        self.assertEqual(rc, 0)
        self.assertIsNotNone(captured.get("oracle_history"))


class TestUpdownBacktestSplit(unittest.TestCase):
    def test_split_preserves_nonzero_windows_scanned(self):
        from src.backtest.updown_engine import UpdownBacktestResult, UpdownTrade

        trades = [
            UpdownTrade(
                window_open=pd.Timestamp("2025-01-02T00:00:00Z"),
                window_close=pd.Timestamp("2025-01-02T00:15:00Z"),
                symbol="ETH",
                window_size=15,
                action="BUY_YES",
                htf_bias="BULLISH",
                ltf_confirmed=True,
                ltf_strength=0.6,
                entry_price=0.5,
                fill_price=0.5,
                size=50.0,
                edge=0.08,
                confidence=0.6,
                outcome="WIN",
                exit_price=1.0,
                pnl=25.0,
                slip=0.0,
            ),
            UpdownTrade(
                window_open=pd.Timestamp("2025-01-05T00:00:00Z"),
                window_close=pd.Timestamp("2025-01-05T00:15:00Z"),
                symbol="ETH",
                window_size=15,
                action="BUY_NO",
                htf_bias="BEARISH",
                ltf_confirmed=True,
                ltf_strength=0.6,
                entry_price=0.5,
                fill_price=0.5,
                size=50.0,
                edge=0.08,
                confidence=0.6,
                outcome="LOSS",
                exit_price=0.0,
                pnl=-25.0,
                slip=0.0,
            ),
        ]
        result = UpdownBacktestResult(
            symbol="ETH",
            window_size=15,
            start_date="2025-01-01",
            end_date="2025-01-07",
            initial_bankroll=500.0,
            final_bankroll=500.0,
            trades=trades,
            windows_scanned=672,
            windows_entered=2,
            wins=1,
            losses=1,
        )
        train, test = result.split("2025-01-04")
        self.assertGreater(train.windows_scanned, 0)
        self.assertGreater(test.windows_scanned, 0)
        self.assertEqual(train.windows_entered, 1)
        self.assertEqual(test.windows_entered, 1)


if __name__ == "__main__":
    unittest.main()
