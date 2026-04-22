# Assessment: Collaborative Filtering, Time Series & Forecasting (LSTM / ARIMA)

**Context:** Polymarket execution bot with fade and arbitrage strategies, backtest on historical price data. This doc assesses whether adding collaborative filtering (CF), LSTM, or ARIMA is warranted.

---

## 1. Current System (What You Have)

- **Signals are cross-sectional:** Each decision uses only the **current** bar (price, spread). The backtest engine passes one row per timestamp to the strategy; there is no lookback window or history.
- **“AI” in backtest is rule-based:** `BacktestAIAgent` uses simple rules (e.g. fade: true prob = 1 − consensus; arbitrage: mean reversion toward 0.5). No learned model, no time series, no similarity across markets.
- **Strategies:** Fade = bet against extreme consensus; arbitrage = trade when AI-estimated probability differs from market price by more than a threshold. Both rely on **current price vs a “true” probability** estimate.

So today you have **no**:
- Collaborative filtering
- Time series models (LSTM, ARIMA)
- Use of past price path or similar markets

---

## 2. Collaborative Filtering (CF) — Do You Need It?

**What CF usually does:** Uses a user–item (or entity–entity) interaction matrix to fill missing values or recommend items. “Users like you liked X.”

**Relevant angles for prediction markets:**

| Angle | What it would do | Fit for your bot? |
|-------|-------------------|--------------------|
| **Market–market similarity** | “Markets similar to this one (category, end date, liquidity) had resolution / path pattern X.” Use that to refine probability or confidence. | **Maybe later.** Helps when you have many resolved markets and want to borrow strength (e.g. new market with little history, or to adjust AI estimate using similar resolved events). |
| **User–market (trader behavior)** | “Traders similar to you made money on these markets.” | **Low.** You’re building an autonomous bot, not a recommender for human traders. |
| **Cross-market / cross-platform** | Same event on multiple platforms; use one platform’s price to inform another (arbitrage or calibration). | **Possible.** You already list “cross-market duplicate questions” as a category; CF-like “same event, other market” could support arbitrage or calibration, but that’s more “same event” matching than classic CF. |

**Verdict on CF:**

- **Not needed for your current fade/arbitrage logic.** Those strategies depend on current price and a single probability estimate, not on “similar markets” or user behavior.
- **Worth considering later if** you (1) add a “similar markets” or “analog events” module, (2) want to calibrate or refine probabilities using resolved analogues, or (3) explicitly model cross-market duplicates. Then something **CF-like** (e.g. k-NN over market features, or a small matrix over event types) could be useful. Classic matrix-factorization CF is optional and only if you introduce a clear user/item or market–market interaction structure.

---

## 3. Time Series & Forecasting (LSTM, ARIMA) — Do You Need Them?

**What they do:**

- **ARIMA:** Linear model for univariate series (trend + seasonality + noise). Good for short-horizon forecasts when the series is relatively smooth and not bounded.
- **LSTM:** Nonlinear sequence model. Can capture long-range dependencies and complex patterns; needs more data and tuning; risk of overfitting on thin series.

**How prediction market prices behave:**

- Bounded in (0, 1); often mean-reverting or jumping to 0 or 1 at resolution.
- Many markets have short life (days/weeks) and sparse liquidity → short, noisy series per market.
- Information is event-driven (news, polls); price path is not purely “smooth time series.”

**Possible uses in your bot:**

| Use case | ARIMA | LSTM | Comment |
|----------|--------|------|--------|
| **Next-bar or short-horizon price** | Possible with transform (e.g. logit); simple. | Possible; needs enough data and regularization. | Only useful if you **change strategy** to “time entries using predicted move” (e.g. “price will drop in 1h, wait to fade”). Right now you don’t use forecasts. |
| **Regime / volatility** | Can model variance (e.g. GARCH). | Can learn “regime” from sequence. | Could support “fade more when reversion regime” or “tighten size when volatile.” Optional enhancement. |
| **Path to resolution** | Less natural (bounded, jumpy). | Theoretically possible (e.g. “path of similar markets”). | High complexity; “similar markets” is closer to CF-like ideas than to raw LSTM on one series. |
| **Replacing current “true prob”** | Not a direct replacement (ARIMA predicts price, not latent prob). | Could predict probability or move; heavy for backtest proxy. | Your backtest proxy is intentionally simple; a full LSTM “true prob” would need retraining, validation, and integration. |

**Verdict on LSTM / ARIMA:**

- **Not required for your current design.** Your strategies do not use forecasts or past paths; they use current price and a rule-based (or future LLM) probability. Adding LSTM/ARIMA without a clear use case (e.g. entry timing, regime filter) would add complexity without immediate benefit.
- **ARIMA:** Easiest to try if you ever want a **simple forecast** (e.g. next 1h price or short-horizon move) for entry timing or filtering. Use a transformed series (e.g. logit(price)) and keep horizons short.
- **LSTM:** Only consider if you (1) have or aggregate enough history (e.g. many markets or long series), (2) define a clear target (e.g. “revert in 24h” or “direction next bar”), and (3) are willing to maintain training, validation, and backtest integration. Risk of overfitting is high on thin markets.

---

## 4. Summary Table

| Component | Need now? | When it could help | Effort |
|-----------|-----------|--------------------|--------|
| **Collaborative filtering** | No | Similar-markets / analog calibration; cross-market duplicates; cold start. | Medium (define “similar”, build features, optional matrix/embedding). |
| **ARIMA** | No | Short-horizon price forecast for entry timing or simple regime. | Low–medium (transform, fit, plug into strategy if you add “wait for forecast” logic). |
| **LSTM** | No | Richer sequence model for path or regime; needs clear target and data. | High (data, training, validation, integration). |

---

## 5. Recommendations

1. **Keep current design as the base.** Your fade/arbitrage logic and backtest are cross-sectional and rule-based by design; that’s appropriate for validating edge and execution.
2. **Before adding models:**  
   - **Improve data and backtest first:** 2024 slug list, validation, stress tests, multi-period and train/test. You’re already doing this.  
   - **Define a concrete question**, e.g. “We want to time fade entries using a 1h-ahead price forecast” or “We want to weight signals by similarity to resolved analogues.” Without that, CF/LSTM/ARIMA add complexity without a clear payoff.
3. **If you experiment next:**  
   - **ARIMA** (or a simple moving average / mean-reversion forecast on logit(price)) is the lowest-friction way to try “short-horizon forecast” for entry timing.  
   - **“Similar markets” (CF-like)** is the most natural next step if you want to use resolved history to refine probability or confidence (e.g. “elections with similar poll spread resolved YES 70% of the time”).  
   - **LSTM** last: only after you have a clear target, enough data, and a reason the simpler ARIMA or rule-based approach is insufficient.

---

## 6. References (for when you do look deeper)

- **ARIMA:** `statsmodels` (Python): `ARIMA`, `SARIMAX`; use `scipy.special.logit` / `expit` for bounded price.
- **LSTM:** `torch` or `tensorflow`; consider sequence length and regularization; validate on out-of-sample periods and multiple markets.
- **CF / similarity:** Off-the-shelf (e.g. `surprise`, `implicit`) or custom k-NN / embeddings on market features (category, end_date, liquidity, wording); focus on “market–market” or “event–event” similarity, not user–item.
