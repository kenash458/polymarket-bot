"""
config.py - Centralized configuration for Polymarket Trading Bot
All parameters are loaded from environment variables with sensible defaults.
"""

import os
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

load_dotenv()


@dataclass
class TradingConfig:
    # Entry thresholds
    buy_probability_threshold: float = float(os.getenv("BUY_PROB_THRESHOLD", "0.03"))   # Buy if price <= 3%
    sell_probability_target: float = float(os.getenv("SELL_PROB_TARGET", "0.06"))        # Sell if price >= 6%

    # Position sizing
    max_position_size_usd: float = float(os.getenv("MAX_POSITION_USD", "10.0"))          # Max $ per position
    max_concurrent_positions: int = int(os.getenv("MAX_CONCURRENT_POSITIONS", "3"))

    # Exit timing
    forced_exit_seconds_before_close: int = int(os.getenv("FORCED_EXIT_SECONDS", "25")) # Sell 25s before expiry
    safety_exit_liquidity_threshold: float = float(os.getenv("SAFETY_LIQUIDITY", "50")) # Min $50 liquidity

    # Spread / orderbook safety
    max_spread_pct: float = float(os.getenv("MAX_SPREAD_PCT", "0.015"))                  # 1.5% max spread
    min_book_imbalance_ratio: float = float(os.getenv("MIN_BOOK_IMBALANCE", "0.2"))      # bid/ask ratio

    # Slippage
    slippage_tolerance: float = float(os.getenv("SLIPPAGE_TOLERANCE", "0.005"))          # 0.5%

    # Fee awareness (Polymarket takes ~2% on winnings; we never hold to resolution)
    # Platform fee on CLOB trades is currently 0 for makers, small for takers
    taker_fee: float = float(os.getenv("TAKER_FEE", "0.0"))

    # Retry logic
    order_retry_attempts: int = int(os.getenv("ORDER_RETRY_ATTEMPTS", "3"))
    order_retry_delay_ms: int = int(os.getenv("ORDER_RETRY_DELAY_MS", "200"))

    # Market filters
    min_market_duration_minutes: int = int(os.getenv("MIN_MARKET_DURATION_MIN", "3"))
    max_market_duration_minutes: int = int(os.getenv("MAX_MARKET_DURATION_MIN", "10"))
    market_keyword_filter: str = os.getenv("MARKET_KEYWORD", "BTC")

    # Paper trading mode - NEVER sends real orders
    paper_trading: bool = os.getenv("PAPER_TRADING", "true").lower() == "true"


@dataclass
class NetworkConfig:
    # Polymarket CLOB API
    clob_api_url: str = os.getenv("CLOB_API_URL", "https://clob.polymarket.com")
    gamma_api_url: str = os.getenv("GAMMA_API_URL", "https://gamma-api.polymarket.com")
    ws_url: str = os.getenv("WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/")

    # Auth
    private_key: Optional[str] = os.getenv("PRIVATE_KEY")          # EVM private key
    api_key: Optional[str] = os.getenv("POLYMARKET_API_KEY")
    api_secret: Optional[str] = os.getenv("POLYMARKET_API_SECRET")
    api_passphrase: Optional[str] = os.getenv("POLYMARKET_API_PASSPHRASE")

    # Chain
    chain_id: int = int(os.getenv("CHAIN_ID", "137"))               # Polygon mainnet
    rpc_url: str = os.getenv("RPC_URL", "https://polygon-rpc.com")


@dataclass
class TelegramConfig:
    bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    allowed_chat_ids: list = field(default_factory=lambda: [
        int(x) for x in os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",") if x.strip()
    ])


@dataclass
class AppConfig:
    trading: TradingConfig = field(default_factory=TradingConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)

    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    log_file: str = os.getenv("LOG_FILE", "logs/bot.log")
    ws_reconnect_delay_s: int = int(os.getenv("WS_RECONNECT_DELAY", "3"))
    ws_heartbeat_interval_s: int = int(os.getenv("WS_HEARTBEAT_INTERVAL", "30"))


# Singleton
config = AppConfig()
