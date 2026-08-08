#!/usr/bin/env python3
"""PSB direction driver — wires the AI cascade to DRIVE the live override.

The gap (operator 2026-08-08): the 3-provider cascade (Claude -> minimax -> qwen)
only ran in SHADOW (ai_direction_engine.py -> data/calibration/ai_direction_shadow.jsonl).
The direction-override seam's mode=ai path is INERT, so nothing drove the live
override — the bot fell to the quant resolver (long-biased off the macro classifier)
with NO minimax fallback and NO alert when that happened.

This daemon closes it. Every INTERVAL it reads the freshest AI decision per asset
and writes the live override the seam consumes (data/runtime/claude_direction_override.json,
mode=claude), applying the cascade with FALLBACK + ALERT:

  Claude manual (data/runtime/claude_direction_manual.json, if fresh)   -- primary
    -> minimax_tape.dir  (if fresh + no error)                          -- AI primary
      -> qwen_vision.dir (if fresh + no error)                          -- AI fallback
        -> (write nothing => seam falls to quant)                       -- last resort

It ALERTS Oracle 2 (rate-limited) whenever the driving tier DEGRADES (claude->AI,
minimax->qwen, ->quant) or a side FLIPS — so a silent direction failure can never
happen again. SAFETY: writes only; never trades. The seam's own `enforce` flag still
gates whether the override actually changes a side (run enforce:false to SHADOW first).

Launch:  nohup .venv/bin/python scripts/psb_direction_driver.py > data/logs/direction_driver.log 2>&1 &
"""
import json, os, time, subprocess, glob

CWD = "/Users/mainfolder/Documents/psb-main 1"
HERMES = "/Users/mainfolder/.local/bin/hermes"
SHADOW = f"{CWD}/data/calibration/ai_direction_shadow.jsonl"
MANUAL = f"{CWD}/data/runtime/claude_direction_manual.json"   # Claude (me) can drop calls here
OVERRIDE = f"{CWD}/data/runtime/claude_direction_override.json"  # what the seam reads
LOG = f"{CWD}/data/logs/direction_driver.log"
ORACLE_TARGET = "telegram:-1004498748669"   # PSB Oracle 2 (direction status/degrade alerts)

ASSETS = ["bitcoin", "sol_macro", "eth_macro", "hype_macro", "bnb_macro", "xrp_macro", "doge_macro"]
PREFER_HORIZON = 15       # use the 15-min call as the driving horizon
FRESH_SEC = 900           # 08-08: widened 420->900. Engine interval is 300s but a full cycle
                          # (7 assets x 3 horizons x ~4-8s/call) puts an asset's age near the old
                          # 420 belt right before its refresh, dropping it to quant. Quant is the
                          # UNTUNED resolver we're trying to AVOID, so honor an AI read up to 15min
                          # old (still better than quant); an asset only falls to quant if the engine
                          # genuinely hasn't produced a call for it in 15 min (real failure -> alert).
MIN_CONF = 0.55           # below this the provider is too unsure -> sit out that asset
INTERVAL = 120            # driver refresh cadence
TTL = 900                 # ttl stamped on each override entry (seam belt is max_age_sec)
ALERT_BACKOFF = 900       # 15 min per-condition alert backoff

_DIR2SIDE = {"UP": "LONG", "DOWN": "SHORT", "LONG": "LONG", "SHORT": "SHORT"}
_last_alert = {}
_last_side = {}
_last_tier = {}


def log(m):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {m}\n")
    except Exception:
        pass


def alert(key, msg):
    now = time.time()
    if now - _last_alert.get(key, 0) >= ALERT_BACKOFF:
        try:
            subprocess.run([HERMES, "send", "--to", ORACLE_TARGET, msg], timeout=30)
        except Exception as e:
            log(f"alert-fail {e}")
        _last_alert[key] = now
        log(f"ALERT {key}: {msg}")


def latest_ai():
    """Freshest AI decision per asset at PREFER_HORIZON. Reads the tail of the shadow log."""
    out = {}
    if not os.path.exists(SHADOW):
        return out
    try:
        # tail ~last 400 lines is plenty for one cycle across 6 assets x 3 horizons
        lines = subprocess.run(["tail", "-n", "400", SHADOW], capture_output=True, text=True).stdout.splitlines()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("horizon_min") != PREFER_HORIZON:
                continue
            a = r.get("asset")
            if a in ASSETS:
                out[a] = r   # later lines overwrite -> freshest wins
    except Exception as ex:
        log(f"latest_ai err: {ex}")
    return out


def load_manual():
    try:
        if os.path.exists(MANUAL):
            with open(MANUAL) as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        pass
    return {}


def _fresh(ts, now, window):
    """True if ts is a number within `window` seconds of now. Malformed ts => stale (False)."""
    try:
        return 0 <= (now - float(ts)) <= float(window)
    except (TypeError, ValueError):
        return False


