"""
CTF Redeemer — Claims resolved Polymarket positions on-chain.

When a Polymarket market resolves, winning conditional tokens must be explicitly
redeemed via the CTF (Conditional Token Framework) contract to receive USDC back.
Without this step, profits remain locked in resolved tokens indefinitely.

In dry_run (paper) mode : logs what would be redeemed — no chain interaction.
In live mode            : submits redeemPositions() on Polygon via Web3.

IMPORTANT: This module is a no-op in dry_run=True mode. Paper trade PnL is
calculated separately by the resolution tracker. Redemption only matters for
real on-chain positions when dry_run=False.

Usage (wired in main.py):
    redeemer = CTFRedeemer(dry_run=is_paper, private_key=pk, rpc_url=rpc)
    # Passed into resolution_tracker.check_and_settle(ctf_redeemer=redeemer)
"""

import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Polygon contract addresses ────────────────────────────────────────────────

# Gnosis CTF contract deployed by Polymarket on Polygon
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

# USDC.e (bridged) — Polymarket collateral token on Polygon
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# Zero bytes32 — parentCollectionId for top-level (non-nested) conditions
ZERO_BYTES32 = b"\x00" * 32

# Minimal ABI — only the redeemPositions function is needed
CTF_ABI = [
    {
        "name": "redeemPositions",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "outputs": [],
    }
]

# Polymarket index sets:
#   YES = outcome index 0 → bit 0 set → uint 1
#   NO  = outcome index 1 → bit 1 set → uint 2
_INDEX_SETS = {"YES": [1], "NO": [2]}

# Gas limit for redeemPositions — typically ~80k, 200k is safe ceiling
_GAS_LIMIT = 200_000

REDEEMED_LOG = Path(__file__).resolve().parent.parent.parent / "data" / "ctf_redeemed.jsonl"


