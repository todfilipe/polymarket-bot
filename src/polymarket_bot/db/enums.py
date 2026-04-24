import sys

if sys.version_info >= (3, 11):
    from enum import StrEnum
else:
    from enum import Enum

    class StrEnum(str, Enum):
        pass


class TradeSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class WalletTier(StrEnum):
    TOP = "TOP"
    BOTTOM = "BOTTOM"


class BotTradeStatus(StrEnum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class PositionStatus(StrEnum):
    OPEN = "OPEN"
    PARTIALLY_CLOSED = "PARTIALLY_CLOSED"
    CLOSED = "CLOSED"


class CircuitBreakerStatus(StrEnum):
    NORMAL = "NORMAL"
    PAUSED = "PAUSED"
    RECOVERY = "RECOVERY"
    HALTED = "HALTED"
