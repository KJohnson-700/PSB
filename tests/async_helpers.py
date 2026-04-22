"""Helpers for running async code from synchronous pytest tests (Python 3.10+ safe)."""
import asyncio


def run_async(coro):
    return asyncio.run(coro)
