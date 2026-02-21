"""
core/market_scanner.py - Scans for eligible short-duration markets

Filters active Polymarket markets for:
- BTC price range markets
- Duration within configured window (e.g., 3–10 minutes)
- Not already tracked / paused
"""

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

from core.models import Market
from exchange.polymarket_client import PolymarketRESTClient
from utils.logger import get_logger

logger = get_logger("market_scanner")


def _parse_resolution_time(raw: dict) -> Optional[datetime]:
    """Parse resolution timestamp from various Gamma API formats."""
    for key in ("end_date_iso", "endDateIso", "gameStartTime", "resolution_time"):
        val = raw.get(key)
        if val:
            try:
                # Handle ISO string with/without timezone
                if isinstance(val, str):
                    val = val.replace("Z", "+00:00")
                    return datetime.fromisoformat(val)
                elif isinstance(val, (int, float)):
                    return datetime.fromtimestamp(val, tz=timezone.utc)
            except Exception:
                continue
    return None


def _extract_token_ids(raw: dict) -> tuple[Optional[str], Optional[str]]:
    """Extract YES/NO token IDs from market data."""
    tokens = raw.get("tokens") or raw.get("clob_token_ids") or []

    # tokens is usually list of {outcome, token_id}
    yes_id = no_id = None
    if isinstance(tokens, list):
        for t in tokens:
            if isinstance(t, dict):
                outcome = t.get("outcome", "").upper()
                tid = t.get("token_id") or t.get("tokenId")
                if "YES" in outcome or outcome == "1":
                    yes_id = tid
                elif "NO" in outcome or outcome == "0":
                    no_id = tid
    return yes_id, no_id


class MarketScanner:
    """
    Periodically scans Polymarket for active short-duration markets.
    Emits Market objects to the trading engine.
    """

    def __init__(self, config, rest_client: PolymarketRESTClient):
        self.config = config.trading
        self.rest = rest_client
        self._tracked_ids: set[str] = set()
        self._paused_ids: set[str] = set()
        self._scan_interval = 15  # seconds between full scans

    def pause_market(self, market_id: str):
        self._paused_ids.add(market_id)
        logger.info(f"Market paused: {market_id}")

    def resume_market(self, market_id: str):
        self._paused_ids.discard(market_id)

    async def scan_once(self) -> list[Market]:
        """
        Run one scan pass. Returns list of newly discovered eligible markets.
        """
        raw_markets = await self.rest.get_active_markets()
        eligible = []

        now = datetime.now(tz=timezone.utc)

        for raw in raw_markets:
            try:
                market_id = raw.get("condition_id") or raw.get("id", "")
                if not market_id:
                    continue

                # Skip already tracked or paused
                if market_id in self._tracked_ids or market_id in self._paused_ids:
                    continue

                # Keyword filter
                question = raw.get("question", "") or raw.get("title", "")
                if self.config.market_keyword_filter.lower() not in question.lower():
                    continue

                # Parse resolution time
                res_time = _parse_resolution_time(raw)
                if res_time is None:
                    continue

                # Make timezone-aware if naive
                if res_time.tzinfo is None:
                    res_time = res_time.replace(tzinfo=timezone.utc)

                # Duration filter
                duration_min = (res_time - now).total_seconds() / 60
                if not (self.config.min_market_duration_minutes
                        <= duration_min
                        <= self.config.max_market_duration_minutes):
                    continue

                # Extract token IDs
                yes_id, no_id = _extract_token_ids(raw)
                if not yes_id or not no_id:
                    logger.debug(f"Missing token IDs for market {market_id}")
                    continue

                market = Market(
                    market_id=market_id,
                    question=question,
                    token_id_yes=yes_id,
                    token_id_no=no_id,
                    resolution_time=res_time,
                    is_active=True,
                )
                self._tracked_ids.add(market_id)
                eligible.append(market)
                logger.info(
                    f"New market: [{duration_min:.1f}m] {question[:60]} | "
                    f"closes {res_time.strftime('%H:%M:%S UTC')}"
                )

            except Exception as e:
                logger.warning(f"Error parsing market {raw.get('id', '?')}: {e}")

        return eligible

    async def run(self, on_market_discovered):
        """
        Continuous scan loop. Calls on_market_discovered(market) for each new market.
        """
        logger.info("Market scanner started")
        while True:
            try:
                markets = await self.scan_once()
                for m in markets:
                    await on_market_discovered(m)
            except Exception as e:
                logger.error(f"Scanner loop error: {e}")
            await asyncio.sleep(self._scan_interval)

    def mark_closed(self, market_id: str):
        """Remove from tracked set when market closes."""
        self._tracked_ids.discard(market_id)
