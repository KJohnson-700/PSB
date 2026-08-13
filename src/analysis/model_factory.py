"""Model-swappable decision-provider seam — the Moon Dev "ModelFactory" pattern.

The whole AI-driven route (task #107, plan 2026-06-28-claude-code-execution-layer) hangs on this:
the LLM decision layer must be MODEL-AGNOSTIC so MiniMax / Claude / local models swap behind ONE
interface. Providers all return the same structured JSON decision contract; every call is fail-safe
(returns None on error/timeout -> caller falls back to the deterministic champion). Providers are
pure decision functions with NO trading side-effects — logged-before-acting, champion-challenger.

Contract:  provider.predict_direction(asset_label, features, horizon_min) -> {"dir","conf","why"} | None
  dir  : "UP" | "DOWN" | "FLAT"
  conf : 0.0-1.0
  why  : short rationale (<=8 words)

`features` is a dict from the tape_map snapshot (price, rsi_14, macd_signs, ema_dir, vol_pct,
trend_dir_label, and — for the deterministic champion — direction/confidence).

Later: the same DecisionProvider drops into AIDecisionBroker's worker (enqueue->resolve) so the
scan loop never blocks; execution + risk stay deterministic (LLM is SIGNAL/DECISION layer only).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.request
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Union

# Shared prompt so every model is judged on the SAME instruction (fair head-to-head).
# 2026-08-09 FLAT-DEFAULT FIX (operator GO): the model was stamping FLAT/"sitout:low volume" on 5/6
# assets overnight, killing frequency on a clock (low vol is a time-of-day artifact, not no-direction).
# A/B on live tape: this wording flipped BTC/ETH FLAT->DOWN c0.55-0.62 (matching the real 1h drift) while
# correctly KEEPING genuinely-mixed DOGE FLAT. Weak calls come graded low-conf, so the bot's min_conf gate
# + realized adapter still filter the fee-bleed the 08-03 flat-block was added for.
DIRECTION_SYSTEM = (
    "You are a crypto short-horizon direction classifier. Given indicator features (or raw tape) for one "
    'asset, output ONLY a compact JSON object {{"dir":"UP|DOWN|FLAT","conf":0.0-1.0,"why":"<=8 words"}} '
    "predicting the price direction over the NEXT {h} MINUTES. "
    "Reserve FLAT ONLY for a truly directionless tape (no net drift on ANY timeframe). If any timeframe "
    "shows a consistent drift, even a small one on low volume, COMMIT to that direction with LOW confidence "
    "(0.50-0.60). Low volume ALONE is NOT a reason for FLAT. No prose, JSON only."
)
_FEATURE_KEYS = ("price", "rsi_14", "macd_signs", "ema_dir", "vol_pct", "trend_dir_label")


def build_message(asset_label: str, features: Dict[str, Any], horizon_min: int) -> str:
    payload = {k: features.get(k) for k in _FEATURE_KEYS}
    return f"Asset={asset_label}. Features: {json.dumps(payload)}. Predict next {horizon_min}min. JSON only."


def extract_decision(text: str) -> Optional[Dict[str, Any]]:
    """Pull the {dir,conf,why} object out of arbitrary model output. None if absent/malformed."""
    if not text:
        return None
    m = re.search(r'\{[^{}]*"dir"\s*:\s*"(UP|DOWN|FLAT)"[^{}]*\}', text)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return None
    obj["dir"] = str(obj.get("dir", "")).upper()
    return obj


# ---- raw-tape fetch (the "read the tape" input, independent of the digest) -------------------
_BINANCE_SYMBOL = {
    "BTC": "BTCUSDT", "BITCOIN": "BTCUSDT", "SOL": "SOLUSDT", "ETH": "ETHUSDT",
    "XRP": "XRPUSDT", "DOGE": "DOGEUSDT", "BNB": "BNBUSDT", "HYPE": "HYPEUSDT",
}


def _fetch_klines(symbol: str, interval: str, limit: int = 16, timeout: float = 8.0):
    """Recent (close, volume) candles from Binance REST — same source the bot uses. None on failure."""
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 - fixed https host
            data = json.loads(r.read().decode())
        return [(float(k[4]), float(k[5])) for k in data]
    except Exception:
        return None


def raw_tape_summary(symbol: str) -> Optional[str]:
    """Compact multi-timeframe RAW tape the AI can actually read (candle shape, not a digest)."""
    parts: List[str] = []
    for tf in ("5m", "15m", "1h"):
        kl = _fetch_klines(symbol, tf, 16)
        if not kl:
            continue
        closes = [c for c, _ in kl]
        vols = [v for _, v in kl]
        base = closes[0] or 1e-9
        pct = [round(100 * (c - base) / base, 2) for c in closes]        # % vs window start = the shape
        rng = round(100 * (max(closes) - min(closes)) / base, 2)
        vtrend = round(vols[-1] / (sum(vols) / len(vols) + 1e-9), 2)     # latest vol / avg
        parts.append(f"{tf}[last={closes[-1]} closes%={pct} range%={rng} volX={vtrend}]")
    return " ".join(parts) if parts else None


class DecisionProvider(ABC):
    """Base class for a swappable model. Subclasses MUST be fail-safe (return None, never raise)."""

    name: str = "base"

    @abstractmethod
    def predict_direction(self, asset_label: str, features: Dict[str, Any],
                          horizon_min: int = 15) -> Optional[Dict[str, Any]]:
        ...


class MiniMaxProvider(DecisionProvider):
    name = "minimax"

    def __init__(self, model: str = "MiniMax-M3",
                 mmx_path: str = "~/.hermes/node/bin/mmx", timeout: float = 45.0):
        self.model = model
        self.mmx = os.path.expanduser(mmx_path)
        self.timeout = timeout

    def _call(self, system: str, message: str, max_tokens: str = "120") -> Optional[Dict[str, Any]]:
        try:
            r = subprocess.run(
                [self.mmx, "text", "chat", "--non-interactive", "--quiet", "--output", "json",
                 "--model", self.model, "--max-tokens", max_tokens, "--temperature", "0.2",
                 "--system", system, "--message", message],
                capture_output=True, text=True, timeout=self.timeout,
            )
            return extract_decision((r.stdout or "") + (r.stderr or ""))
        except Exception:
            return None

    def predict_direction(self, asset_label, features, horizon_min=15):
        return self._call(DIRECTION_SYSTEM.format(h=horizon_min),
                          build_message(asset_label, features, horizon_min))


class MiniMaxTapeProvider(MiniMaxProvider):
    """The REAL 'reads the tape' arm: feeds the AI RAW multi-timeframe candle action (Binance),
    NOT the pre-computed rsi/macd digest — so it can distinguish oversold-bounce from downtrend-
    continuation, which fixed-rule features cannot encode. Same cheap model, richer input."""

    name = "minimax_tape"

    def predict_direction(self, asset_label, features, horizon_min=15):
        sym = _BINANCE_SYMBOL.get(str(asset_label).upper())
        if not sym:
            return None
        tape = raw_tape_summary(sym)
        if not tape:
            return None
        msg = (f"Asset={asset_label}. RAW multi-timeframe tape (16 recent candles per TF; closes% = "
               f"% change vs each window's start = the price SHAPE; volX = latest vol / avg): {tape}. "
               f"Read the tape and predict the next {horizon_min} minutes. JSON only.")
        return self._call(DIRECTION_SYSTEM.format(h=horizon_min), msg, max_tokens="150")


class ClaudeProvider(DecisionProvider):
    name = "claude"

    def __init__(self, claude_path: str = "claude", model: Optional[str] = None, timeout: float = 60.0):
        self.claude = claude_path
        self.model = model
        self.timeout = timeout

    def predict_direction(self, asset_label, features, horizon_min=15):
        try:
            cmd = [self.claude, "-p", build_message(asset_label, features, horizon_min),
                   "--append-system-prompt", DIRECTION_SYSTEM.format(h=horizon_min),
                   "--output-format", "json"]
            if self.model:
                cmd += ["--model", self.model]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
            out = r.stdout or ""
            # `claude -p --output-format json` wraps the answer in an envelope {..,"result": "<text>"}
            txt = out
            try:
                env = json.loads(out)
                txt = env.get("result") or env.get("content") or out
                if not isinstance(txt, str):
                    txt = json.dumps(txt)
            except Exception:
                pass
            return extract_decision(txt)
        except Exception:
            return None


class TapeMapProvider(DecisionProvider):
    """The DETERMINISTIC champion, behind the same seam — so it can be compared/swapped uniformly.
    Reads the mechanical verdict already computed in the features (no model call)."""

    name = "tape_map"

    def predict_direction(self, asset_label, features, horizon_min=15):
        d = features.get("direction") or features.get("tape_dir")
        if not d:
            return None
        c = features.get("confidence")
        if c is None:
            c = features.get("tape_conf")
        return {"dir": str(d).upper(), "conf": c, "why": "mechanical tape_map"}


class QwenProvider(DecisionProvider):
    """LOCAL ollama model (default qwen3-vl:4b-instruct-q4_K_M) reading the RAW multi-timeframe tape.
    The cheap, high-frequency arm of the cascade: local => zero API cost, NO rate limits (unlike the
    minimax API which rate-limited/errored 2026-08-05), near-zero marginal latency. Same raw-tape
    input + shared DIRECTION_SYSTEM prompt as minimax_tape for a fair head-to-head. OBSERVE-ONLY like
    every provider. Fail-safe: ollama down / timeout / bad JSON => None (caller falls back to the
    deterministic champion). Note: qwen3-VL is vision-capable but used here as a TEXT tape reader.
    Requires `ollama serve` + `ollama pull qwen3-vl:4b-instruct-q4_K_M`."""

    name = "qwen"

    def __init__(self, model: str = "qwen3-vl:4b-instruct-q4_K_M",
                 host: str = "http://localhost:11434", timeout: float = 30.0,
                 use_tape: Union[str, bool] = True):
        self.model = model
        self.host = str(host).rstrip("/")
        self.timeout = float(timeout)
        # accept "false"/"0" from the --providers spec string
        self.use_tape = str(use_tape).lower() not in ("false", "0", "no", "off")

    def _call(self, system: str, message: str, max_tokens: int = 150) -> Optional[Dict[str, Any]]:
        try:
            body = json.dumps({
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": message},
                ],
                "stream": False,
                "options": {"temperature": 0.2, "num_predict": max_tokens},
            }).encode()
            req = urllib.request.Request(
                f"{self.host}/api/chat", data=body,
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310 - fixed localhost host
                resp = json.loads(r.read().decode())
            txt = (resp.get("message") or {}).get("content") or resp.get("response") or ""
            return extract_decision(txt)
        except Exception:
            return None

    def predict_direction(self, asset_label, features, horizon_min=15):
        if self.use_tape:
            sym = _BINANCE_SYMBOL.get(str(asset_label).upper())
            tape = raw_tape_summary(sym) if sym else None
            if tape:
                msg = (f"Asset={asset_label}. RAW multi-timeframe tape (16 recent candles per TF; "
                       f"closes% = % change vs each window's start = the price SHAPE; volX = latest "
                       f"vol / avg): {tape}. Read the tape and predict the next {horizon_min} minutes. "
                       f"JSON only.")
                return self._call(DIRECTION_SYSTEM.format(h=horizon_min), msg)
        # digest-feature fallback (also when tape fetch failed)
        return self._call(DIRECTION_SYSTEM.format(h=horizon_min),
                          build_message(asset_label, features, horizon_min))


# ── VISION path — qwen3-vl reads a rendered CHART, not a text tape ──────────────────────────────────
# 2026-08-06 (operator): qwen3-VL is a VISION model (strong at vision, weak at math per the Hermes test);
# its AGREED job is to READ THE CHARTS and lay the tape/regime -> direction (minimax does the harder text
# reasoning). The old QwenProvider fed it a TEXT tape summary — the wrong job for a vision model. This
# renders real candlesticks and lets qwen read them. Pure-numpy PNG (no PIL/matplotlib dependency).


def _fetch_ohlc(symbol: str, interval: str, limit: int = 24, timeout: float = 8.0):
    """Recent OHLC candles from Binance REST -> list of (open,high,low,close). None on failure."""
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 - fixed https host
            data = json.loads(r.read().decode())
        return [(float(k[1]), float(k[2]), float(k[3]), float(k[4])) for k in data]
    except Exception:
        return None


def _candles_png_b64(symbol: str) -> Optional[str]:
    """Render a multi-timeframe candlestick chart (5m top / 15m mid / 1h bottom) to a base64 PNG the
    vision model can read. Green=up candle, red=down; wick=high-low, body=open-close. No PIL/matplotlib."""
    try:
        import numpy as np
        import zlib
        import struct
        panels = []
        for tf in ("5m", "15m", "1h"):
            sym_ohlc = _fetch_ohlc(symbol, tf, 24)
            if sym_ohlc:
                panels.append(sym_ohlc)
        if not panels:
            return None
        PW, PH, GAP, LM = 460, 150, 16, 12
        W = PW + LM * 2
        H = len(panels) * (PH + GAP) + GAP
        img = np.full((H, W, 3), 255, np.uint8)
        for pi, ohlc in enumerate(panels):
            y0 = GAP + pi * (PH + GAP)
            hi = max(c[1] for c in ohlc)
            lo = min(c[2] for c in ohlc)
            rng = (hi - lo) or 1e-9

            def py(p, _y0=y0, _hi=hi, _rng=rng):
                return int(_y0 + (PH - 1) * (_hi - p) / _rng)
            m = len(ohlc)
            cw = max(2, PW // m - 3)
            for i, (o, h, l, c) in enumerate(ohlc):
                cx = LM + int((i + 0.5) * PW / m)
                col = [0, 150, 0] if c >= o else [210, 20, 20]
                yh, yl = py(h), py(l)
                img[min(yh, yl):max(yh, yl) + 1, cx] = col          # wick
                yo, yc = py(o), py(c)
                top, bot = min(yo, yc), max(yo, yc)
                img[top:bot + 1, max(LM, cx - cw // 2):min(LM + PW, cx + cw // 2) + 1] = col  # body
            img[y0 + PH:y0 + PH + 1, :] = [190, 190, 190]           # panel separator

        raw = b"".join(b"\x00" + img[y].tobytes() for y in range(H))

        def _chunk(t, d):
            return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
        png = (b"\x89PNG\r\n\x1a\n"
               + _chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0))
               + _chunk(b"IDAT", zlib.compress(raw, 6))
               + _chunk(b"IEND", b""))
        import base64
        return base64.b64encode(png).decode()
    except Exception:
        return None


VISION_SYSTEM = (
    "You are a price-action chart reader. You are shown a candlestick chart with THREE stacked panels: "
    "TOP=5-minute, MIDDLE=15-minute, BOTTOM=1-hour. Green candle=close above open, red=below; the thin "
    "line is the high-low wick. Read the TAPE and REGIME (trending up / trending down / ranging) and "
    "predict the next {h} minutes of price. Weight the middle (15m) panel most, using 5m for immediate "
    "momentum and 1h for the broader regime. Respond with JSON ONLY: "
    '{{"dir":"UP"|"DOWN"|"FLAT","conf":0.0-1.0,"why":"<=12 words tape/regime read"}}.'
)


class QwenVisionProvider(QwenProvider):
    """qwen3-vl doing its ACTUAL job: READ THE CHART (vision) to lay tape/regime -> direction. Renders a
    3-panel candlestick chart and sends the IMAGE to ollama (not a text tape). OBSERVE-ONLY, fail-safe
    (no chart / ollama down / bad JSON => None => caller falls back to the deterministic champion). Runs
    on the same ModelFactory seam + scored the same way as every other arm (ai_direction_score.py)."""

    name = "qwen_vision"

    def _call_vision(self, system: str, prompt: str, image_b64: str,
                     max_tokens: int = 120) -> Optional[Dict[str, Any]]:
        try:
            body = json.dumps({
                "model": self.model,
                "prompt": f"{system}\n\n{prompt}",
                "images": [image_b64],
                "stream": False,
                "options": {"temperature": 0.1, "num_predict": max_tokens},
            }).encode()
            req = urllib.request.Request(
                f"{self.host}/api/generate", data=body,
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310 - fixed localhost host
                resp = json.loads(r.read().decode())
            return extract_decision(resp.get("response") or "")
        except Exception:
            return None

    def predict_direction(self, asset_label, features, horizon_min=15):
        sym = _BINANCE_SYMBOL.get(str(asset_label).upper())
        img = _candles_png_b64(sym) if sym else None
        if not img:
            return None  # fail-safe: no chart => defer to champion (do NOT fall back to the text hack)
        return self._call_vision(
            VISION_SYSTEM.format(h=horizon_min),
            f"Asset={asset_label}. Read this chart and predict the next {horizon_min} minutes.",
            img,
        )


class ModelFactory:
    """Registry + factory. Register new models with one line; create by name or spec dict."""

    _registry: Dict[str, type] = {
        "minimax": MiniMaxProvider,
        "minimax_tape": MiniMaxTapeProvider,
        "claude": ClaudeProvider,
        "tape_map": TapeMapProvider,
        "qwen": QwenProvider,
        "qwen_vision": QwenVisionProvider,
    }

    @classmethod
    def register(cls, name: str, provider_cls: type) -> None:
        cls._registry[name.lower()] = provider_cls

    @classmethod
    def available(cls) -> List[str]:
        return sorted(cls._registry)

    @classmethod
    def create(cls, spec: Union[str, Dict[str, Any]]) -> DecisionProvider:
        if isinstance(spec, str):
            name, kwargs = spec, {}
        else:
            name = spec.get("provider") or spec.get("name")
            kwargs = {k: v for k, v in spec.items() if k not in ("provider", "name")}
        provider_cls = cls._registry.get(str(name or "").lower())
        if provider_cls is None:
            raise ValueError(f"unknown provider '{name}'; available: {cls.available()}")
        return provider_cls(**kwargs)

    @classmethod
    def create_all(cls, specs: List[Union[str, Dict[str, Any]]]) -> List[DecisionProvider]:
        return [cls.create(s) for s in specs]
