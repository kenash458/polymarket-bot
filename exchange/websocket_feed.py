"""
exchange/websocket_feed.py - Polymarket WebSocket price feed

Subscribes to real-time orderbook updates via Polymarket's WebSocket API.
Implements auto-reconnect with exponential backoff.

Protocol: wss://ws-subscriptions-clob.polymarket.com/ws/
Subscription type: "market" channel provides real-time price ticks.
"""

import asyncio
import json
import time
from collections import defaultdict
from datetime import datetime
from typing import Callable, Optional

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from utils.logger import get_logger

logger = get_logger("ws_feed")


class PriceTick:
    __slots__ = ("token_id", "price", "size", "side", "timestamp")

    def __init__(self, token_id: str, price: float, size: float, side: str):
        self.token_id = token_id
        self.price = price
        self.size = size
        self.side = side
        self.timestamp = time.monotonic()


class OrderbookState:
    """
    Maintains a local orderbook replica from WebSocket deltas.
    Thread-safe via asyncio single-thread guarantee.
    """

    def __init__(self):
        self.bids: dict[float, float] = {}  # price -> size
        self.asks: dict[float, float] = {}
        self.last_update: float = 0.0

    def apply_delta(self, side: str, price: float, size: float):
        book = self.bids if side.upper() == "BUY" else self.asks
        if size == 0:
            book.pop(price, None)
        else:
            book[price] = size
        self.last_update = time.monotonic()

    @property
    def best_bid(self) -> float:
        return max(self.bids.keys()) if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return min(self.asks.keys()) if self.asks else 1.0

    @property
    def mid_price(self) -> float:
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread(self) -> float:
        return self.best_ask - self.best_bid

    def bid_liquidity(self, levels: int = 5) -> float:
        top = sorted(self.bids.items(), reverse=True)[:levels]
        return sum(p * s for p, s in top)

    def ask_liquidity(self, levels: int = 5) -> float:
        top = sorted(self.asks.items())[:levels]
        return sum(p * s for p, s in top)


