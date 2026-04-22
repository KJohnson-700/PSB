import os
import sys
import asyncio
from pathlib import Path

import anthropic

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))
from src.env_bootstrap import load_project_dotenv

load_project_dotenv(_ROOT)

# Minimax uses ANTHROPIC_API_KEY per Minimax docs (Anthropic-compatible endpoint)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

async def test_minimax():
    """Tests Minimax M2.5 via Anthropic-compatible endpoint (uses ANTHROPIC_API_KEY)."""
    print("--- Testing Minimax (ANTHROPIC_API_KEY) ---")
    if not ANTHROPIC_API_KEY:
        print("Result: FAILED - ANTHROPIC_API_KEY not found in environment (.env / secrets.env)")
        return

    try:
        client = anthropic.AsyncAnthropic(
            api_key=ANTHROPIC_API_KEY,
            base_url="https://api.minimax.io/anthropic"
        )
        # Try models in order (M2.5-highspeed may require higher plan tier)
        models_to_try = ["MiniMax-M2.5-highspeed", "MiniMax-M2.5", "MiniMax-M2"]
        response = None
        last_err = None
        for model in models_to_try:
            try:
                response = await client.messages.create(
                    model=model,
                    messages=[{"role": "user", "content": "Reply with only: OK"}],
                    max_tokens=20
                )
                print(f"  (used model: {model})")
                break
            except Exception as e:
                last_err = e
                if "not support model" in str(e):
                    continue
                raise
        if response is None:
            raise last_err or Exception("No supported Minimax model for your plan")
        text = None
        if response and response.content:
            for block in response.content:
                if hasattr(block, "text") and block.text:
                    text = block.text
                    break
                if hasattr(block, "thinking") and block.thinking:
                    text = block.thinking or "OK"  # fallback if only thinking
                    break
        if text:
            print("Result: SUCCESS - Minimax responded:", text[:50])
        else:
            print("Result: FAILED - API call succeeded but response was empty or invalid.")
    except Exception as e:
        print(f"Result: FAILED - {e}")

async def test_groq():
    """Tests Groq API (free tier, get key at console.groq.com/keys)."""
    print("\n--- Testing Groq (GROQ_API_KEY) ---")
    groq_key = os.getenv("GROQ_API_KEY")
    if not groq_key:
        print("Result: SKIPPED - GROQ_API_KEY not set (get free key at console.groq.com/keys)")
        return

    try:
        from groq import AsyncGroq
        client = AsyncGroq(api_key=groq_key)
        response = await client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": "Reply with only: OK"}],
            max_tokens=20,
        )
        text = response.choices[0].message.content
        if text:
            print("Result: SUCCESS - Groq responded:", text[:50])
        else:
            print("Result: FAILED - Response was empty.")
    except Exception as e:
        print(f"Result: FAILED - {e}")


async def test_polymarket_clob():
    """Tests Polymarket CLOB API (read-only)."""
    print("\n--- Testing Polymarket CLOB ---")
    try:
        from py_clob_client.client import ClobClient
        client = ClobClient("https://clob.polymarket.com")
        ok = client.get_ok()
        if ok:
            print("Result: SUCCESS - Polymarket CLOB is reachable.")
        else:
            print("Result: FAILED - CLOB returned not OK.")
    except Exception as e:
        print(f"Result: FAILED - {e}")


async def main():
    await test_minimax()
    await test_groq()
    await test_polymarket_clob()

if __name__ == "__main__":
    asyncio.run(main())