class CTFRedeemer:
    """Redeems resolved Polymarket CTF positions.

    Safe to instantiate always — dry_run=True needs no credentials/Web3.
    In live mode, Web3 is initialized lazily on first real redemption attempt.

    Args:
        dry_run:     If True (paper trading), log only — never touch the chain.
        private_key: Wallet private key for signing redemption transactions.
        rpc_url:     Polygon RPC URL. Defaults to public polygon-rpc.com.
    """

    def __init__(
        self,
        dry_run: bool = True,
        private_key: Optional[str] = None,
        rpc_url: Optional[str] = None,
    ):
        self.dry_run = dry_run
        self._rpc_url = rpc_url or "https://polygon-rpc.com"
        self._private_key = private_key  # held only until _init_web3 consumes it

        self._nonce_lock = threading.Lock()
        self._next_nonce: Optional[int] = None

        # Prevent double-redemption across restarts.
        self._redeemed: set = self._load_redeemed()  # {(condition_id, outcome)}

        # Web3 objects — None until needed (lazy init)
        self._w3 = None
        self._account = None
        self._contract = None
        self._live_ready = False

        if not dry_run and private_key:
            self._init_web3()

    # ── Initialisation ────────────────────────────────────────────────────────

    def _init_web3(self):
        """Connect to Polygon and prepare the CTF contract handle."""
        try:
            from web3 import Web3

            self._w3 = Web3(Web3.HTTPProvider(self._rpc_url))
            if not self._w3.is_connected():
                logger.error(
                    "[CTFRedeemer] Cannot connect to Polygon RPC (%s) — "
                    "redemption disabled. Check RPC_URL env var.",
                    self._rpc_url,
                )
                self._w3 = None
                return

            if self._private_key:
                self._account = self._w3.eth.account.from_key(self._private_key)
                # Clear plaintext key immediately
                self._private_key = None

                self._contract = self._w3.eth.contract(
                    address=Web3.to_checksum_address(CTF_ADDRESS),
                    abi=CTF_ABI,
                )
                self._live_ready = True
                logger.info(
                    "[CTFRedeemer] Live mode ready | wallet=%s... | rpc=%s",
                    self._account.address[:10],
                    self._rpc_url,
                )
            else:
                logger.warning("[CTFRedeemer] No private key supplied — redemption disabled")

        except ImportError:
            logger.error("[CTFRedeemer] web3 package not installed — pip install web3>=6")
        except Exception as exc:
            logger.error("[CTFRedeemer] Web3 init failed: %s", exc)
            self._w3 = None

    def _load_redeemed(self) -> set:
        redeemed = set()
        if not REDEEMED_LOG.exists():
            return redeemed
        try:
            with REDEEMED_LOG.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        redeemed.add((rec["condition_id"].lower(), rec["outcome"].upper()))
                    except (json.JSONDecodeError, KeyError, AttributeError):
                        continue
        except OSError as e:
            logger.warning("[CTFRedeemer] Could not load %s: %s", REDEEMED_LOG, e)
        return redeemed

    def _persist_redemption(self, condition_id: str, outcome: str, tx_hash: str) -> None:
        rec = {
            "condition_id": condition_id,
            "outcome": outcome.upper(),
            "tx_hash": tx_hash,
            "ts": int(time.time()),
        }
        try:
            REDEEMED_LOG.parent.mkdir(parents=True, exist_ok=True)
            with REDEEMED_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, sort_keys=True) + "\n")
        except OSError as e:
            logger.warning("[CTFRedeemer] Could not persist redemption %s: %s", condition_id[:14], e)

    def _get_next_nonce(self) -> int:
        with self._nonce_lock:
            if self._next_nonce is None:
                self._next_nonce = self._w3.eth.get_transaction_count(
                    self._account.address, "pending"
                )
            nonce = self._next_nonce
            self._next_nonce += 1
            return nonce

    def _fee_fields(self) -> dict:
        try:
            fee_history = self._w3.eth.fee_history(1, "latest")
            base_fees = fee_history.get("baseFeePerGas") or []
            base_fee = int(base_fees[-1]) if base_fees else int(self._w3.eth.gas_price)
            priority_fee = int(self._w3.to_wei(30, "gwei"))
            return {
                "type": 2,
                "maxFeePerGas": base_fee * 2 + priority_fee,
                "maxPriorityFeePerGas": priority_fee,
            }
        except Exception:
            return {"gasPrice": self._w3.eth.gas_price}

    @staticmethod
    def _receipt_value(receipt, key: str):
        if isinstance(receipt, dict):
            return receipt.get(key)
        return getattr(receipt, key, None)

    # ── Public API ────────────────────────────────────────────────────────────

    def redeem(
        self,
        condition_id: Optional[str],
        outcome_won: str,
        market_question: str = "",
    ) -> bool:
        """Redeem a resolved position.

        Only redeems the WINNING side (losing tokens are worthless — no tx needed).

        Args:
            condition_id:    Hex condition ID from Polymarket market data (may be None
                             for older positions logged before we started capturing it).
            outcome_won:     "YES" or "NO" — the side that resolved.
            market_question: Human-readable label for log messages only.

        Returns:
            True  — redemption submitted (live) or logged (dry_run).
            False — skipped (no condition_id, already redeemed, or error).
        """
        if not condition_id:
            logger.debug("[CTFRedeemer] No condition_id — skipping redemption for '%s'", market_question[:50])
            return False

        key = (condition_id.lower(), outcome_won.upper())
        if key in self._redeemed:
            logger.debug("[CTFRedeemer] Already redeemed %s %s", condition_id[:12], outcome_won)
            return False

        index_sets = _INDEX_SETS.get(outcome_won.upper())
        if not index_sets:
            logger.warning("[CTFRedeemer] Unknown outcome_won=%r — skipping", outcome_won)
            return False

        if self.dry_run:
            logger.info(
                "[CTFRedeemer] [DRY RUN] Would redeem %s tokens | '%s' | conditionId=%s...",
                outcome_won,
                market_question[:60],
                condition_id[:14],
            )
            self._redeemed.add(key)
            return True

        return self._submit_redemption(condition_id, index_sets, outcome_won, market_question, key)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _submit_redemption(
        self,
        condition_id: str,
        index_sets: list,
        outcome_won: str,
        market_question: str,
        cache_key: tuple,
    ) -> bool:
        """Build, sign, and send the redeemPositions transaction."""
        if not self._live_ready:
            logger.error(
                "[CTFRedeemer] Live mode not ready — cannot redeem '%s'. "
                "Ensure WALLET_PRIVATE_KEY and RPC_URL are set.",
                market_question[:50],
            )
            return False

        try:
            from web3 import Web3

            condition_bytes = Web3.to_bytes(hexstr=condition_id)

            # Build transaction
            fn = self._contract.functions.redeemPositions(
                Web3.to_checksum_address(USDC_ADDRESS),
                ZERO_BYTES32,
                condition_bytes,
                index_sets,
            )
            tx = fn.build_transaction(
                {
                    "from": self._account.address,
                    "gas": _GAS_LIMIT,
                    "nonce": self._get_next_nonce(),
                    **self._fee_fields(),
                }
            )

            signed = self._account.sign_transaction(tx)
            tx_hash = self._w3.eth.send_raw_transaction(signed.raw_transaction)
            tx_hash_hex = tx_hash.hex()

            logger.info(
                "[CTFRedeemer] Submitted %s redemption | '%s' | conditionId=%s... | tx=%s... — waiting for confirmation",
                outcome_won,
                market_question[:60],
                condition_id[:14],
                tx_hash_hex[:18],
            )
            receipt = self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            if self._receipt_value(receipt, "status") != 1:
                logger.error(
                    "[CTFRedeemer] Tx reverted | conditionId=%s... | tx=%s... | block=%s",
                    condition_id[:14],
                    tx_hash_hex[:18],
                    self._receipt_value(receipt, "blockNumber"),
                )
                return False

            logger.info(
                "[CTFRedeemer] Confirmed %s redemption | gas=%s | block=%s | tx=%s...",
                outcome_won,
                self._receipt_value(receipt, "gasUsed"),
                self._receipt_value(receipt, "blockNumber"),
                tx_hash_hex[:18],
            )
            self._redeemed.add(cache_key)
            self._persist_redemption(condition_id, outcome_won, tx_hash_hex)
            return True

        except Exception as exc:
            self._next_nonce = None
            logger.error(
                "[CTFRedeemer] Redemption failed for conditionId=%s: %s",
                condition_id[:14],
                exc,
            )
            return False
