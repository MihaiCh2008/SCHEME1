"""What the research agents read, and the names their evidence may cite.

Citable references
- a feature key, such as ``rsi_14`` (its value in the ``FeatureSnapshot``);
- ``validation.status``, ``validation.issues``, ``feature.warmup_complete``;
- ``safety.tradable`` and ``safety.<check name>``, such as ``safety.holder_concentration``.

A FACT may only cite references that exist, and a feature it cites must have a value (not ``None``).
"""

from __future__ import annotations

from dataclasses import dataclass

from scheme1.contracts import FeatureSnapshot, SafetyReport, ValidationReport


@dataclass(frozen=True)
class AgentInputs:
    features: FeatureSnapshot
    validation: ValidationReport
    safety: SafetyReport

    def __post_init__(self) -> None:
        if self.safety.symbol != self.features.symbol:
            raise ValueError(f"safety report is for {self.safety.symbol}, features for {self.features.symbol}")

    def f(self, key: str) -> float | None:
        """A feature value; ``None`` when not computable. An unknown key is a programming error."""
        return self.features.values[key]

    def citable(self) -> dict[str, object]:
        """Every reference an agent may cite, with its value."""
        refs: dict[str, object] = dict(self.features.values)
        refs["feature.warmup_complete"] = self.features.warmup_complete
        refs["validation.status"] = self.validation.status.value
        refs["validation.issues"] = len(self.validation.issues)
        refs["safety.tradable"] = self.safety.tradable
        refs.update({f"safety.{c.name}": c.status.value for c in self.safety.checks})
        return refs
