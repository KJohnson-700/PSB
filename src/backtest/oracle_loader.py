"""Historical Chainlink oracle loader for backtest replay.

Loads and caches timestamped reference prices from the same Chainlink feeds used
by the live crypto strategies. The cache is local and append-only at the file
level: each symbol gets a JSONL file under ``data/backtest/oracle``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

from src.analysis.sol_btc_service import ORACLE_FEEDS, SOLBTCService

logger = logging.getLogger(__name__)

CHAINLINK_HISTORY_ABI = [
    {
        "inputs": [],
        "name": "latestRoundData",
        "outputs": [
            {"internalType": "uint80", "name": "roundId", "type": "uint80"},
            {"internalType": "int256", "name": "answer", "type": "int256"},
            {"internalType": "uint256", "name": "startedAt", "type": "uint256"},
            {"internalType": "uint256", "name": "updatedAt", "type": "uint256"},
            {"internalType": "uint80", "name": "answeredInRound", "type": "uint80"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "uint80", "name": "_roundId", "type": "uint80"}],
        "name": "getRoundData",
        "outputs": [
            {"internalType": "uint80", "name": "roundId", "type": "uint80"},
            {"internalType": "int256", "name": "answer", "type": "int256"},
            {"internalType": "uint256", "name": "startedAt", "type": "uint256"},
            {"internalType": "uint256", "name": "updatedAt", "type": "uint256"},
            {"internalType": "uint80", "name": "answeredInRound", "type": "uint80"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"internalType": "uint8", "name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
]


@dataclass(frozen=True)
class OracleFeedSpec:
    symbol: str
    network: str
    address: str


class OracleHistoryLoader:
    """Load historical oracle observations for backtest replay."""

    def __init__(self, cache_dir: Optional[Path] = None, max_rounds: int = 50000):
        root = Path(__file__).resolve().parent.parent.parent
        self.cache_dir = cache_dir or (root / "data" / "backtest" / "oracle")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.max_rounds = max_rounds
        self._svc = SOLBTCService()

    @staticmethod
    def normalize_symbol(symbol: str) -> str:
        raw = (symbol or "").upper()
        if raw.endswith("USDT"):
            return raw
        return f"{raw}USDT"

    def resolve_feed(self, symbol: str) -> Optional[OracleFeedSpec]:
        normalized = self.normalize_symbol(symbol)
        feed = ORACLE_FEEDS.get(normalized)
        if not feed:
            return None
        network, address = feed
        return OracleFeedSpec(symbol=normalized, network=network, address=address)

    def load_history(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        spec = self.resolve_feed(symbol)
        if spec is None:
            return pd.DataFrame(columns=["updated_at", "price", "round_id", "network", "address"])

        start_ts = pd.Timestamp(datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc))
        end_ts = pd.Timestamp(
            datetime.strptime(end_date, "%Y-%m-%d").replace(
                hour=23, minute=59, second=59, tzinfo=timezone.utc
            )
        )
        cache = self._load_cache(spec)

        history = self._slice_history(cache, start_ts, end_ts)
        if self._cache_covers(history, start_ts, end_ts):
            return history

        fetched = self._fetch_history(spec, start_ts - pd.Timedelta(days=1), end_ts)
        if fetched.empty:
            return history

        merged = self._merge(cache, fetched)
        self._save_cache(spec, merged)
        return self._slice_history(merged, start_ts, end_ts)

    def _cache_path(self, spec: OracleFeedSpec) -> Path:
        return self.cache_dir / f"{spec.symbol.lower()}_{spec.network}_chainlink.jsonl"

    def _load_cache(self, spec: OracleFeedSpec) -> pd.DataFrame:
        path = self._cache_path(spec)
        if not path.exists():
            return pd.DataFrame(columns=["updated_at", "price", "round_id", "network", "address"])

        rows: List[dict] = []
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError as exc:
            logger.warning(f"oracle_loader: failed reading cache {path}: {exc}")
            return pd.DataFrame(columns=["updated_at", "price", "round_id", "network", "address"])

        if not rows:
            return pd.DataFrame(columns=["updated_at", "price", "round_id", "network", "address"])

        df = pd.DataFrame(rows)
        df["updated_at"] = pd.to_datetime(df["updated_at"], utc=True)
        df = df.sort_values("updated_at").drop_duplicates(subset=["round_id"], keep="last")
        return df.reset_index(drop=True)

    def _save_cache(self, spec: OracleFeedSpec, df: pd.DataFrame) -> None:
        path = self._cache_path(spec)
        try:
            with path.open("w", encoding="utf-8") as fh:
                for row in df.sort_values("updated_at").to_dict("records"):
                    payload = dict(row)
                    payload["updated_at"] = pd.Timestamp(payload["updated_at"]).isoformat()
                    fh.write(json.dumps(payload) + "\n")
        except OSError as exc:
            logger.warning(f"oracle_loader: failed writing cache {path}: {exc}")

    @staticmethod
    def _merge(existing: pd.DataFrame, fetched: pd.DataFrame) -> pd.DataFrame:
        if existing.empty:
            return fetched.reset_index(drop=True)
        if fetched.empty:
            return existing.reset_index(drop=True)
        df = pd.concat([existing, fetched], ignore_index=True)
        df = df.sort_values("updated_at").drop_duplicates(subset=["round_id"], keep="last")
        return df.reset_index(drop=True)

    @staticmethod
    def _slice_history(df: pd.DataFrame, start_ts: pd.Timestamp, end_ts: pd.Timestamp) -> pd.DataFrame:
        if df.empty:
            return df
        mask = (df["updated_at"] >= start_ts) & (df["updated_at"] <= end_ts)
        sliced = df.loc[mask].copy()
        earlier = df.loc[df["updated_at"] < start_ts].tail(1)
        if not earlier.empty:
            sliced = pd.concat([earlier, sliced], ignore_index=True)
        return sliced.sort_values("updated_at").reset_index(drop=True)

    @staticmethod
    def _cache_covers(df: pd.DataFrame, start_ts: pd.Timestamp, end_ts: pd.Timestamp) -> bool:
        if df.empty:
            return False
        first = pd.Timestamp(df["updated_at"].iloc[0])
        last = pd.Timestamp(df["updated_at"].iloc[-1])
        return first <= start_ts and last >= end_ts

    def _fetch_history(
        self,
        spec: OracleFeedSpec,
        start_ts: pd.Timestamp,
        end_ts: pd.Timestamp,
    ) -> pd.DataFrame:
        try:
            from web3 import Web3
        except Exception as exc:
            logger.warning(f"oracle_loader: web3 unavailable for {spec.symbol}: {exc}")
            return pd.DataFrame(columns=["updated_at", "price", "round_id", "network", "address"])

        rows: List[dict] = []
        rpc_urls = self._svc._chainlink_rpcs_for_network(spec.network)
        for rpc_url in rpc_urls:
            try:
                w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
                contract = w3.eth.contract(
                    address=w3.to_checksum_address(spec.address),
                    abi=CHAINLINK_HISTORY_ABI,
                )
                decimals = contract.functions.decimals().call()
                latest_round_id, *_ = contract.functions.latestRoundData().call()
                rows = self._walk_rounds(
                    contract=contract,
                    decimals=decimals,
                    latest_round_id=int(latest_round_id),
                    start_ts=start_ts,
                    end_ts=end_ts,
                    network=spec.network,
                    address=spec.address,
                )
                if rows:
                    break
            except Exception as exc:
                logger.debug(
                    f"oracle_loader: RPC {spec.network}:{rpc_url} failed for {spec.symbol}: {exc}"
                )
                continue

        if not rows:
            logger.warning(f"oracle_loader: no oracle history fetched for {spec.symbol}")
            return pd.DataFrame(columns=["updated_at", "price", "round_id", "network", "address"])

        df = pd.DataFrame(rows)
        df["updated_at"] = pd.to_datetime(df["updated_at"], utc=True)
        df = df.sort_values("updated_at").drop_duplicates(subset=["round_id"], keep="last")
        return df.reset_index(drop=True)

    def _walk_rounds(
        self,
        contract,
        decimals: int,
        latest_round_id: int,
        start_ts: pd.Timestamp,
        end_ts: pd.Timestamp,
        network: str,
        address: str,
    ) -> List[dict]:
        rows: List[dict] = []
        seen_updates = set()
        round_id = latest_round_id
        scanned = 0
        start_floor = int(start_ts.timestamp())
        end_ceiling = int((end_ts + pd.Timedelta(days=1)).timestamp())

        while round_id > 0 and scanned < self.max_rounds:
            scanned += 1
            try:
                current_round_id, answer, _started_at, updated_at, _answered = (
                    contract.functions.getRoundData(round_id).call()
                )
            except Exception:
                round_id -= 1
                continue

            if updated_at <= 0 or answer <= 0:
                round_id -= 1
                continue

            updated_at = int(updated_at)
            if updated_at in seen_updates:
                round_id -= 1
                continue
            seen_updates.add(updated_at)

            if updated_at > end_ceiling:
                round_id -= 1
                continue

            rows.append(
                {
                    "updated_at": datetime.fromtimestamp(updated_at, tz=timezone.utc).isoformat(),
                    "price": float(answer) / (10 ** decimals),
                    "round_id": int(current_round_id),
                    "network": network,
                    "address": address,
                }
            )

            if updated_at < start_floor:
                break
            round_id -= 1

        return rows
