# AI Provider Integration Guide

This document outlines the architecture for integrating and managing multiple Large Language Model (LLM) providers within the PolyBot.

## 1. Core Concepts

The system is designed to be resilient and cost-effective by using a configurable "provider chain". If the primary AI provider fails, the system automatically falls back to the next provider in the chain.

### Provider Chain

The entire logic is controlled by the `provider_chain` list in `config/settings.yaml`. The bot will attempt to call the providers in the order they are listed.

**Example from `config/settings.yaml`:**
```yaml
ai:
  # ...
  provider_chain:
    - name: minimax_primary
      type: minimax
      model: "abab5.5-chat"
      api_key_secret: MINIMAX_API_KEY
    - name: google_fallback
      type: google
      model: "gemini-1.5-flash-001"
      api_key_secret: GOOGLE_API_KEY # Note: For Vertex AI, this is used as a placeholder
```

- **`name`**: A unique identifier for the provider instance.
- **`type`**: The provider type. This MUST correspond to a method in `AIAgent` (e.g., `type: "google"` maps to `_analyze_with_google`).
- **`model`**: The specific model to use for that provider.
- **`api_key_secret`**: The name of the environment variable that holds the API key, as defined in `config/secrets.env`.
- **`base_url`** (optional): For OpenAI-compatible hosts (e.g. **OpenRouter**). When set, the agent uses `AsyncOpenAI(api_key=..., base_url=...)` instead of the default OpenAI API.
- **`models`** (optional): Ordered list of model IDs to try on this provider (rotation on failure, empty JSON, or rate limits). If omitted, **`model`** is used as a single candidate.
- **`json_mode`** (optional, default `true`): When `true`, requests use `response_format: json_object` (not supported by every free model; set `false` to retry without it).
- **`http_referer`** / **`x_title`** (optional): OpenRouter [recommends](https://openrouter.ai/docs) `HTTP-Referer` and `X-Title` for attribution.

### OpenRouter (free-tier models)

1. Create an API key at [openrouter.ai](https://openrouter.ai) and add `OPENROUTER_API_KEY` to `config/secrets.env`.
2. In `config/settings.yaml`, set `ai.enabled: true` and add a chain entry with `type: openai`, `base_url: "https://openrouter.ai/api/v1"`, `api_key_secret: OPENROUTER_API_KEY`, and one or more `:free` model IDs under `models:`.
3. Optionally set `ai.free_tier_daily_request_budget` to a positive integer to log **WARNING** when local daily usage crosses **50%** and **75%** of that budget (in addition to header-based warnings when `x-ratelimit-*` headers are present).

Example:

```yaml
ai:
  enabled: true
  free_tier_daily_request_budget: 200
  provider_chain:
    - name: openrouter_free
      type: openai
      base_url: "https://openrouter.ai/api/v1"
      api_key_secret: OPENROUTER_API_KEY
      http_referer: "https://your-site.example"
      x_title: "Polymarket Bot"
      models:
        - "google/gemma-2-9b-it:free"
        - "meta-llama/llama-3.2-3b-instruct:free"
      cooldown_on_429_seconds: 90
    - name: gemini_fallback
      type: google
      model: "gemini-2.0-flash"
      api_key_secret: GOOGLE_API_KEY
```

After OpenRouter free models are exhausted for a given request, the agent continues to the **next** `provider_chain` entry (e.g. Google Gemini, Groq) if configured.

## 2. API Key Management

- All API keys are stored in `config/secrets.env`.
- The `main.py` script loads these keys at startup.
- The `AIAgent` receives a dictionary of all available keys and uses the `api_key_secret` from the provider chain to select the correct one for each call.

## 3. Provider-Specific Implementations

The `AIAgent` class in `src/analysis/ai_agent.py` contains the specific logic for handling each provider.

### Minimax Integration

- **SDK:** `anthropic`
- **Method:** `_analyze_with_minimax`
- **Key Detail:** Minimax's API is compatible with the Anthropic SDK. The integration is achieved by passing a custom `base_url` to the `anthropic.AsyncAnthropic` client.

```python
# Inside _analyze_with_minimax in ai_agent.py
client = anthropic.AsyncAnthropic(
    api_key=api_key,
    base_url="https://api.minimax.io/v1/text/chatcompletion-anthropic"
)
```

### Google Gemini (Vertex AI) Integration

- **SDK:** `google-cloud-aiplatform`
- **Method:** `_analyze_with_google`
- **Key Details:**
    - This integration uses Google Cloud's Vertex AI platform, which is the current standard for using Gemini models.
    - It does **not** use the API key directly in the call. Instead, it relies on Google Cloud's Application Default Credentials (ADC). You must be authenticated with `gcloud` on the machine running the bot (`gcloud auth application-default login`).
    - The `config/secrets.env` file must contain the `GOOGLE_PROJECT_ID` and `GOOGLE_LOCATION` for your Google Cloud project.

```python
# Inside _analyze_with_google in ai_agent.py
vertexai.init(project=self.api_keys.get("GOOGLE_PROJECT_ID"), location=self.api_keys.get("GOOGLE_LOCATION"))
model = GenerativeModel(model_name=model_name)
response = await model.generate_content_async(...)
```

## 4. How to Add a New Provider (e.g., "Groq")

1.  **Install SDK:** Add the provider's Python SDK to `requirements.txt` (e.g., `pip install groq`).
2.  **Add to `secrets.env.example`:** Add the API key name (e.g., `GROQ_API_KEY=""`).
3.  **Update `settings.yaml`:** Add the new provider to the `provider_chain`.
    ```yaml
    - name: groq_fast_fallback
      type: groq
      model: "llama3-8b-8192"
      api_key_secret: GROQ_API_KEY
    ```
4.  **Implement in `AIAgent`:** Create a new `async def _analyze_with_groq(self, ...)` method in `src/analysis/ai_agent.py` that contains the specific logic for calling the Groq SDK.
5.  **Update `main.py`:** Ensure the new API key (`GROQ_API_KEY`) is loaded from the environment variables into the `api_keys` dictionary.

## 5. LLM quota controls (global, per-strategy, dashboard)

**Goal:** Reduce surprise LLM usage (free-tier limits, rate limits, spend) without turning off Polymarket, scanners, or price feeds.

### Effective LLM call

A strategy only calls `analyze_market` when all of the following hold:

- **`ai.enabled`** is `true` (global “quant mode” when `false`: no LLM calls).
- **`provider_chain`** is non-empty (`AIAgent.is_available()`).
- **`strategies.<name>.use_ai`** is `true` where the strategy supports it (Fade ambiguous band; BTC/SOL marginal / no-threshold; Arbitrage blend / non-crypto path — see `settings.yaml`).

Optional **`strategies.<name>.enabled`** narrows which strategies run in the main loop (fewer strategies ⇒ fewer optional LLM call sites).

### Low-call knobs (`config/settings.yaml` → `ai`)

| Key | Role |
| --- | --- |
| `min_call_gap` | Minimum seconds between LLM API calls. |
| `cache_ttl` | Cache analyses per market to avoid repeat calls. |
| `free_tier_daily_request_budget` | Local soft budget; logs warnings at 50% / 75%. |

Per-strategy caps (e.g. BTC/SOL `max_ai_calls_per_scan`) limit how many LLM tie-breakers run per scan cycle.

### Operator workflow (recommended)

1. **Discovery (short):** `ai.enabled: true`, a real `provider_chain`, only the strategies you are evaluating with **`enabled: true`**. Watch logs for 429s, rotation, usage. Answers: “Do I hit limits at my scan rate?”
2. **Sustained / quota-safe:** turn on **quant mode** (`ai.enabled: false`) and/or set **`use_ai: false`** per strategy, raise `min_call_gap`, lower `max_ai_calls_per_scan`, set `free_tier_daily_request_budget` for warnings.

### Dashboard live apply

Saving configuration from the dashboard (`POST /api/config`) writes to **`config/settings.yaml`** and, when the bot is connected to the dashboard process, **`PolyBot.apply_config_updates`** merges the same payload into the running bot and refreshes the **`AIAgent`** — so toggles like `ai.enabled` and `use_ai` take effect **without restarting** the bot.
