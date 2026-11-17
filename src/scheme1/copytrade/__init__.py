"""Copy trading (D22, D23): follow the trades of wallets the user chose. Simulation only."""

from scheme1.copytrade.parse import parse_trade
from scheme1.copytrade.strategy import COPY_STRATEGY_VERSION, CopyConfig, CopyMarket, exit_price, resolve_buy
from scheme1.copytrade.watcher import PollResult, Trader, TraderWatcher, load_traders

__all__ = ["COPY_STRATEGY_VERSION", "CopyConfig", "CopyMarket", "PollResult", "Trader", "TraderWatcher",
           "exit_price", "load_traders", "parse_trade", "resolve_buy"]
