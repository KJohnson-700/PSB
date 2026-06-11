#!/usr/bin/env python3
"""
ghost_decision_tree.py
Finds the best feature splits separating ghost winners from losers
using a DecisionTreeClassifier on the settled ghost log.
"""

import json
import pandas as pd
import numpy as np
from sklearn.tree import DecisionTreeClassifier, export_text
from sklearn.preprocessing import LabelEncoder
from sklearn.model_selection import cross_val_score
import warnings

warnings.filterwarnings("ignore")

DATA_PATH = "data/calibration/rejected_candidates_settled.jsonl"


def load_data(path):
    rows = []
    with open(path) as f:
        for line in f:
            rows.append(json.loads(line))
    return rows


def build_features(rows):
    """
    Extract features from each ghost record.
    Returns (X, y, feature_names, df)
    """
    records = []
    for r in rows:
        rec = {
            # Target
            "win": int(r["win"]),
            # Core numeric
            "est_prob_up": r.get("est_prob_up") if r.get("est_prob_up") is not None else 0.5,
            "hypothetical_payout": r.get("hypothetical_payout") or 0.0,
            # Categorical — keep as string for display breakdowns
            "strategy": r.get("strategy", "unknown"),
            "window": r.get("window", "unknown"),
            "side": r.get("side", "unknown"),
            "htf_bias": r.get("htf_bias") or "NONE",
            # Context booleans
            "macd_4h_histogram_rising": int(
                r.get("context", {}).get("macd_4h_histogram_rising", False)
            ),
            "macd_1h_histogram_rising": int(
                r.get("context", {}).get("macd_1h_histogram_rising", False)
            ),
            "macd_4h_above_zero": int(
                r.get("context", {}).get("macd_4h_above_zero", False)
            ),
            "sabre_trend": r.get("context", {}).get("sabre_trend", 0) or 0,
            # Whether context is rich (non-backfill-only)
            "has_rich_context": int(
                len([k for k in r.get("context", {}) if k != "backfilled_from_log"]) > 0
            ),
            # Rejection reason family
            "reason_family": r.get("reason", "unknown") or "unknown",
        }
        records.append(rec)

    df = pd.DataFrame(records)

    # Encode string categoricals for X matrix only
    cat_cols = ["strategy", "window", "side", "htf_bias", "reason_family"]
    encoders = {}
    for col in cat_cols:
        le = LabelEncoder()
        df[f"_enc_{col}"] = le.fit_transform(df[col].astype(str))
        encoders[col] = le

    feature_cols = [
        "est_prob_up",
        "hypothetical_payout",
        "_enc_strategy",
        "_enc_window",
        "_enc_side",
        "_enc_htf_bias",
        "macd_4h_histogram_rising",
        "macd_1h_histogram_rising",
        "macd_4h_above_zero",
        "sabre_trend",
        "has_rich_context",
        "_enc_reason_family",
    ]

    # Human-readable names for tree display (same order as feature_cols)
    display_names = [
        "est_prob_up",
        "hypothetical_payout",
        "strategy",
        "window",
        "side",
        "htf_bias",
        "macd_4h_histogram_rising",
        "macd_1h_histogram_rising",
        "macd_4h_above_zero",
        "sabre_trend",
        "has_rich_context",
        "reason_family",
    ]

    X = df[feature_cols].values
    y = df["win"].values

    return X, y, display_names, df, encoders


