#!/usr/bin/env python3
"""AI exit-decision runner — the terminal-AI brain for the Tier-0 shadow exit-manager.

Reads the no-outcome decision batch emitted by exit_policy_lab.py --emit-batch and asks a
LOCAL terminal AI (Codex CLI, no API key, no per-call billing) to decide, per position,
HOLD (keep to resolution) or CUT (take the static exit that actually fired). Writes
data/calibration/ai_exit_decisions.jsonl {trade_id, decision, reason}, which the `llm`
policy in exit_policy_lab.py then scores on the exact champion/challenger footing as every
coded policy. The AI never touches a live order — it scores a batch offline.

Design notes:
  * OUT-OF-PROCESS: the AI sees only the emitted context (side, entry, exit_reason,
    mfe/mae excursion-to-exit, secs-to-expiry, tape). It never sees actual_pnl/held_pnl —
    that is graded afterward, so the decision is honest.
  * Terminal, not API: shells to `codex exec`. Fully re-runnable (nightly shadow).
  * Fail-safe: any position the AI does not return a valid HOLD/CUT for is left out, and the
    scorer treats a missing trade as CUT (= static), so a partial AI response never invents edge.

Usage:
  python3 scripts/exit_policy_lab.py --emit-batch data/calibration/ai_exit_batch.jsonl
  python3 scripts/ai_exit_decide.py                    # runs Codex over the batch
  python3 scripts/exit_policy_lab.py --by-lane         # now includes the `llm` policy
"""
from __future__ import annotations
import json
import os
import re
import subprocess
import sys
from pathlib import Path

CAL = Path(__file__).resolve().parent.parent / "data" / "calibration"
BATCH = CAL / "ai_exit_batch.jsonl"
OUT = CAL / "ai_exit_decisions.jsonl"
CODEX = os.path.expanduser("~/.hermes/node/bin/codex")

PROMPT_HEADER = """\
You are an EXIT MANAGER for a Polymarket crypto up/down trading bot, running in an
offline shadow (your calls are scored, never executed). For each open position I give you
the state KNOWN AT THE MOMENT ITS STATIC EXIT FIRED, decide whether that exit was right.

Fields per position:
- side: LONG (bet price UP, bought YES) or SHORT (bet DOWN, bought NO)
- entry_price: share price paid (0..1); payoff is 1 if the side resolves correct, else 0
- exit_reason: why the bot's static rule exited (updown_stop_loss / take_profit /
  take_profit_late / never_green_cut / updown_time_stop / updown_expired)
- mfe_pct: best FAVORABLE excursion the position reached before the static exit (e.g. 0.30 = +30%)
- mae_pct: worst ADVERSE excursion before the static exit (negative)
- secs_to_expiry_at_exit: seconds left until the market resolves, at the exit moment
- tape_dir: mechanical tape for that asset (UP / DOWN / FLAT / null); tape_source proxy_htf is a weak read
- tape_strength: 0..1 trend conviction (null = unknown; do not assume strong)
- rsi_bucket: RSI regime at entry (oversold / neutral / overbought / null)
- lane_hold_prior: how often HOLDING beat cutting on THIS lane historically (0..1), from PAST
  trades only. lane_prior_n = how many past trades that is. THIS IS YOUR STRONGEST GROUNDING:
  a prior >=0.6 with n>=5 means holding this lane reliably paid; a prior <=0.4 means holding it
  historically LOST — cut. When lane_prior_n is small (<=2) the prior is weak (near 0.5), lean on tape/excursion instead.

Your decision per position:
- "HOLD" = override the exit and keep the position to resolution (you believe it recovers / the exit cut a winner)
- "CUT"  = the static exit was correct; take it

Judgment guidance (not rules): WEIGHT lane_hold_prior heavily when its n is adequate. A
stop_loss with a high mfe_pct means it went green then reversed into the stop — a cut-a-winner
candidate to HOLD IF the lane prior favors holding AND the tape still supports the side AND time
remains. A position fighting the tape (LONG in DOWN tape, SHORT in UP) with deep mae_pct and
little time left is usually a correct CUT even if mfe was decent. Do NOT hold a lane whose prior
is below 0.45 with n>=4. take_profit exits banked a gain; HOLD only with strong prior + tape support.

Output ONLY a JSON array, one object per position, no prose:
[{"trade_id": "...", "decision": "HOLD"|"CUT", "reason": "<=12 words"}]

Positions:
"""


def _extract_json_array(text: str):
    """Pull the outermost [...] JSON array out of possibly-noisy stdout."""
    i = text.find("[")
    j = text.rfind("]")
    if i == -1 or j == -1 or j <= i:
        return None
    try:
        return json.loads(text[i:j + 1])
    except Exception:
        # last resort: line-by-line object salvage
        objs = []
        for m in re.finditer(r"\{[^{}]*\"trade_id\"[^{}]*\}", text):
            try:
                objs.append(json.loads(m.group(0)))
            except Exception:
                continue
        return objs or None


def run_codex(prompt: str) -> str:
    if not os.path.exists(CODEX):
        print(f"[ai_exit] codex CLI not found at {CODEX}", file=sys.stderr)
        sys.exit(2)
    # codex exec prints the model reply to stdout; </dev/null so it never waits on a tty.
    proc = subprocess.run(
        [CODEX, "exec", "--skip-git-repo-check", prompt],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0:
        print(f"[ai_exit] codex exit {proc.returncode}: {proc.stderr[:400]}", file=sys.stderr)
    return proc.stdout


def main():
    if not BATCH.exists():
        print(f"[ai_exit] no batch at {BATCH}; run exit_policy_lab.py --emit-batch first",
              file=sys.stderr)
        sys.exit(1)
    ctxs = [l.strip() for l in open(BATCH) if l.strip()]
    prompt = PROMPT_HEADER + "\n".join(ctxs) + "\n"
    print(f"[ai_exit] asking Codex to score {len(ctxs)} positions (offline, no order path)…")
    raw = run_codex(prompt)
    arr = _extract_json_array(raw)
    if not arr:
        print("[ai_exit] could not parse a JSON array from Codex output. Raw head:",
              file=sys.stderr)
        print(raw[:800], file=sys.stderr)
        sys.exit(3)
    valid = [d for d in arr if d.get("trade_id") and str(d.get("decision", "")).upper() in ("HOLD", "CUT")]
    with open(OUT, "w") as fh:
        for d in valid:
            fh.write(json.dumps({"trade_id": d["trade_id"],
                                 "decision": str(d["decision"]).upper(),
                                 "reason": d.get("reason", "")}) + "\n")
    holds = sum(1 for d in valid if d["decision"].upper() == "HOLD")
    print(f"[ai_exit] wrote {len(valid)}/{len(ctxs)} decisions -> {OUT}  "
          f"(HOLD={holds} CUT={len(valid)-holds})")


if __name__ == "__main__":
    main()
