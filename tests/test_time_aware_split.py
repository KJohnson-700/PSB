from datetime import datetime, timedelta, timezone

from src.analysis.time_aware_split import build_purged_time_folds, describe_folds


def test_build_purged_time_folds_excludes_nearby_train_points() -> None:
    base = datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc)
    timestamps = [base + timedelta(minutes=idx * 5) for idx in range(8)]

    folds = build_purged_time_folds(
        timestamps,
        n_splits=4,
        purge=timedelta(minutes=10),
        min_train_size=1,
    )

    assert len(folds) == 2
    assert folds[0].train_indices == [0, 1]
    assert folds[0].test_indices == [4, 5]
    assert folds[-1].test_indices == [6, 7]


def test_describe_folds_returns_iso_ranges() -> None:
    base = datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc)
    timestamps = [base + timedelta(minutes=idx * 5) for idx in range(6)]
    folds = build_purged_time_folds(
        timestamps,
        n_splits=3,
        purge=timedelta(minutes=5),
        min_train_size=1,
    )

    described = describe_folds(timestamps, folds)

    assert described[0]["fold"] == 1
    assert described[0]["train_start"] == timestamps[0].isoformat()
    assert described[0]["test_start"] == timestamps[2].isoformat()
