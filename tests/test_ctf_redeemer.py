import json

from src.execution import ctf_redeemer as ctf_module
from src.execution.ctf_redeemer import CTFRedeemer


def test_ctf_redeemer_loads_persisted_redemptions(monkeypatch, tmp_path):
    log_path = tmp_path / "ctf_redeemed.jsonl"
    condition_id = "0x" + "ab" * 32
    log_path.write_text(
        json.dumps(
            {
                "condition_id": condition_id,
                "outcome": "NO",
                "tx_hash": "0xabc",
                "ts": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ctf_module, "REDEEMED_LOG", log_path)

    redeemer = CTFRedeemer(dry_run=True)

    assert redeemer.redeem(condition_id, "NO", "already redeemed") is False


def test_ctf_redeemer_persists_confirmed_redemption(monkeypatch, tmp_path):
    log_path = tmp_path / "ctf_redeemed.jsonl"
    monkeypatch.setattr(ctf_module, "REDEEMED_LOG", log_path)

    redeemer = CTFRedeemer(dry_run=True)
    condition_id = "0x" + "cd" * 32
    redeemer._persist_redemption(condition_id, "YES", "0xhash")

    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert records == [
        {
            "condition_id": condition_id,
            "outcome": "YES",
            "tx_hash": "0xhash",
            "ts": records[0]["ts"],
        }
    ]
