from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _side_policy_blocks(path: str) -> list[str]:
    text = (ROOT / path).read_text()
    marker = "if _side_policy_active and _side_policy_flat_edge > 0.0:"
    blocks = []
    start = 0
    while True:
        idx = text.find(marker, start)
        if idx == -1:
            break
        blocks.append(text[idx : idx + 450])
        start = idx + len(marker)
    return blocks


def test_favorite_policy_does_not_zero_lane_min_edge_gate():
    for path in (
        "src/strategies/sol_macro.py",
        "src/strategies/eth_macro.py",
        "src/strategies/bitcoin.py",
    ):
        blocks = _side_policy_blocks(path)
        assert blocks, f"missing side-policy admission block in {path}"
        assert all("effective_min_edge = 0.0" not in block for block in blocks)


def test_btc_favorite_policy_tags_side_source():
    text = (ROOT / "src/strategies/bitcoin.py").read_text()
    apply_idx = text.index('allowed_side = _sp["side"]')
    next_chunk = text[apply_idx : apply_idx + 160]

    assert 'side_source = (side_source or "") + "+" + str(_sp.get("tag") or "")' in next_chunk
