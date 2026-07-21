"""P2: per-asset 3-state Gaussian HMM regime layer (2026-07-03). OBSERVE-ONLY.

Pure-numpy univariate Gaussian HMM on 5m log-returns, fed by asset_regime.update
(same closes the TA services already fetch — no extra I/O). Three latent states
initialised and labelled by volatility tercile: quiet / normal / turbulent, with
the state mean adding drift direction. Output = FILTERED state probabilities
(P(state | data so far), no lookahead), the practitioner-standard way to consume
regimes probabilistically (size scaling / confidence) rather than hard labels.

Deliberately NOT consumed by anything yet. Snapshots go to ops via
asset_regime piggyback and transitions to data/calibration/asset_regime_hmm.jsonl
for the same live-outcome validation the P0 gate cleared and P1 must clear.

Limitations (documented, accepted for observe phase):
- fit window = whatever closes the services pass (~100 bars ≈ 8h). Refit is
  throttled to every REFIT_SEC per asset; filtering runs every update.
- EM on 100 points / 3 states is noisy; treat params as provisional until the
  validation join says otherwise. A longer-history fetch is a later upgrade.

Never raises into the caller; all failures degrade to "no state".
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TRANSITIONS_PATH = _REPO_ROOT / "data" / "calibration" / "asset_regime_hmm.jsonl"

N_STATES = 3
REFIT_SEC = 3600.0
EM_ITERS = 25
MIN_BARS = 60
_FLOOR = 1e-12

_lock = threading.Lock()
_models: dict = {}   # symbol -> {"mu","sigma","A","pi","fit_ts"}
_latest: dict = {}   # symbol -> {"state","probs","mu","updated"}


def _gauss_pdf(x, mu, sigma):
    s = np.maximum(sigma, 1e-8)
    return np.exp(-0.5 * ((x[:, None] - mu[None, :]) / s[None, :]) ** 2) / (s[None, :] * math.sqrt(2 * math.pi))


def _fit_em(r: np.ndarray):
    """EM for a 3-state Gaussian HMM; init by |return| terciles."""
    order = np.argsort(np.abs(r))
    thirds = np.array_split(order, N_STATES)
    mu = np.array([float(np.mean(r[ix])) for ix in thirds])
    sigma = np.array([max(float(np.std(r[ix])), 1e-6) for ix in thirds])
    A = np.full((N_STATES, N_STATES), 0.1 / (N_STATES - 1))
    np.fill_diagonal(A, 0.9)
    pi = np.full(N_STATES, 1.0 / N_STATES)
    T = len(r)

    for _ in range(EM_ITERS):
        B = np.maximum(_gauss_pdf(r, mu, sigma), _FLOOR)  # T x K
        # scaled forward-backward
        alpha = np.zeros((T, N_STATES))
        c = np.zeros(T)
        alpha[0] = pi * B[0]
        c[0] = max(alpha[0].sum(), _FLOOR)
        alpha[0] /= c[0]
        for t in range(1, T):
            alpha[t] = (alpha[t - 1] @ A) * B[t]
            c[t] = max(alpha[t].sum(), _FLOOR)
            alpha[t] /= c[t]
        beta = np.ones((T, N_STATES))
        for t in range(T - 2, -1, -1):
            beta[t] = (A @ (B[t + 1] * beta[t + 1])) / c[t + 1]
        gamma = alpha * beta
        gamma /= np.maximum(gamma.sum(axis=1, keepdims=True), _FLOOR)
        xi_num = np.zeros((N_STATES, N_STATES))
        for t in range(T - 1):
            m = (alpha[t][:, None] * A) * (B[t + 1] * beta[t + 1])[None, :]
            xi_num += m / max(m.sum(), _FLOOR)
        xi_num += 1e-4  # transition smoothing: no dead rows/zero-prob transitions
        A = xi_num / np.maximum(xi_num.sum(axis=1, keepdims=True), _FLOOR)
        pi = gamma[0]
        w = gamma.sum(axis=0)
        mu = (gamma * r[:, None]).sum(axis=0) / np.maximum(w, _FLOOR)
        var = (gamma * (r[:, None] - mu[None, :]) ** 2).sum(axis=0) / np.maximum(w, _FLOOR)
        sigma = np.sqrt(np.maximum(var, 1e-12))

    # relabel states by sigma ascending: 0=quiet, 1=normal, 2=turbulent
    order = np.argsort(sigma)
    remap = np.empty(N_STATES, dtype=int)
    remap[order] = np.arange(N_STATES)
    inv = np.argsort(remap)
    return {"mu": mu[inv], "sigma": sigma[inv], "A": A[np.ix_(inv, inv)], "pi": pi[inv], "fit_ts": time.time()}


def _filter_probs(model, r: np.ndarray):
    B = np.maximum(_gauss_pdf(r, model["mu"], model["sigma"]), _FLOOR)
    f = model["pi"] * B[0]
    f /= max(f.sum(), _FLOOR)
    for t in range(1, len(r)):
        f = (f @ model["A"]) * B[t]
        f /= max(f.sum(), _FLOOR)
    return f


_LABELS = ["quiet", "normal", "turbulent"]


def update(symbol: str, closes: list) -> None:
    """Feed 5m closes (same call path as asset_regime). Never raises."""
    try:
        c = np.asarray([float(x) for x in closes], dtype=float)
        c = c[np.isfinite(c)]
        c = c[-500:]  # window cap: bound EM/filter cost regardless of caller
        if len(c) < MIN_BARS + 1 or np.any(c <= 0):
            return
        r = np.diff(np.log(c))
        now = time.time()
        with _lock:
            model = _models.get(symbol)
        if model is None or (now - model["fit_ts"]) > REFIT_SEC:
            model = _fit_em(r)
            with _lock:
                _models[symbol] = model
        probs = _filter_probs(model, r)
        k = int(np.argmax(probs))
        drift = float(model["mu"][k])
        state = _LABELS[k]
        with _lock:
            prev = _latest.get(symbol)
            _latest[symbol] = {
                "state": state,
                "probs": [round(float(p), 3) for p in probs],
                "drift_5m": round(drift, 6),
                "updated": now,
            }
            changed = (prev is None) or (prev["state"] != state)
        if changed:
            logger.info("ASSET_HMM %s -> %s probs=%s drift=%.5f", symbol, state,
                        [round(float(p), 2) for p in probs], drift)
            try:
                _TRANSITIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
                with _lock, open(_TRANSITIONS_PATH, "a") as f:
                    f.write(json.dumps({
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now)),
                        "symbol": symbol,
                        "state": state,
                        "prev": prev["state"] if prev else None,
                        "probs": [round(float(p), 3) for p in probs],
                        "drift_5m": round(drift, 6),
                    }) + "\n")
            except Exception:
                pass
    except Exception as e:
        logger.debug("asset_regime_hmm.update(%s) failed: %s", symbol, e)


def get_state(symbol: str):
    try:
        with _lock:
            d = _latest.get(symbol)
            if not d or time.time() - d["updated"] > 300:
                return None
            out = dict(d)
        out["age_sec"] = round(time.time() - out.pop("updated"), 1)
        return out
    except Exception:
        return None
