"""
core/models.py - Domain models for the trading bot
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class PositionStatus(Enum):
    PENDING_ENTRY = "pending_entry"
    OPEN = "open"
    PENDING_EXIT = "pending_exit"
    CLOSED = "closed"
    FAILED = "failed"
    PAPER = "paper"


class OrderSide(Enum):
    BUY = "BUY"
    SELL = "SELL"


class ExitReason(Enum):
    PROFIT_TARGET = "profit_target"
    FORCED_TIME = "forced_time"
    SAFETY_LIQUIDITY = "safety_liquidity"
    SAFETY_SPREAD = "safety_spread"
    SAFETY_STALL = "safety_stall"
    MANUAL = "manual"
    MARKET_PAUSED = "market_paused"


@dataclass
class OrderbookSnapshot:
    token_id: str
    best_bid: float
    best_ask: float
    bid_liquidity_usd: float
    ask_liquidity_usd: float
    mid_price: float
    spread: float
    timestamp: datetime = field(default_factory=datetime.utcnow)

    @property
    def spread_pct(self) -> float:
        return self.spread / self.mid_price if self.mid_price > 0 else 1.0

    @property
    def book_imbalance(self) -> float:
        total = self.bid_liquidity_usd + self.ask_liquidity_usd
        return self.bid_liquidity_usd / total if total > 0 else 0.5


@dataclass
class Market:
    market_id: str              # Condition ID
    question: str
    token_id_yes: str
    token_id_no: str
    resolution_time: datetime
    is_active: bool = True
    paused: bool = False

    @property
    def seconds_to_close(self) -> float:
        return (self.resolution_time - datetime.utcnow()).total_seconds()

    @property
    def is_expiring_soon(self) -> bool:
        return self.seconds_to_close <= 60


@dataclass
class Position:
    position_id: str            # UUID
    market_id: str
    token_id: str
    outcome: str                # "YES" or "NO"
    entry_price: float
    shares: float
    entry_time: datetime
    entry_order_id: Optional[str] = None
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    exit_order_id: Optional[str] = None
    exit_reason: Optional[ExitReason] = None
    status: PositionStatus = PositionStatus.OPEN
    paper: bool = False

    @property
    def cost_basis(self) -> float:
        return self.entry_price * self.shares

    @property
    def unrealized_pnl(self) -> Optional[float]:
        return None  # Calculated externally from live price

    @property
    def realized_pnl(self) -> Optional[float]:
        if self.exit_price is None:
            return None
        return (self.exit_price - self.entry_price) * self.shares

    @property
    def realized_pnl_pct(self) -> Optional[float]:
        if self.exit_price is None or self.entry_price == 0:
            return None
        return (self.exit_price - self.entry_price) / self.entry_price * 100


@dataclass
class TradeSignal:
    market: Market
    token_id: str
    outcome: str
    current_price: float
    orderbook: OrderbookSnapshot
    signal_time: datetime = field(default_factory=datetime.utcnow)


@dataclass
class BotStats:
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl_usd: float = 0.0
    total_volume_usd: float = 0.0
    forced_exits: int = 0
    safety_exits: int = 0
    start_time: datetime = field(default_factory=datetime.utcnow)

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.winning_trades / self.total_trades * 100
