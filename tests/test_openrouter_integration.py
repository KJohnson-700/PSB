"""
Optional integration smoke: one OpenRouter OpenAI-compatible call (skipped without key).

Run: pytest tests/test_openrouter_integration.py -m integration
"""

import asyncio
import json
import os

import pytest

pytestmark = pytest.mark.integration


def test_openrouter_minimal_completion_parses():
    key = os.getenv("OPENROUTER_API_KEY")
    if not key:
        pytest.skip("OPENROUTER_API_KEY not set")

    async def _run():
        import openai

        client = openai.AsyncOpenAI(api_key=key, base_url="https://openrouter.ai/api/v1")
        response = await client.chat.completions.create(
            model="google/gemma-2-9b-it:free",
            messages=[
                {"role": "system", "content": "Reply with only valid JSON, no markdown."},
                {"role": "user", "content": '{"task":"ping"} — respond with {"ok":true}'},
            ],
            max_tokens=80,
            temperature=0,
        )
        text = (response.choices[0].message.content or "").strip()
        assert text
        start, end = text.find("{"), text.rfind("}")
        assert start != -1 and end > start
        data = json.loads(text[start : end + 1])
        assert isinstance(data, dict)

    asyncio.run(_run())
