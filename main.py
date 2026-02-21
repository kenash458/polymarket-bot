"""
main.py - Application entry point

Wires together:
  - Config
  - REST client
  - WebSocket feed
  - Market scanner
  - Trading engine
  - Telegram bot

Run: python main.py
"""

import asyncio
import signal
import sys
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent))

from config import config
from core.market_scanner import MarketScanner
from core.trading_engine import TradingEngine
from exchange.polymarket_client import PolymarketRESTClient
from exchange.websocket_feed import PolymarketWebSocketFeed
from telegram_bot.bot import TelegramBot
from telegram import Update
from utils.logger import get_logger

logger = get_logger("main")


class PolymarketBotApp:
    def __init__(self):
        self.config = config
        self.rest_client = PolymarketRESTClient(config)
        self.ws_feed = PolymarketWebSocketFeed(config)
        self.engine = TradingEngine(config, self.rest_client)
        self.scanner = MarketScanner(config, self.rest_client)
        self.tg_bot = TelegramBot(config, self.engine)
        self._shutdown_event = asyncio.Event()

    async def on_market_discovered(self, market):
        """Called when scanner finds a new eligible market."""
        # Register with engine
        self.engine.register_market(market)

        # Subscribe WebSocket to both YES and NO tokens
        await self.ws_feed.subscribe([market.token_id_yes, market.token_id_no])

        logger.info(
            f"Subscribed WS: {market.question[:50]} | "
            f"closes in {market.seconds_to_close:.0f}s"
        )

    async def cleanup_expired_markets(self):
        """Remove expired markets from engine and unsubscribe WS."""
        while True:
            await asyncio.sleep(10)
            expired = [
                m for m in self.engine.active_markets.values()
                if m.seconds_to_close < -30  # 30s grace after close
            ]
            for m in expired:
                logger.info(f"Cleaning up expired market: {m.market_id[:12]}...")
                self.engine.remove_market(m.market_id)
                self.scanner.mark_closed(m.market_id)
                await self.ws_feed.unsubscribe([m.token_id_yes, m.token_id_no])

    async def run(self):
        logger.info("=" * 60)
        logger.info("Polymarket Trading Bot Starting")
        logger.info(f"Paper trading: {self.config.trading.paper_trading}")
        logger.info(f"Buy threshold: {self.config.trading.buy_probability_threshold:.2%}")
        logger.info(f"Sell target:   {self.config.trading.sell_probability_target:.2%}")
        logger.info(f"Forced exit:   {self.config.trading.forced_exit_seconds_before_close}s before close")
        logger.info("=" * 60)

        if not self.config.trading.paper_trading:
            if not self.config.network.private_key:
                logger.critical("LIVE mode requires PRIVATE_KEY in .env — aborting.")
                return

        # Wire WebSocket price callbacks to engine
        self.ws_feed.on_price_update = self.engine.on_price_update

        # Build Telegram bot application
        tg_app = self.tg_bot.build()

        # Start trading engine
        await self.engine.start()

        # Start WebSocket feed (no tokens yet — scanner will subscribe)
        await self.ws_feed.start([])

        # Tasks
        tasks = [
            asyncio.create_task(
                self.scanner.run(self.on_market_discovered),
                name="scanner"
            ),
            asyncio.create_task(
                self.cleanup_expired_markets(),
                name="cleanup"
            ),
        ]

        # Start Telegram bot in polling mode
        await tg_app.initialize()
        await tg_app.start()
        await tg_app.updater.start_polling(
            allowed_updates=["message"],
            drop_pending_updates=True,
        )

        logger.info("All systems online. Monitoring for markets...")

        # Wait for shutdown signal
        await self._shutdown_event.wait()

        # Graceful shutdown
        logger.info("Shutting down...")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        await self.engine.stop()
        await self.ws_feed.stop()
        await self.rest_client.close()

        await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()

        logger.info("Shutdown complete.")

    def request_shutdown(self):
        self._shutdown_event.set()


async def main():
    app = PolymarketBotApp()

    loop = asyncio.get_running_loop()

    def _signal_handler():
        logger.info("Signal received — shutting down...")
        app.request_shutdown()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            pass  # Windows

    await app.run()


if __name__ == "__main__":
    asyncio.run(main())
