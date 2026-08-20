## Olympus P0/P1 Live-Path Review

Branch: `live/olympus-smoke`  
Scope: `src/main.py`, `src/execution/olympus_client.py`; ignored `*.bak`.

### Findings

1. **NO-GO - P0 #1 phantom guard can create an unmanaged real position on late fill.**  
   `src/main.py:5474` and `src/main.py:5798` correctly skip journaling when Olympus is still non-terminal, but the stated recovery path is not true: `reconcile_open_positions_with_venue` only compares already-journaled open positions to Olympus condition IDs, drops missing phantoms, then warns on `unmanaged` live positions (`src/main.py:2121`, `src/main.py:2137`, `src/main.py:2143`). It does not promote `clob_client.pending_orders` into `risk_manager.active_positions` or the journal. If Olympus fills after the 12s poll, PSB can hold real shares with no active position, no exit engine, and only a warning on a future startup reconcile. **Fix before smoke:** add an in-run pending Olympus order reconciler that polls pending entries, journals/adds position only after `FILLED`, and removes/marks failed orders.

2. **NO-GO - P1 #4 retries non-idempotent live trade POSTs without a client order id.**  
   `_request_json` retries every 429 including `POST /v1/trade` (`src/execution/olympus_client.py:253`, `src/execution/olympus_client.py:267`, `src/execution/olympus_client.py:400`). The code comment asserts a 429 means the order was rejected (`src/execution/olympus_client.py:239`), but the payload built by `build_trade_payload` has no idempotency/client-order field (`src/execution/olympus_client.py:334`) and `_request_json` has no dedupe key. If a gateway/proxy returns a racy 429 after upstream acceptance, retry can double-submit. **Fix before smoke:** either do not retry POST, or add a documented Olympus idempotency key/client order ID and reconcile by that key before resubmitting.

### Verified

- **P0 #1 partial GO:** the early `return` unwinds the `async with self._execution_lock` callers normally (`src/main.py:5319`, `src/main.py:5638`), and it happens before `risk_manager.add_position`, websocket held subscription, session-entry memory, journal entry, entry-book recording, and annotation tasks (`src/main.py:5542`, `src/main.py:5546`, `src/main.py:5609`; `src/main.py:5870`, `src/main.py:5874`, `src/main.py:5939`). `evaluate_entry` only computes/caps size, so there is no reservation left behind.
- **P0 #1 fast-fill check GO:** a real Olympus `SUCCEEDED` is normalized to `OrderStatus.FILLED` before the guard in the await-on-submit path, so legitimate terminal fills are journaled.
- **P0 #1 scoping GO:** the guard is scoped to `not dry_run` plus `using_olympus()` (`src/main.py:5477`, `src/main.py:5478`; `src/main.py:5801`, `src/main.py:5802`), so paper and non-Olympus CLOB paths are unaffected.
- **P1 #4 mechanics GO:** retried 429s do not consume `exc.read()` until the terminal raise (`src/execution/olympus_client.py:253`, `src/execution/olympus_client.py:269`); `time.sleep` runs inside `run_in_executor` callers (`src/execution/olympus_client.py:272`, `src/execution/olympus_client.py:398`); retry sleep is bounded to 30s per attempt (`src/execution/olympus_client.py:199`, `src/execution/olympus_client.py:207`), so it cannot stall indefinitely.
- **P1 #3 GO:** preflight telemetry is SELL-safe because all fields use `payload.get`, with `requested_amountUsd=None` and `submitted_amountUsd=None` for SELL (`src/execution/olympus_client.py:372`, `src/execution/olympus_client.py:385`). It logs public market/order fields only, not `Authorization` or API key headers.
- **P0 #2 GO:** `build_trade_payload` sends no `stopLossPercent` or `takeProfitPercent`; PSB remains the only exit engine (`src/execution/olympus_client.py:334`).

### Verdict

**NO-GO-FOR-LIVE-SMOKE** until the late-fill tracking gap and non-idempotent POST retry are fixed.

### Metadata/Summary

Tags: #PSB #Olympus #LiveTrading #CodeReview #Risk  
Related Concepts: [[Olympus async fills]], [[Idempotent order submission]], [[PSB exit engine]], [[Venue reconciliation]]  
Summary: The phantom guard prevents bad active-position journaling, but late fills can become real unmanaged holdings. The 429 backoff is mechanically bounded, but retrying live POST trades without idempotency is not safe enough for real-money smoke.
