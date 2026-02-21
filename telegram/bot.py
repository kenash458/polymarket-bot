"""
telegram/bot.py - Telegram bot interface

Commands:
  /start          - Start monitoring and trading
  /stop           - Stop all trading, exit positions
  /status         - Show current positions and P&L
  /setbuy 0.02    - Set buy threshold
  /setsell 0.06   - Set sell target
  /setexit 25     - Set forced exit seconds
  /setsize 10     - Set max position size in USD
  /positions      - List all open positions
  /pausemarket <id> - Pause a specific market
  /stats          - Lifetime stats
  /mode           - Toggle paper/live (if keys configured)

Security: Only responds to whitelisted chat IDs from config.
"""

import asyncio
import html
from datetime import datetime, timezone
from typing import Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from config import AppConfig
from core.trading_engine import TradingEngine
from core.models import Position, Market, PositionStatus
from utils.logger import get_logger

logger = get_logger("telegram_bot")


def _authorized(config: AppConfig, update: Update) -> bool:
    """Check if the sending chat is whitelisted."""
    chat_id = update.effective_chat.id
    if not config.telegram.allowed_chat_ids:
        # No whitelist configured — allow all (not recommended for production)
        return True
    return chat_id in config.telegram.allowed_chat_ids


def _fmt_pos(pos: Position, market_question: str = "") -> str:
    """Format a position for display."""
    status_emoji = {
        PositionStatus.OPEN: "🟢",
        PositionStatus.PAPER: "🟡",
        PositionStatus.PENDING_EXIT: "🔄",
        PositionStatus.CLOSED: "⚫",
    }.get(pos.status, "❓")

    lines = [
        f"{status_emoji} <b>Position {pos.position_id[:8]}</b>",
        f"  Market: {html.escape(market_question[:50]) if market_question else pos.market_id[:12]}...",
        f"  Outcome: {pos.outcome}",
        f"  Entry: ${pos.entry_price:.4f} × {pos.shares:.2f} = ${pos.cost_basis:.2f}",
    ]
    if pos.exit_price is not None:
        pnl = pos.realized_pnl or 0
        pct = pos.realized_pnl_pct or 0
        emoji = "✅" if pnl >= 0 else "❌"
        lines.append(f"  Exit: ${pos.exit_price:.4f} | PnL: {emoji} ${pnl:+.4f} ({pct:+.1f}%)")
        lines.append(f"  Reason: {pos.exit_reason.value if pos.exit_reason else 'N/A'}")
    return "\n".join(lines)


