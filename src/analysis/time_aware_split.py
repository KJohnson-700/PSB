"""Simple time-aware split helpers for probability evaluation.

This module intentionally avoids external dependencies. The current use case is
small offline calibration/evaluation tooling where we need chronological folds
and a basic purge window to reduce overlap leakage between adjacent samples.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Sequence


@dataclass(frozen=True)
class TimeAwareFold:
    train_indices: list[int]
    test_indices: list[int]


def _ensure_sorted_datetimes(timestamps: Sequence[datetime]) -> list[datetime]:
    values = list(timestamps)
    if any(values[idx] > values[idx + 1] for idx in range(len(values) - 1)):
        raise ValueError("timestamps must be sorted ascending")
    return values


def build_purged_time_folds(
    timestamps: Sequence[datetime],
    *,
    n_splits: int = 5,
    purge: timedelta = timedelta(0),
    min_train_size: int = 1,
) -> list[TimeAwareFold]:
    """Return chronological folds with a backward purge window."""

    ordered = _ensure_sorted_datetimes(timestamps)
    n = len(ordered)
    if n == 0 or n_splits <= 1:
        return []

    folds: list[TimeAwareFold] = []
    base = n // n_splits
    rem = n % n_splits
    start = 0
    for split_idx in range(n_splits):
        size = base + (1 if split_idx < rem else 0)
        if size <= 0:
            continue
        test_start = start
        test_end = start + size
        start = test_end
        if test_start == 0:
            continue
        cutoff = ordered[test_start] - purge
        train_indices = [idx for idx in range(test_start) if ordered[idx] < cutoff]
        if len(train_indices) < min_train_size:
            continue
        test_indices = list(range(test_start, test_end))
        folds.append(TimeAwareFold(train_indices=train_indices, test_indices=test_indices))
    return folds


def describe_folds(
    timestamps: Sequence[datetime],
    folds: Iterable[TimeAwareFold],
) -> list[dict[str, object]]:
    """Summarize fold coverage for report surfaces."""

    ordered = _ensure_sorted_datetimes(timestamps)
    out: list[dict[str, object]] = []
    for idx, fold in enumerate(folds, start=1):
        train_ts = [ordered[i] for i in fold.train_indices]
        test_ts = [ordered[i] for i in fold.test_indices]
        out.append(
            {
                "fold": idx,
                "train_n": len(fold.train_indices),
                "test_n": len(fold.test_indices),
                "train_start": train_ts[0].isoformat() if train_ts else None,
                "train_end": train_ts[-1].isoformat() if train_ts else None,
                "test_start": test_ts[0].isoformat() if test_ts else None,
                "test_end": test_ts[-1].isoformat() if test_ts else None,
            }
        )
    return out
