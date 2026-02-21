"""
core/trading_engine.py - Core trading logic

Responsibilities:
- Evaluate entry signals from price updates
- Place buy orders when conditions met
- Monitor open positions for exit conditions
- Execute sells (profit target / forced time / safety)
- Track all positions and P&L
- Never hold into resolution
"""

import asyncio
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from config import AppConfig
from core.models import (
    BotStats, ExitReason, Market, OrderSide, Position,
    PositionStatus, TradeSignal
)
from exchange.polymarket_client import PolymarketRESTClient
from exchange.websocket_feed import OrderbookState
from utils.logger import get_logger

logger = get_logger("trading_engine")


class TradingEngine:
    """
    Central trading engine.

    - Receives price updates from WebSocket feed
    - Decides when to enter / exit positions
    - Enforces all safety rules
    - Never holds to settlement
    """

    def __init__(self, config: AppConfig, rest_client: PolymarketRESTClient):
        self.cfg = config.trading
        self.rest = rest_client

        self.active_markets: dict[str, Market] = {}
        self.open_positions: dict[str, Position] = {}  # position_id -> Position
        self.closed_positions: list[Position] = []
        self.stats = BotStats()

        self._running = False
        self._monitor_task: Optional[asyncio.Task] = None

        # Notification callback (set by Telegram bot)
        self.on_position_opened = None   # async (position) callback
        self.on_position_closed = None   # async (position) callback
        self.on_alert = None             # async (msg: str) callback

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    async def start(self):
        self._running = True
        self._monitor_task = asyncio.create_task(self._position_monitor_loop())
        logger.info(
            f"Trading engine started | paper={self.cfg.paper_trading} | "
            f"buy_threshold={self.cfg.buy_probability_threshold} | "
            f"sell_target={self.cfg.sell_probability_target}"
        )

    async def stop(self):
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
        # Force-exit all open positions
        for pos in list(self.open_positions.values()):
            await self._exit_position(pos, ExitReason.MANUAL)
        logger.info("Trading engine stopped")

    # -------------------------------------------------------------------------
    # Market Management
    # -------------------------------------------------------------------------

    def register_market(self, market: Market):
        self.active_markets[market.market_id] = market
        logger.info(f"Market registered: {market.market_id[:12]}... | {market.question[:50]}")

    def remove_market(self, market_id: str):
        self.active_markets.pop(market_id, None)

    # -------------------------------------------------------------------------
    # Price Update Handler (called from WebSocket feed)
    # -------------------------------------------------------------------------

    async def on_price_update(self, token_id: str, book: OrderbookState):
        """
        Main callback from WebSocket feed.
        Called on every price update — must be fast.
        """
        if not self._running:
            return

        # Update any open positions watching this token
        await self._check_exit_conditions_for_token(token_id, book)

        # Check if this token represents an entry opportunity
        await self._evaluate_entry(token_id, book)

    # -------------------------------------------------------------------------
    # Entry Logic
    # -------------------------------------------------------------------------

    async def _evaluate_entry(self, token_id: str, book: OrderbookState):
        """Check whether to enter a position on this token."""
        # Find which market / outcome this token belongs to
        market, outcome = self._find_market_by_token(token_id)
        if market is None or market.paused:
            return

        # Skip if market is too close to expiry to enter
        seconds_left = market.seconds_to_close
        if seconds_left < (self.cfg.forced_exit_seconds_before_close + 30):
            return  # Not enough time to enter and exit safely

        # Skip if we already have a position in this market
        if self._has_position_in_market(market.market_id):
            return

        # Skip if at max concurrent positions
        if len(self.open_positions) >= self.cfg.max_concurrent_positions:
            return

        # Entry condition: ask price below threshold
        ask = book.best_ask
        if ask <= 0 or ask > self.cfg.buy_probability_threshold:
            return

        # Safety checks before entry
        if not self._passes_safety_checks(book, is_entry=True):
            return

        # Calculate order size (buy as many shares as budget allows)
        budget = self.cfg.max_position_size_usd
        shares = budget / ask  # shares = dollars / price
        shares = round(shares, 2)

        if shares <= 0:
            return

        logger.info(
            f"ENTRY SIGNAL | token={token_id[:12]}... | outcome={outcome} | "
            f"ask={ask:.4f} | shares={shares:.2f} | market={market.question[:40]}"
        )

        await self._enter_position(market, token_id, outcome, ask, shares)

    async def _enter_position(
        self,
        market: Market,
        token_id: str,
        outcome: str,
        ask_price: float,
        shares: float,
    ):
        """Place a buy order and record position."""
        # Apply slippage tolerance: buy at ask + slippage
        limit_price = round(min(ask_price * (1 + self.cfg.slippage_tolerance), 0.99), 4)

        order_resp = await self.rest.place_limit_order(
            token_id=token_id,
            side=OrderSide.BUY.value,
            price=limit_price,
            size=shares,
        )

        if order_resp is None:
            logger.error(f"Entry order failed for {token_id[:12]}...")
            return

        # Confirm fill (paper: immediate; live: check status)
        filled_price = float(order_resp.get("price", limit_price))
        filled_shares = float(order_resp.get("filled", shares))

        if filled_shares <= 0:
            logger.warning(f"Order placed but zero fill: {order_resp}")
            return

        position = Position(
            position_id=str(uuid.uuid4()),
            market_id=market.market_id,
            token_id=token_id,
            outcome=outcome,
            entry_price=filled_price,
            shares=filled_shares,
            entry_time=datetime.now(tz=timezone.utc),
            entry_order_id=order_resp.get("order_id") or order_resp.get("orderID"),
            status=PositionStatus.PAPER if self.cfg.paper_trading else PositionStatus.OPEN,
            paper=self.cfg.paper_trading,
        )

        self.open_positions[position.position_id] = position
        self.stats.total_volume_usd += position.cost_basis

        logger.info(
            f"POSITION OPENED | id={position.position_id[:8]} | "
            f"outcome={outcome} | price={filled_price:.4f} | "
            f"shares={filled_shares:.2f} | cost=${position.cost_basis:.2f} | "
            f"paper={position.paper}"
        )

        if self.on_position_opened:
            await self.on_position_opened(position, market)

    # -------------------------------------------------------------------------
    # Exit Logic
    # -------------------------------------------------------------------------

    async def _check_exit_conditions_for_token(self, token_id: str, book: OrderbookState):
        """
        For all open positions in this token, check if exit conditions are met.
        This is called on every price tick — critical path.
        """
        for pos in list(self.open_positions.values()):
            if pos.token_id != token_id:
                continue
            if pos.status in (PositionStatus.PENDING_EXIT, PositionStatus.CLOSED):
                continue

            market = self.active_markets.get(pos.market_id)
            if market is None:
                continue

            exit_reason = self._determine_exit_reason(pos, market, book)
            if exit_reason:
                await self._exit_position(pos, exit_reason, book)

    def _determine_exit_reason(
        self,
        pos: Position,
        market: Market,
        book: OrderbookState,
    ) -> Optional[ExitReason]:
        """
        Evaluate all exit conditions. Returns reason if we should exit, else None.

        Priority order (fastest exits first):
        1. Forced time exit (imminent expiry)
        2. Safety exits
        3. Profit target
        """
        current_bid = book.best_bid
        seconds_left = market.seconds_to_close

        # 1. FORCED TIME EXIT — highest priority
        if seconds_left <= self.cfg.forced_exit_seconds_before_close:
            logger.warning(
                f"FORCED TIME EXIT | pos={pos.position_id[:8]} | "
                f"seconds_left={seconds_left:.1f}"
            )
            return ExitReason.FORCED_TIME

        # 2. SAFETY — market closing early (unexpected)
        if seconds_left <= 0:
            return ExitReason.FORCED_TIME

        # 3. SAFETY — liquidity collapsed
        bid_liq = book.bid_liquidity()
        if bid_liq < self.cfg.safety_exit_liquidity_threshold:
            logger.warning(
                f"SAFETY EXIT (low liquidity) | pos={pos.position_id[:8]} | "
                f"bid_liq=${bid_liq:.2f}"
            )
            return ExitReason.SAFETY_LIQUIDITY

        # 4. SAFETY — spread too wide (can't exit cleanly)
        if book.spread_pct > self.cfg.max_spread_pct:
            logger.warning(
                f"SAFETY EXIT (wide spread) | pos={pos.position_id[:8]} | "
                f"spread={book.spread_pct:.2%}"
            )
            return ExitReason.SAFETY_SPREAD

        # 5. SAFETY — price stalled near zero (order won't fill)
        if current_bid < pos.entry_price * 0.3 and seconds_left < 60:
            return ExitReason.SAFETY_STALL

        # 6. PROFIT TARGET — bid crossed our sell target
        if current_bid >= self.cfg.sell_probability_target:
            logger.info(
                f"PROFIT TARGET HIT | pos={pos.position_id[:8]} | "
                f"entry={pos.entry_price:.4f} | bid={current_bid:.4f} | "
                f"target={self.cfg.sell_probability_target:.4f}"
            )
            return ExitReason.PROFIT_TARGET

        return None

    async def _exit_position(
        self,
        pos: Position,
        reason: ExitReason,
        book: Optional[OrderbookState] = None,
    ):
        """Execute exit order for a position."""
        if pos.status == PositionStatus.CLOSED:
            return

        pos.status = PositionStatus.PENDING_EXIT

        # Determine sell price
        if book:
            # Sell at best bid (market-like), with small slippage buffer
            sell_price = round(max(book.best_bid * (1 - self.cfg.slippage_tolerance), 0.01), 4)
        else:
            # Fallback: use a safe low price to guarantee fill
            sell_price = round(max(pos.entry_price * 0.5, 0.01), 4)

        logger.info(
            f"EXITING | pos={pos.position_id[:8]} | reason={reason.value} | "
            f"shares={pos.shares:.2f} | sell_price={sell_price:.4f}"
        )

        order_resp = await self.rest.place_limit_order(
            token_id=pos.token_id,
            side=OrderSide.SELL.value,
            price=sell_price,
            size=pos.shares,
        )

        if order_resp is None:
            logger.error(f"Exit order failed for {pos.position_id[:8]} — retrying at lower price")
            # Emergency exit at lower price
            sell_price = round(sell_price * 0.9, 4)
            order_resp = await self.rest.place_limit_order(
                token_id=pos.token_id,
                side=OrderSide.SELL.value,
                price=sell_price,
                size=pos.shares,
            )

        if order_resp:
            filled_exit = float(order_resp.get("price", sell_price))
        else:
            filled_exit = sell_price  # Paper fallback

        pos.exit_price = filled_exit
        pos.exit_time = datetime.now(tz=timezone.utc)
        pos.exit_order_id = order_resp.get("order_id") if order_resp else None
        pos.exit_reason = reason
        pos.status = PositionStatus.CLOSED

        # Update stats
        pnl = pos.realized_pnl or 0.0
        self.stats.total_trades += 1
        self.stats.total_pnl_usd += pnl
        if reason == ExitReason.FORCED_TIME:
            self.stats.forced_exits += 1
        elif reason in (ExitReason.SAFETY_LIQUIDITY, ExitReason.SAFETY_SPREAD, ExitReason.SAFETY_STALL):
            self.stats.safety_exits += 1
        if pnl > 0:
            self.stats.winning_trades += 1
        else:
            self.stats.losing_trades += 1

        # Move to closed list
        del self.open_positions[pos.position_id]
        self.closed_positions.append(pos)

        logger.info(
            f"POSITION CLOSED | id={pos.position_id[:8]} | reason={reason.value} | "
            f"pnl=${pnl:+.4f} ({pos.realized_pnl_pct:+.1f}%) | "
            f"entry={pos.entry_price:.4f} | exit={filled_exit:.4f}"
        )

        if self.on_position_closed:
            await self.on_position_closed(pos)

        if self.on_alert and reason in (ExitReason.SAFETY_LIQUIDITY, ExitReason.SAFETY_SPREAD):
            await self.on_alert(f"⚠️ Safety exit triggered: {reason.value} for {pos.position_id[:8]}")

    # -------------------------------------------------------------------------
    # Position Monitor Loop (time-based safety net)
    # -------------------------------------------------------------------------

    async def _position_monitor_loop(self):
        """
        Runs every second to enforce time-based exits.
        This is a safety net in case price ticks slow down near expiry.
        """
        while self._running:
            try:
                for pos in list(self.open_positions.values()):
                    if pos.status == PositionStatus.PENDING_EXIT:
                        continue

                    market = self.active_markets.get(pos.market_id)
                    if market is None:
                        # Market no longer tracked - force exit
                        await self._exit_position(pos, ExitReason.FORCED_TIME)
                        continue

                    seconds_left = market.seconds_to_close

                    if seconds_left <= self.cfg.forced_exit_seconds_before_close:
                        await self._exit_position(pos, ExitReason.FORCED_TIME)
                    elif seconds_left < 0:
                        # Market expired — should never hold here
                        logger.critical(
                            f"CRITICAL: Position {pos.position_id[:8]} held past expiry! "
                            "Forcing exit immediately."
                        )
                        await self._exit_position(pos, ExitReason.FORCED_TIME)

            except Exception as e:
                logger.error(f"Monitor loop error: {e}")

            await asyncio.sleep(1)

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _find_market_by_token(self, token_id: str) -> tuple[Optional[Market], str]:
        for market in self.active_markets.values():
            if market.token_id_yes == token_id:
                return market, "YES"
            if market.token_id_no == token_id:
                return market, "NO"
        return None, ""

    def _has_position_in_market(self, market_id: str) -> bool:
        return any(p.market_id == market_id for p in self.open_positions.values())

    def _passes_safety_checks(self, book: OrderbookState, is_entry: bool = True) -> bool:
        """Pre-trade safety checks."""
        # Spread check
        if book.spread_pct > self.cfg.max_spread_pct:
            logger.debug(f"Safety: spread too wide ({book.spread_pct:.2%})")
            return False

        # Liquidity check
        ask_liq = book.ask_liquidity()
        if ask_liq < self.cfg.safety_exit_liquidity_threshold:
            logger.debug(f"Safety: insufficient ask liquidity (${ask_liq:.2f})")
            return False

        return True

    def get_summary(self) -> dict:
        return {
            "open_positions": len(self.open_positions),
            "total_trades": self.stats.total_trades,
            "win_rate": self.stats.win_rate,
            "total_pnl": self.stats.total_pnl_usd,
            "forced_exits": self.stats.forced_exits,
            "safety_exits": self.stats.safety_exits,
            "paper_trading": self.cfg.paper_trading,
        }
