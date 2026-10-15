"""Stage 6: decision engine (manager, exit rules) and order authorizer."""

from scheme1.decision.authorize import SizingConfig, authorize
from scheme1.decision.manager import STRATEGY_VERSION, OpenPosition, StrategyConfig, combine, decide

__all__ = ["STRATEGY_VERSION", "OpenPosition", "SizingConfig", "StrategyConfig", "authorize", "combine", "decide"]