def main():
    print("=" * 70)
    print("GHOST DECISION TREE — Winners vs Losers")
    print("=" * 70)

    print("\n[1] Loading data...")
    rows = load_data(DATA_PATH)
    print(f"    Loaded {len(rows):,} records")

    winners = sum(1 for r in rows if r["win"])
    losers = sum(1 for r in rows if not r["win"])
    print(f"    Winners: {winners:,}  ({100*winners/(winners+losers):.1f}%)")
    print(f"    Losers:  {losers:,}  ({100*losers/(winners+losers):.1f}%)")

    print("\n[2] Building features...")
    X, y, feature_names, df, encoders = build_features(rows)
    print(f"    Feature matrix: {X.shape}")
    print(f"    Features: {feature_names}")

    # Class balance
    print(f"\n    Class distribution — Win=1: {y.sum():,}, Lose=0: {(1-y).sum():,}")

    # ── Shallow tree (readable) ────────────────────────────────────────────
    print("\n[3] Training shallow decision tree (depth=3)...")
    dt_shallow = DecisionTreeClassifier(
        max_depth=3, min_samples_leaf=5000, random_state=42
    )
    dt_shallow.fit(X, y)
    train_acc = dt_shallow.score(X, y)
    print(f"    Training accuracy: {train_acc:.4f}")

    print("\n    Tree structure (text):")
    tree_rules = export_text(dt_shallow, feature_names=feature_names)
    print(tree_rules)

    # Feature importances (shallow)
    print("\n    Feature importances (shallow tree):")
    imp = pd.Series(dt_shallow.feature_importances_, index=feature_names)
    imp = imp[imp > 0].sort_values(ascending=False)
    for feat, val in imp.items():
        print(f"      {feat:35s}  {val:.4f}")

    # ── Medium tree (more detail) ──────────────────────────────────────────
    print("\n[4] Training medium decision tree (depth=5)...")
    dt_med = DecisionTreeClassifier(
        max_depth=5, min_samples_leaf=2000, random_state=42
    )
    dt_med.fit(X, y)
    print(f"    Training accuracy: {dt_med.score(X, y):.4f}")

    print("\n    Feature importances (medium tree):")
    imp2 = pd.Series(dt_med.feature_importances_, index=feature_names)
    imp2 = imp2[imp2 > 0].sort_values(ascending=False)
    for feat, val in imp2.items():
        print(f"      {feat:35s}  {val:.4f}")

    # ── Cross-validation score ────────────────────────────────────────────
    print("\n[5] Cross-validation (5-fold, depth=4)...")
    dt_cv = DecisionTreeClassifier(
        max_depth=4, min_samples_leaf=5000, random_state=42
    )
    scores = cross_val_score(dt_cv, X, y, cv=5, scoring="accuracy")
    print(f"    CV Accuracy: {scores.mean():.4f} ± {scores.std():.4f}")
    print(f"    Per-fold:    {[f'{s:.4f}' for s in scores]}")

    # ── Univariate splits: best threshold for est_prob_up ─────────────────
    print("\n[6] Best univariate split on est_prob_up...")
    prob_col = df["est_prob_up"].values
    best_acc = 0
    best_thresh = 0.5
    for t in np.arange(0.30, 0.71, 0.01):
        pred = (prob_col >= t).astype(int)
        acc = (pred == y).mean()
        if acc > best_acc:
            best_acc = acc
            best_thresh = t
    print(f"    Best threshold: est_prob_up >= {best_thresh:.2f}")
    print(f"    Accuracy at this split: {best_acc:.4f}")

    # Breakdown by threshold
    print("\n    Accuracy by est_prob_up threshold:")
    for t in [0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70]:
        pred = (prob_col >= t).astype(int)
        acc = (pred == y).mean()
        n_above = (prob_col >= t).sum()
        win_rate = y[prob_col >= t].mean() if n_above > 0 else 0
        print(
            f"      prob_up >= {t:.2f}: acc={acc:.4f}  "
            f"n={n_above:>7,}  win_rate={win_rate:.3f}"
        )

    # ── Breakdown by strategy ─────────────────────────────────────────────
    print("\n[7] Win rate by strategy:")
    # Re-load for decoded values
    for strat in df["strategy"].unique():
        mask = df["strategy"] == strat
        n = mask.sum()
        wr = y[mask].mean()
        print(f"      strategy={strat:15s}  n={n:>7,}  win_rate={wr:.3f}")

    # ── Breakdown by htf_bias ────────────────────────────────────────────
    print("\n[8] Win rate by htf_bias:")
    for bias in sorted(df["htf_bias"].unique()):
        mask = df["htf_bias"] == bias
        n = mask.sum()
        wr = y[mask].mean()
        print(f"      htf_bias={bias:10s}  n={n:>7,}  win_rate={wr:.3f}")

    # ── Breakdown by window ───────────────────────────────────────────────
    print("\n[9] Win rate by window:")
    for win in sorted(df["window"].unique()):
        mask = df["window"] == win
        n = mask.sum()
        wr = y[mask].mean()
        print(f"      window={win:5s}  n={n:>7,}  win_rate={wr:.3f}")

    # ── Breakdown by side ─────────────────────────────────────────────────
    print("\n[10] Win rate by side:")
    for side in sorted(df["side"].unique()):
        mask = df["side"] == side
        n = mask.sum()
        wr = y[mask].mean()
        print(f"      side={side:6s}  n={n:>7,}  win_rate={wr:.3f}")

    print("\n" + "=" * 70)
    print("Done.")
    print("=" * 70)


if __name__ == "__main__":
    main()
