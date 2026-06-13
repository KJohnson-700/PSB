# Decision note — exits are a wash, paper ≠ live, edge is entry (2026-06-12)

A synthesis to steer future sessions. Came out of the stop-question deep-dive
(reverting Codex's correlation guard + stop-widen, then chasing per-lane stop
widths). The conclusion reframes where calibration effort should go.

## What we learned

1. **Exits are nearly a wash on EV.** Held-to-resolution (`taken_exit_settler`,
   715 trades): stops are ~EV-neutral vs holding — only **34.8%** of stopped
   trades win if held, and holding them all is *slightly worse*. With the +30% TP
   capping upside and losers bleeding to −100% / −28%, exit tuning barely moves EV.

2. **MFE-touch overstates recovery.** The excursion-shadow "62% would recover to
   +30%" was intraday MFE *touches*; at **resolution** only 34.8% win. A transient
   +30% spike ≠ a win. Use the settler (held-to-resolution), not MAE/MFE, to judge
   exits.

3. **Selection bias flips conclusions.** Evaluating only the stopped/losing subset
   made a tighter S8 stop and a time-exit both look like wins; on the **full
   population** both went negative. Always evaluate on the full population.

4. **Stops gap; the trigger % isn't the realized fill.** A 15% stop fills at a
   **−28% median** (−45% in the last 2 min to resolution) — partly loop latency
   (the 10s exit loop catches the cross late), partly (in live) bid-ask spread.

5. **Paper ≠ live, structurally.** `dry_run` fills every order at the *requested*
   price — no spread, slippage, or partial fills — and until commit `ba6cadb` the
   live order path was **fully broken** (would crash on every order). So all our
   EV numbers are a paper engine that is optimistic exactly where live is hardest.

6. **Entry selection is the trustworthy part.** The ghost log settles candidates
   against **real Polymarket outcomes** (resolution-based, fill-independent), so
   entry-side EV is robust. Exit/stop/sizing EV is paper-fiction.

## What this changes

### Calculation
- Judge exits on held-to-resolution (settler), never MAE/MFE touches.
- Always evaluate a change on the full population, not the affected subset.
- Treat paper EV **magnitudes** as directional only for anything fill-dependent
  (exits, stops, sizing). Entry-side ghost EV is the exception (resolution-based).
- Stops are a **risk-control** tool (variance/drawdown/turnover), not an EV tool.

### Trading
- Reallocate effort from exits to **entry selection** (lane/window/side sit-outs
  and flips) — that is where EV lives and what the ghost log can prove.
- Don't chase per-lane stop widths until there is **live fill data** — their
  optimum depends on fills we can't model on paper.
- The +30% TP / −100% asymmetry means a lane needs a high win rate just to break
  even; use that as the admission lens.

### Code
- A **live smoke test ($1–2)** is mandatory before trusting anything live — the
  order path had never been exercised.
- **Make the paper fill model realistic** so paper stops hiding the fill (started
  — see below).
- Reducing exit-loop latency (fresher book at fire-time / faster cadence for short
  windows near resolution) is the paper-actionable half of the gap.

## Work shipped against this

- `200f875` — executable-price stop trigger (default-off): mark the stop on the
  exit-side bid, not the midpoint.
- `ba6cadb` — FAK marketable stop exits + fixed the latent live-order `OrderType`
  bug (live placement was impossible before this).
- Realistic paper fills (this commit, default-off): `src/execution/fill_sim.py`
  book-walk + wired into `PositionExitManager.check_exits` for long-YES exits via
  `trading.exit_rules.realistic_paper_fills`. Flip on to start collecting realistic
  paper data and measure the paper→live haircut.

## Next increments
- Capture the YES **ask ladder** in the book snapshot so long-NO / short-YES exits
  (and entries) get realistic fills too (v1 covers long-YES only).
- Apply realistic fills to **entries** (walk the ask ladder at fill time).
- After a live smoke test, re-measure per-lane stop widths on real fills.
