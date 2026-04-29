"""Models for the weather AI ensemble."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Dict


@dataclass
class WeatherAgentResult:
    """One role-specific AI estimate inside the weather ensemble."""

    role: str
    reasoning: str
    confidence_score: float
    estimated_probability: float
    recommendation: str


@dataclass
class WeatherEnsembleDecision:
    """Aggregated weather ensemble output."""

    reasoning: str
    confidence_score: float
    estimated_probability: float
    recommendation: str
    agent_results: Dict[str, WeatherAgentResult]
    weighted_probability: float
    disagreement_std: float
    agent_count: int
    elapsed_seconds: float

    def to_signal_payload(self) -> Dict[str, object]:
        """Serialize for journaling / signal attachment."""
        return {
            "reasoning": self.reasoning,
            "confidence_score": self.confidence_score,
            "estimated_probability": self.estimated_probability,
            "recommendation": self.recommendation,
            "agent_results": {
                role: asdict(result) for role, result in self.agent_results.items()
            },
            "weighted_probability": self.weighted_probability,
            "disagreement_std": self.disagreement_std,
            "agent_count": self.agent_count,
            "elapsed_seconds": self.elapsed_seconds,
        }