class TelegramBot:
    def __init__(self, config: AppConfig, engine: TradingEngine):
        self.config = config
        self.engine = engine
        self.app: Optional[Application] = None
        self._chat_ids_for_alerts: list[int] = config.telegram.allowed_chat_ids or []

        # Wire engine callbacks
        engine.on_position_opened = self._notify_position_opened
        engine.on_position_closed = self._notify_position_closed
        engine.on_alert = self._send_alert

    def build(self) -> Application:
        self.app = (
            Application.builder()
            .token(self.config.telegram.bot_token)
            .build()
        )

        handlers = [
            CommandHandler("start", self.cmd_start),
            CommandHandler("stop", self.cmd_stop),
            CommandHandler("status", self.cmd_status),
            CommandHandler("setbuy", self.cmd_setbuy),
            CommandHandler("setsell", self.cmd_setsell),
            CommandHandler("setexit", self.cmd_setexit),
            CommandHandler("setsize", self.cmd_setsize),
            CommandHandler("positions", self.cmd_positions),
            CommandHandler("pausemarket", self.cmd_pausemarket),
            CommandHandler("stats", self.cmd_stats),
            CommandHandler("help", self.cmd_help),
        ]
        for h in handlers:
            self.app.add_handler(h)

        return self.app

    # -------------------------------------------------------------------------
    # Commands
    # -------------------------------------------------------------------------

    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not _authorized(self.config, update):
            return
        await self.engine.start()
        mode = "📄 PAPER" if self.config.trading.paper_trading else "💰 LIVE"
        await update.message.reply_text(
            f"✅ <b>Bot Started</b>\n\n"
            f"Mode: {mode}\n"
            f"Buy threshold: {self.config.trading.buy_probability_threshold:.2%}\n"
            f"Sell target: {self.config.trading.sell_probability_target:.2%}\n"
            f"Forced exit: {self.config.trading.forced_exit_seconds_before_close}s before close\n"
            f"Max position: ${self.config.trading.max_position_size_usd:.2f}",
            parse_mode=ParseMode.HTML
        )

    async def cmd_stop(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not _authorized(self.config, update):
            return
        await update.message.reply_text("⏹️ Stopping bot and exiting all positions...")
        await self.engine.stop()
        await update.message.reply_text("✅ Bot stopped. All positions exited.")

    async def cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not _authorized(self.config, update):
            return
        summary = self.engine.get_summary()
        mode = "📄 PAPER" if summary["paper_trading"] else "💰 LIVE"
        text = (
            f"<b>Bot Status</b>\n\n"
            f"Mode: {mode}\n"
            f"Open positions: {summary['open_positions']}\n"
            f"Total trades: {summary['total_trades']}\n"
            f"Win rate: {summary['win_rate']:.1f}%\n"
            f"Total PnL: ${summary['total_pnl']:+.4f}\n"
            f"Forced exits: {summary['forced_exits']}\n"
            f"Safety exits: {summary['safety_exits']}"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def cmd_setbuy(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not _authorized(self.config, update):
            return
        try:
            val = float(ctx.args[0])
            if not 0 < val < 0.5:
                raise ValueError
            self.config.trading.buy_probability_threshold = val
            await update.message.reply_text(f"✅ Buy threshold set to {val:.2%}")
        except (IndexError, ValueError):
            await update.message.reply_text("❌ Usage: /setbuy 0.03 (value between 0 and 0.5)")

    async def cmd_setsell(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not _authorized(self.config, update):
            return
        try:
            val = float(ctx.args[0])
            if not 0 < val < 1.0:
                raise ValueError
            self.config.trading.sell_probability_target = val
            await update.message.reply_text(f"✅ Sell target set to {val:.2%}")
        except (IndexError, ValueError):
            await update.message.reply_text("❌ Usage: /setsell 0.06")

    async def cmd_setexit(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not _authorized(self.config, update):
            return
        try:
            raw = ctx.args[0].rstrip("s")  # Allow "25s" or "25"
            val = int(raw)
            if not 5 <= val <= 120:
                raise ValueError
            self.config.trading.forced_exit_seconds_before_close = val
            await update.message.reply_text(f"✅ Forced exit set to {val}s before close")
        except (IndexError, ValueError):
            await update.message.reply_text("❌ Usage: /setexit 25")

    async def cmd_setsize(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not _authorized(self.config, update):
            return
        try:
            val = float(ctx.args[0].lstrip("$"))
            if not 1 <= val <= 1000:
                raise ValueError
            self.config.trading.max_position_size_usd = val
            await update.message.reply_text(f"✅ Max position size set to ${val:.2f}")
        except (IndexError, ValueError):
            await update.message.reply_text("❌ Usage: /setsize 10")

    async def cmd_positions(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not _authorized(self.config, update):
            return
        if not self.engine.open_positions:
            await update.message.reply_text("📭 No open positions")
            return

        parts = []
        for pos in self.engine.open_positions.values():
            market = self.engine.active_markets.get(pos.market_id)
            q = market.question if market else ""
            parts.append(_fmt_pos(pos, q))

        await update.message.reply_text(
            "\n\n".join(parts),
            parse_mode=ParseMode.HTML
        )

    async def cmd_pausemarket(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not _authorized(self.config, update):
            return
        try:
            market_id = ctx.args[0]
            from core.market_scanner import MarketScanner
            # Pause via engine's active_markets
            market = self.engine.active_markets.get(market_id)
            if market:
                market.paused = True
                await update.message.reply_text(f"⏸️ Market paused: {market_id[:12]}...")
            else:
                await update.message.reply_text(f"❌ Market not found: {market_id}")
        except IndexError:
            await update.message.reply_text("❌ Usage: /pausemarket <market_id>")

    async def cmd_stats(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not _authorized(self.config, update):
            return
        s = self.engine.stats
        uptime = datetime.now(tz=timezone.utc) - s.start_time
        text = (
            f"<b>📊 Lifetime Stats</b>\n\n"
            f"Uptime: {str(uptime).split('.')[0]}\n"
            f"Total trades: {s.total_trades}\n"
            f"Wins / Losses: {s.winning_trades} / {s.losing_trades}\n"
            f"Win rate: {s.win_rate:.1f}%\n"
            f"Total PnL: ${s.total_pnl_usd:+.4f}\n"
            f"Total volume: ${s.total_volume_usd:.2f}\n"
            f"Forced exits: {s.forced_exits}\n"
            f"Safety exits: {s.safety_exits}"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not _authorized(self.config, update):
            return
        text = (
            "<b>📖 Commands</b>\n\n"
            "/start — Start bot\n"
            "/stop — Stop bot + exit all\n"
            "/status — Current status\n"
            "/positions — Open positions\n"
            "/stats — Lifetime stats\n"
            "/setbuy 0.03 — Buy threshold\n"
            "/setsell 0.06 — Sell target\n"
            "/setexit 25 — Forced exit seconds\n"
            "/setsize 10 — Max position USD\n"
            "/pausemarket <id> — Pause market\n"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    # -------------------------------------------------------------------------
    # Notification Callbacks
    # -------------------------------------------------------------------------

    async def _notify_position_opened(self, pos: Position, market: Market):
        msg = (
            f"🟢 <b>Position Opened</b>\n"
            f"{_fmt_pos(pos, market.question)}\n"
            f"Closes in: {market.seconds_to_close:.0f}s"
        )
        await self._broadcast(msg)

    async def _notify_position_closed(self, pos: Position):
        msg = (
            f"{'✅' if (pos.realized_pnl or 0) >= 0 else '❌'} <b>Position Closed</b>\n"
            f"{_fmt_pos(pos)}"
        )
        await self._broadcast(msg)

    async def _send_alert(self, message: str):
        await self._broadcast(f"⚠️ {message}")

    async def _broadcast(self, text: str):
        if not self.app or not self._chat_ids_for_alerts:
            logger.info(f"[TG BROADCAST] {text[:100]}")
            return
        for chat_id in self._chat_ids_for_alerts:
            try:
                await self.app.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=ParseMode.HTML
                )
            except Exception as e:
                logger.error(f"Failed to send Telegram message to {chat_id}: {e}")
