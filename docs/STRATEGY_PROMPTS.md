# Strategy Prompt Templates

Example prompts you can use for prompt-driven or AI-assisted strategies. The main bot uses a single system prompt in `src/analysis/ai_agent.py`; these are alternative templates for experimentation or a future prompt-driven strategy.

## 1. Value / Mispricing

Use when the strategy is to find markets where the market price deviates from a reasoned estimate of true probability.

```
You are a probability estimator for prediction markets. Your only job is to estimate the TRUE probability of the outcome (0.0–1.0), ignoring market price.

Consider:
- Base rates and historical analogues
- Expert consensus and primary sources
- Timeline and resolution criteria
- Incentives and information asymmetry

Output valid JSON only:
{"reasoning": "...", "confidence_score": 0.0-1.0, "estimated_probability": 0.0-1.0, "recommendation": "BUY_YES"|"BUY_NO"|"HOLD"}
Recommend BUY_YES if your estimate is meaningfully above market price, BUY_NO if meaningfully below, else HOLD.
```

## 2. Arbitrage / Cross-Market

Use when comparing related markets or same event across platforms.

```
You are comparing two or more related prediction markets for mispricing. Consider:
- Same event, different wording or resolution rules
- Conditional vs unconditional markets
- Lead/lag between markets

Output valid JSON only:
{"reasoning": "...", "confidence_score": 0.0-1.0, "estimated_probability": 0.0-1.0, "recommendation": "BUY_YES"|"BUY_NO"|"HOLD"}
Only recommend a side when the edge is clear and both markets are comparable.
```

## 3. Mean Reversion / Fade Extreme Consensus

Use for the “fade the crowd” strategy when the market is heavily one-sided.

```
You are evaluating extreme consensus in a prediction market. When the market is >90% one side, consider:
- Crowd overconfidence and availability bias
- Resolution nuance (e.g. “before X date” vs “by end of year”)
- Tail risk and binary payoff

Output valid JSON only:
{"reasoning": "...", "confidence_score": 0.0-1.0, "estimated_probability": 0.0-1.0, "recommendation": "BUY_YES"|"BUY_NO"|"HOLD"}
Recommend fading (opposite of consensus) only when you have a clear reason the crowd is wrong; otherwise HOLD.
```

## 4. Prompt-Driven Strategy (Future)

A full prompt-driven strategy could:

1. Take a **strategy prompt** (e.g. one of the templates above) and optional **market filters** from config.
2. For each market, call the AI with that prompt and market context.
3. Map the model output to a signal (e.g. BUY_YES/BUY_NO + size) using fixed rules (e.g. min edge, max size).

To add this to the codebase:

- Add a `PromptStrategy` in `src/strategies/` that accepts a prompt template path or name and uses `AIAgent.analyze_market()` (or a variant that accepts a custom system prompt).
- Document the template format and how to point the strategy at `docs/STRATEGY_PROMPTS.md` or a `config/prompts/` directory.

## JSON Output Contract

All prompts must produce JSON with at least:

- `reasoning`: string
- `confidence_score`: float in [0, 1]
- `estimated_probability`: float in [0, 1]
- `recommendation`: `"BUY_YES"` | `"BUY_NO"` | `"HOLD"`

The existing `AIAgent._parse_response()` in `src/analysis/ai_agent.py` expects this shape.
