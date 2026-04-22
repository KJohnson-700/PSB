"""
Usage Tracker
Module for tracking token usage and costs for different AI providers.
"""
import time
from collections import defaultdict
from dataclasses import dataclass, field
import threading

# Placeholder for actual cost-per-token data
# In a real-world scenario, this would be more sophisticated, possibly fetched from a config or an API
# Costs are per 1,000,000 tokens (input and output), USD.
# MiniMax: https://platform.minimax.io/docs/guides/pricing-paygo
PROVIDER_COSTS = {
    "minimax": {
        "MiniMax-M2.7": {"input": 0.30, "output": 1.20},
        "MiniMax-M2.7-highspeed": {"input": 0.60, "output": 2.40},
        "MiniMax-M2.5": {"input": 0.30, "output": 1.20},
        "MiniMax-M2.5-highspeed": {"input": 0.60, "output": 2.40},
    },
    "anthropic": {
        "claude-3-haiku-20240307": {"input": 0.25, "output": 1.25},
    },
    "google": {
        "gemini-pro": {"input": 0.0, "output": 0.0}, # Free tier
    },
    "groq": {
        "llama-3.3-70b-versatile": {"input": 0.0, "output": 0.0}, # Free tier
    },
    "openai": {
        "gpt-4o": {"input": 5.00, "output": 15.00},
        "gpt-3.5-turbo": {"input": 0.50, "output": 1.50},
    },
}

@dataclass
class APIUsageRecord:
    """Record of a single API call."""
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cost: float
    latency: float # in seconds
    timestamp: float = field(default_factory=time.time)

class UsageTracker:
    """
    A thread-safe class to track API usage and costs for multiple AI providers.
    """
    def __init__(self):
        self.records = []
        self.lock = threading.Lock()

    def _calculate_cost(self, provider: str, model: str, input_tokens: int, output_tokens: int) -> float:
        """Calculates the cost of a single API call."""
        model_costs = PROVIDER_COSTS.get(provider, {}).get(model)
        if not model_costs:
            return 0.0
        
        input_cost = (input_tokens / 1_000_000) * model_costs.get("input", 0)
        output_cost = (output_tokens / 1_000_000) * model_costs.get("output", 0)
        
        return input_cost + output_cost

    def record_usage(self, provider: str, model: str, input_tokens: int, output_tokens: int, latency: float):
        """
        Records a single instance of API usage.

        Args:
            provider (str): The name of the AI provider (e.g., 'minimax', 'anthropic').
            model (str): The specific model used.
            input_tokens (int): The number of tokens in the prompt.
            output_tokens (int): The number of tokens in the completion.
            latency (float): The duration of the API call in seconds.
        """
        cost = self._calculate_cost(provider, model, input_tokens, output_tokens)
        record = APIUsageRecord(
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost=cost,
            latency=latency
        )
        with self.lock:
            self.records.append(record)

    def get_summary(self):
        """
        Provides a summary of all recorded API calls, aggregated by provider and model.
        """
        summary = defaultdict(lambda: defaultdict(lambda: {
            "total_calls": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_cost": 0.0,
            "total_latency": 0.0
        }))

        with self.lock:
            for record in self.records:
                agg = summary[record.provider][record.model]
                agg["total_calls"] += 1
                agg["total_input_tokens"] += record.input_tokens
                agg["total_output_tokens"] += record.output_tokens
                agg["total_cost"] += record.cost
                agg["total_latency"] += record.latency

        # Calculate averages
        for provider, models in summary.items():
            for model, agg in models.items():
                if agg["total_calls"] > 0:
                    agg["avg_latency"] = agg["total_latency"] / agg["total_calls"]
                else:
                    agg["avg_latency"] = 0

        return dict(summary)

    def get_all_records(self):
        """Returns a copy of all individual usage records."""
        with self.lock:
            return list(self.records)

# Singleton instance to be used across the application
usage_tracker = UsageTracker()