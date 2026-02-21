"""
exchange/polymarket_client.py - Polymarket CLOB REST API client

Handles:
- Market discovery
- Orderbook snapshots
- Order placement / cancellation
- Order status polling
- Authentication (L2 API key auth via py_clob_client)
"""

import asyncio
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from utils.logger import get_logger

logger = get_logger("polymarket_client")


class PolymarketRESTClient:
    """
    Async REST client for Polymarket CLOB API.

    Auth: Uses L2 API key credentials derived from your EVM private key.
    Install py_clob_client for signing helpers:
        pip install py-clob-client
    """

    def __init__(self, config):
        self.cfg = config.network
        self.trading = config.trading
        self._session: Optional[aiohttp.ClientSession] = None
        self._clob_client = None  # py_clob_client instance

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(
                limit=50,
                ttl_dns_cache=300,
                enable_cleanup_closed=True
            )
            timeout = aiohttp.ClientTimeout(total=10, connect=3)
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                headers={"User-Agent": "PolymarketBot/1.0"}
            )
        return self._session

    def _init_clob_client(self):
        """Initialize py_clob_client for signed order creation."""
        if self._clob_client is not None:
            return
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds
            creds = ApiCreds(
                api_key=self.cfg.api_key,
                api_secret=self.cfg.api_secret,
                api_passphrase=self.cfg.api_passphrase,
            )
            self._clob_client = ClobClient(
                host=self.cfg.clob_api_url,
                chain_id=self.cfg.chain_id,
                key=self.cfg.private_key,
                creds=creds,
                signature_type=0,  # EOA
            )
            logger.info("CLOB client initialized (live trading)")
        except ImportError:
            logger.warning("py_clob_client not installed – paper trading only")
        except Exception as e:
            logger.error(f"CLOB client init failed: {e}")

    # -------------------------------------------------------------------------
    # Market Discovery
    # -------------------------------------------------------------------------

    async def get_active_markets(self) -> list[dict]:
        """
        Fetch active markets from Gamma API and filter for short-duration BTC markets.
        Returns list of raw market dicts.
        """
        session = await self._get_session()
        params = {
            "active": "true",
            "closed": "false",
            "limit": 100,
        }
        try:
            async with session.get(
                f"{self.cfg.gamma_api_url}/markets",
                params=params
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
                markets = data if isinstance(data, list) else data.get("markets", [])
                logger.debug(f"Fetched {len(markets)} active markets")
                return markets
        except Exception as e:
            logger.error(f"get_active_markets failed: {e}")
            return []

    async def get_market(self, market_id: str) -> Optional[dict]:
        """Get single market by condition_id."""
        session = await self._get_session()
        try:
            async with session.get(
                f"{self.cfg.clob_api_url}/markets/{market_id}"
            ) as resp:
                if resp.status == 404:
                    return None
                resp.raise_for_status()
                return await resp.json()
        except Exception as e:
            logger.error(f"get_market({market_id}) failed: {e}")
            return None

    # -------------------------------------------------------------------------
    # Orderbook
    # -------------------------------------------------------------------------

    async def get_orderbook(self, token_id: str) -> Optional[dict]:
        """
        Fetch L2 orderbook for a token.
        Returns raw CLOB orderbook dict with bids/asks.
        """
        session = await self._get_session()
        try:
            async with session.get(
                f"{self.cfg.clob_api_url}/book",
                params={"token_id": token_id}
            ) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
        except asyncio.TimeoutError:
            logger.warning(f"Orderbook timeout for {token_id}")
            return None
        except Exception as e:
            logger.error(f"get_orderbook({token_id}) failed: {e}")
            return None

    def parse_orderbook_snapshot(self, token_id: str, raw: dict):
        """Parse raw CLOB orderbook into OrderbookSnapshot."""
        from core.models import OrderbookSnapshot

        bids = sorted(raw.get("bids", []), key=lambda x: float(x["price"]), reverse=True)
        asks = sorted(raw.get("asks", []), key=lambda x: float(x["price"]))

        best_bid = float(bids[0]["price"]) if bids else 0.0
        best_ask = float(asks[0]["price"]) if asks else 1.0
        mid = (best_bid + best_ask) / 2

        # Compute top-5 liquidity depth
        bid_liq = sum(float(b["price"]) * float(b["size"]) for b in bids[:5])
        ask_liq = sum(float(a["price"]) * float(a["size"]) for a in asks[:5])

        return OrderbookSnapshot(
            token_id=token_id,
            best_bid=best_bid,
            best_ask=best_ask,
            bid_liquidity_usd=bid_liq,
            ask_liquidity_usd=ask_liq,
            mid_price=mid,
            spread=best_ask - best_bid,
        )

    async def get_last_trade_price(self, token_id: str) -> Optional[float]:
        """Get last matched price for a token."""
        session = await self._get_session()
        try:
            async with session.get(
                f"{self.cfg.clob_api_url}/last-trade-price",
                params={"token_id": token_id}
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return float(data.get("price", 0))
        except Exception as e:
            logger.error(f"get_last_trade_price failed: {e}")
            return None

    # -------------------------------------------------------------------------
    # Order Management
    # -------------------------------------------------------------------------

    async def place_limit_order(
        self,
        token_id: str,
        side: str,       # "BUY" or "SELL"
        price: float,
        size: float,
        order_id: Optional[str] = None,
    ) -> Optional[dict]:
        """
        Place a signed limit order via CLOB.

        In paper trading mode, returns a mock response immediately.
        In live mode, uses py_clob_client to sign and submit.

        Returns order response dict or None on failure.
        """
        if order_id is None:
            order_id = str(uuid.uuid4())

        if self.trading.paper_trading:
            logger.info(f"[PAPER] {side} {size:.4f} shares @ {price:.4f} | token={token_id[:8]}...")
            return {
                "order_id": f"PAPER-{order_id}",
                "status": "MATCHED",
                "price": price,
                "size": size,
                "filled": size,
                "paper": True,
            }

        # Live order
        self._init_clob_client()
        if self._clob_client is None:
            logger.error("Cannot place live order: CLOB client not initialized")
            return None

        for attempt in range(self.trading.order_retry_attempts):
            try:
                from py_clob_client.clob_types import OrderArgs, OrderType

                order_args = OrderArgs(
                    token_id=token_id,
                    price=price,
                    size=size,
                    side=side,
                )
                # Create signed order (pre-signed EIP-712)
                signed_order = self._clob_client.create_order(order_args)

                # Submit
                response = self._clob_client.post_order(signed_order, OrderType.GTC)
                logger.info(
                    f"[LIVE] Order placed: {side} {size:.4f} @ {price:.4f} | "
                    f"id={response.get('orderID')} status={response.get('status')}"
                )
                return response

            except Exception as e:
                logger.warning(f"Order attempt {attempt + 1} failed: {e}")
                if attempt < self.trading.order_retry_attempts - 1:
                    await asyncio.sleep(self.trading.order_retry_delay_ms / 1000)

        logger.error(f"All {self.trading.order_retry_attempts} order attempts failed")
        return None

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        if self.trading.paper_trading:
            logger.info(f"[PAPER] Cancel order {order_id}")
            return True

        self._init_clob_client()
        try:
            resp = self._clob_client.cancel(order_id)
            return resp.get("canceled", False)
        except Exception as e:
            logger.error(f"cancel_order({order_id}) failed: {e}")
            return False

    async def get_order_status(self, order_id: str) -> Optional[dict]:
        """Poll order status for fill confirmation."""
        if order_id.startswith("PAPER-"):
            return {"status": "MATCHED", "filled": True}

        self._init_clob_client()
        try:
            return self._clob_client.get_order(order_id)
        except Exception as e:
            logger.error(f"get_order_status({order_id}) failed: {e}")
            return None

    async def get_positions(self) -> list[dict]:
        """Fetch open positions from the CLOB API."""
        if self.trading.paper_trading:
            return []

        session = await self._get_session()
        # Note: positions endpoint requires auth headers
        # py_clob_client handles auth internally
        self._init_clob_client()
        try:
            return self._clob_client.get_positions() or []
        except Exception as e:
            logger.error(f"get_positions failed: {e}")
            return []

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