# Codex fix #2: provider names drift across engine versions (minimax_tape/minimax,
# qwen_vision/qwen). Match either. minimax is the AI primary, qwen the AI fallback.
_TIERS = (("minimax", ("minimax_tape", "minimax")), ("qwen", ("qwen_vision", "qwen")))


def resolve_asset(asset, ai_rec, manual, now):
    """Return (side, conf, tier, why) applying Claude->minimax->qwen, or (None,...,'quant',...).

    Cascade FALLBACK is on FAILURE ONLY (provider errored / absent / stale). A provider
    that ANSWERS — including a FLAT 'sit out' or a low-confidence call — is respected and
    STOPS the cascade (we do NOT let the next tier override a valid answer). Codex fix #1.
    """
    # 1) Claude manual (primary) — a fresh, mapped manual call wins outright.
    m = manual.get(asset)
    if isinstance(m, dict) and _fresh(m.get("ts", now), now, m.get("ttl", TTL)):
        side = _DIR2SIDE.get(str(m.get("side", "")).upper())
        if side:
            return side, m.get("conf"), "claude", str(m.get("why", "manual"))[:60]
        # manual present but FLAT/unmapped => operator said sit out this asset
        return None, m.get("conf"), "claude", "manual FLAT"
    # 2/3) AI cascade — only fall to the next tier when THIS tier failed (error/absent).
    dec = (ai_rec or {}).get("decisions", {}) or {}
    ai_fresh = bool(ai_rec) and _fresh(ai_rec.get("ts", 0), now, FRESH_SEC)
    if ai_fresh:
        for tname, keys in _TIERS:
            p = None
            for k in keys:
                if isinstance(dec.get(k), dict):
                    p = dec[k]
                    break
            if p is None or p.get("error") is not None:
                continue  # provider absent or errored -> fall back to next tier
            # provider ANSWERED. conf gate: too-unsure => sit out (do NOT fall through).
            conf = p.get("conf")
            try:
                sure = conf is None or float(conf) >= MIN_CONF
            except (TypeError, ValueError):
                sure = True
            if not sure:
                return None, conf, tname, f"low-conf {p.get('dir')}"
            side = _DIR2SIDE.get(str(p.get("dir", "")).upper())  # None for FLAT/unmapped => sit out
            return side, conf, tname, str(p.get("why", ""))[:60]
    return None, None, "quant", "no fresh AI"


def main():
    log("direction-driver START")
    while True:
        try:
            now = time.time()
            ai = latest_ai()
            manual = load_manual()
            out = {}
            tiers = {}
            for a in ASSETS:
                side, conf, tier, why = resolve_asset(a, ai.get(a), manual, now)
                tiers[a] = tier
                if tier == "quant":
                    # No fresh AI/manual at all -> omit the entry so the seam uses quant
                    # (the designed last-resort tier). The degrade alert below fires.
                    pass
                elif side is None:
                    # A provider ANSWERED sit-out (FLAT / low-conf / manual FLAT). Write an
                    # EXPLICIT FLAT so the seam SITS OUT (needs override_when_quant_neutral:true,
                    # which is set) instead of silently falling through to a quant side. Codex fix #1.
                    out[a] = {"side": "FLAT", "conf": conf if conf is not None else 0.5,
                              "ts": int(now), "ttl": TTL, "why": f"{tier}:sitout:{why}"}
                else:
                    out[a] = {"side": side, "conf": conf if conf is not None else 0.6,
                              "ts": int(now), "ttl": TTL, "why": f"{tier}:{why}"}
                # degrade / flip alerts
                if _last_tier.get(a) and _last_tier[a] != tier and tier in ("qwen", "quant"):
                    alert(f"degrade:{a}", f"PSB direction {a}: {_last_tier[a]}->{tier} "
                                          f"({'minimax failed' if tier=='qwen' else 'ALL AI stale -> quant long-bias'})")
                if side and _last_side.get(a) and _last_side[a] != side:
                    alert(f"flip:{a}", f"PSB direction {a}: {_last_side[a]}->{side} ({tier} conf={conf})")
                _last_tier[a] = tier
                if side:
                    _last_side[a] = side
            # if EVERY asset fell to quant, that's a cascade-wide failure -> alert
            if all(t == "quant" for t in tiers.values()):
                alert("all_quant", "PSB DIRECTION: entire AI cascade stale — bot on QUANT (long-biased) for ALL assets. Check ai_direction_engine (pid).")
            # write the live override the seam reads (atomic)
            tmp = OVERRIDE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(out, f, indent=2, sort_keys=True)
            os.replace(tmp, OVERRIDE)
            log(f"wrote override: " + ", ".join(f"{a}={out[a]['side'] if a in out else 'quant'}({tiers[a]})" for a in ASSETS))
        except Exception as ex:
            log(f"loop-err {ex}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
