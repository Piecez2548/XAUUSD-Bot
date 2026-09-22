"""Read-only MetaTrader 5 integration boundary.

This package intentionally exposes no order, deal, or position mutation API.
"""

from mt5.connection import MT5Connection, MT5ConnectionError

__all__ = ["MT5Connection", "MT5ConnectionError"]