class PolymarketWebSocketFeed:
    """
    Maintains a persistent WebSocket connection to Polymarket's price feed.

    Usage:
        feed = PolymarketWebSocketFeed(config)
        feed.on_tick = my_callback
        await feed.start(token_ids=["0xabc...", "0xdef..."])
    """

    def __init__(self, config):
        self.ws_url = config.network.ws_url
        self.reconnect_delay = config.ws_reconnect_delay_s
        self.heartbeat_interval = config.ws_heartbeat_interval_s

        self.orderbooks: dict[str, OrderbookState] = defaultdict(OrderbookState)
        self.subscribed_tokens: set[str] = set()

        self.on_price_update: Optional[Callable] = None  # async callback(token_id, book)

        self._ws = None
        self._running = False
        self._reconnect_attempts = 0
        self._lock = asyncio.Lock()

    async def start(self, token_ids: list[str]):
        """Start the WebSocket feed and subscribe to tokens."""
        self.subscribed_tokens.update(token_ids)
        self._running = True
        asyncio.create_task(self._connection_loop())
        logger.info(f"WebSocket feed starting for {len(token_ids)} tokens")

    async def subscribe(self, token_ids: list[str]):
        """Subscribe to additional tokens at runtime."""
        new_tokens = [t for t in token_ids if t not in self.subscribed_tokens]
        if not new_tokens:
            return
        self.subscribed_tokens.update(new_tokens)
        if self._ws and not self._ws.closed:
            await self._send_subscription(new_tokens)

    async def unsubscribe(self, token_ids: list[str]):
        """Unsubscribe from tokens."""
        for t in token_ids:
            self.subscribed_tokens.discard(t)
        # Note: Polymarket WS may not support explicit unsubscribe;
        # we just stop processing ticks for removed tokens.

    async def stop(self):
        self._running = False
        if self._ws:
            await self._ws.close()

    def get_book(self, token_id: str) -> Optional[OrderbookState]:
        return self.orderbooks.get(token_id)

    # -------------------------------------------------------------------------
    # Internal
    # -------------------------------------------------------------------------

    async def _connection_loop(self):
        """
        Polymarket removed public market websocket.
        This feed now acts as a placeholder so the bot can run using REST polling.
        """
        logger.warning("WebSocket disabled: Polymarket no longer provides public market WS feed.")
        while self._running:
            await asyncio.sleep(60)

    async def _connect_and_listen(self):
        logger.info(f"Connecting to WebSocket: {self.ws_url}")

        async with websockets.connect(
            self.ws_url,
            ping_interval=self.heartbeat_interval,
            ping_timeout=10,
            close_timeout=5,
            max_size=2**20,  # 1MB
        ) as ws:
            self._ws = ws
            logger.info("WebSocket connected")

            # Subscribe to all current tokens
            if self.subscribed_tokens:
                await self._send_subscription(list(self.subscribed_tokens))

            # Also start heartbeat task
            heartbeat_task = asyncio.create_task(self._heartbeat(ws))

            try:
                async for raw_msg in ws:
                    await self._handle_message(raw_msg)
            except ConnectionClosed as e:
                logger.warning(f"WS connection closed: {e}")
            finally:
                heartbeat_task.cancel()
                self._ws = None

    async def _send_subscription(self, token_ids: list[str]):
        """Send subscription message for market channel."""
        if not self._ws or self._ws.closed:
            return

        # Polymarket WS subscription format
        msg = {
            "type": "subscribe",
            "channel": "market",
            "markets": token_ids,
        }
        try:
            await self._ws.send(json.dumps(msg))
            logger.debug(f"Subscribed to {len(token_ids)} tokens")
        except Exception as e:
            logger.error(f"Subscription send failed: {e}")

    async def _heartbeat(self, ws):
        """Send periodic ping to keep connection alive."""
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            try:
                await ws.send(json.dumps({"type": "ping"}))
            except Exception:
                break

    async def _handle_message(self, raw: str):
        """Parse and dispatch incoming WebSocket messages."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = msg.get("event_type") or msg.get("type", "")

        if msg_type == "book":
            # Full book snapshot
            await self._handle_book_snapshot(msg)
        elif msg_type in ("price_change", "tick"):
            # Price delta
            await self._handle_price_tick(msg)
        elif msg_type == "pong":
            pass
        else:
            logger.debug(f"Unhandled WS message type: {msg_type}")

    async def _handle_book_snapshot(self, msg: dict):
        """Handle full orderbook snapshot."""
        asset_id = msg.get("asset_id") or msg.get("market", "")
        if asset_id not in self.subscribed_tokens:
            return

        book = self.orderbooks[asset_id]
        book.bids.clear()
        book.asks.clear()

        for bid in msg.get("bids", []):
            p, s = float(bid["price"]), float(bid["size"])
            if s > 0:
                book.bids[p] = s

        for ask in msg.get("asks", []):
            p, s = float(ask["price"]), float(ask["size"])
            if s > 0:
                book.asks[p] = s

        book.last_update = time.monotonic()

        if self.on_price_update:
            await self.on_price_update(asset_id, book)

    async def _handle_price_tick(self, msg: dict):
        """Handle price delta / tick update."""
        # Polymarket sends changes array
        changes = msg.get("changes", [])
        if not changes and "asset_id" in msg:
            # Single tick format
            changes = [msg]

        for change in changes:
            asset_id = change.get("asset_id") or change.get("market", "")
            if asset_id not in self.subscribed_tokens:
                continue

            side = change.get("side", "")
            price = float(change.get("price", 0))
            size = float(change.get("size", 0))

            self.orderbooks[asset_id].apply_delta(side, price, size)

            if self.on_price_update:
                await self.on_price_update(asset_id, self.orderbooks[asset_id])
