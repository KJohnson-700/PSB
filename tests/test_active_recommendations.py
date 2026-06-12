from src.analysis.active_recommendations import append_active_recommendation


def test_append_active_recommendation_prepends_entry(tmp_path):
    path = tmp_path / "ACTIVE_RECOMMENDATIONS.md"
    append_active_recommendation(
        source="test_source",
        title="First",
        body="first body",
        details={"artifact": "data/example.json"},
        path=path,
    )
    append_active_recommendation(
        source="test_source",
        title="Second",
        body="second body",
        path=path,
    )

    text = path.read_text(encoding="utf-8")
    assert "# PSB Active Recommendations" in text
    assert text.index("Second") < text.index("First")
    assert "**Source:** `test_source`" in text
    assert "**artifact:** `data/example.json`" in text
