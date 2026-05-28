from src.analysis.calibration_buckets import side_source_bucket
from src.analysis.lane_identity import build_lane_metadata


def test_bitcoin_lane_family_splits_by_resolver_path():
    meta = build_lane_metadata(
        strategy="bitcoin",
        window_size="15m",
        action="BUY_NO",
        direction="DOWN",
        side_source="btc_quant_disagree_flip",
        resolver_path="htf_bullish__side_long__quant_short",
        htf_bias="BULLISH",
    )

    assert meta["entry_family"] == "htf_bullish_side_long_quant_short"
    assert (
        meta["lane_id"]
        == "bitcoin|15m|down|bullish|htf_bullish_side_long_quant_short"
    )


def test_alt_5m_downside_lane_family_splits_by_side_source():
    meta = build_lane_metadata(
        strategy="doge_macro",
        window_size="5m",
        action="BUY_NO",
        direction="DOWN",
        side_source="bearish_dip_default",
        primary_htf_bias="BEARISH",
        alt_htf_bias="BEARISH",
        btc_1h_regime="BULL",
    )

    assert meta["entry_family"] == "bearish_dip_default"
    assert (
        meta["lane_id"]
        == "doge_macro|5m|down|bearish__bearish__bull|bearish_dip_default"
    )


def test_non_5m_alt_downside_keeps_existing_standard_family():
    meta = build_lane_metadata(
        strategy="doge_macro",
        window_size="15m",
        action="BUY_NO",
        direction="DOWN",
        side_source="bearish_dip_default",
        primary_htf_bias="BEARISH",
        alt_htf_bias="BEARISH",
        btc_1h_regime="BULL",
    )

    assert meta["entry_family"] == "standard"


def test_uniform_bias_family_is_preserved_outside_legacy_5m_split():
    meta = build_lane_metadata(
        strategy="eth_macro",
        window_size="15m",
        action="BUY_YES",
        direction="UP",
        side_source="eth_15m_native",
        primary_htf_bias="BULLISH",
        alt_htf_bias="BULLISH",
        btc_1h_regime="BEAR",
    )

    assert meta["entry_family"] == "eth_15m_native"
    assert meta["lane_id"] == "eth_macro|15m|up|bullish__bullish__bear|eth_15m_native"


def test_side_source_bucket_recognizes_uniform_bias_taxonomy():
    assert side_source_bucket("btc_5m_native") == "native"
    assert side_source_bucket("sol_5m_vs_slower") == "vs_slower"
    assert (
        side_source_bucket("eth_5m_neutral_fallback_15m") == "neutral_fallback"
    )
