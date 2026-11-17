"""Stage 7: paper broker (simulation only) and its protective exits."""

from scheme1.broker.paper import BrokerConfig, BrokerResult, ExitPlan, PaperBroker, PositionGuard
from scheme1.broker.protective import ProtectiveExit, ProtectiveLevels, check_candle, follow

__all__ = ["BrokerConfig", "BrokerResult", "ExitPlan", "PaperBroker", "PositionGuard", "ProtectiveExit",
           "ProtectiveLevels", "check_candle", "follow"]
