"""Stage 7: paper broker. So far: protective stop-loss and take-profit exits."""

from scheme1.broker.protective import ProtectiveExit, ProtectiveLevels, check_candle

__all__ = ["ProtectiveExit", "ProtectiveLevels", "check_candle"]
