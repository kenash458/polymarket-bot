# Polymarket Short-Duration Trading Bot

A production-ready Python bot for automated trading on Polymarket's 5-minute BTC markets. Captures microstructure profits from late-stage probability repricing by entering cheap positions and exiting before resolution.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        main.py (orchestrator)                    │
│                                                                  │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────────┐  │
│  │ MarketScanner│    │ WS Feed      │    │ Telegram Bot     │  │
│  │              │    │              │    │                  │  │
│  │ Polls Gamma  │    │ Persistent   │    │ Commands:        │  │
│  │ API every 15s│    │ WebSocket    │    │ /start /stop     │  │
│  │              │    │ connection   │    │ /setbuy /setsell │  │
│  │ Filters:     │    │              │    │ /positions etc.  │  │
│  │ - BTC markets│    │ Auto-        │    │                  │  │
│  │ - 3-10 min   │    │ reconnects   │    │ Auth: whitelist  │  │
│  │   remaining  │    │ Heartbeat    │    │ by chat ID       │  │
│  └──────┬───────┘    └──────┬───────┘    └────────┬─────────┘  │
│         │ new market         │ price ticks          │ commands   │
│         ▼                   ▼                      ▼            │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                    TradingEngine                          │   │
│  │                                                          │   │
│  │  Entry Logic:            Exit Logic (priority order):    │   │
│  │  - price <= threshold    1. Forced time (T-25s)          │   │
│  │  - spread OK             2. Safety: liquidity collapsed  │   │
│  │  - liquidity OK          3. Safety: spread too wide      │   │
│  │  - not already in mkt    4. Safety: price stalled        │   │
│  │                          5. Profit target hit            │   │
│  │  Position monitor loop: runs every 1s (safety net)       │   │
│  └──────────────────────────┬───────────────────────────────┘   │
│                             │ orders                             │
│                             ▼                                    │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                PolymarketRESTClient                        │   │
│  │                                                           │   │
│  │  Paper mode: immediate mock fills                         │   │
│  │  Live mode: py_clob_client signs EIP-712 orders          │   │
│  │  Retry: 3 attempts with 200ms backoff                     │   │
│  └───────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

**Data flow:**
1. Scanner finds BTC 5-min market with 3–10 min remaining
2. Bot subscribes to YES and NO token WebSocket feeds
3. On each price tick: engine checks if ask ≤ 3% (entry threshold)
4. If entry conditions pass: buy order placed
5. On subsequent ticks: engine evaluates exit conditions in priority order
6. Position closed via sell order before expiry (default: 25s buffer)
7. Telegram notified of every open/close event

---

## Setup Instructions

### 1. System Requirements

```bash
# Python 3.11+ required
python3 --version

# Ubuntu/Debian VPS recommended
sudo apt update && sudo apt install -y python3.11 python3.11-venv git
```

### 2. Clone and Install

```bash
git clone <your-repo> polymarket_bot
cd polymarket_bot

# Create virtual environment
python3.11 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Configure Environment

```bash
cp .env.example .env
nano .env
```

Fill in at minimum:
- `TELEGRAM_BOT_TOKEN` — from @BotFather
- `TELEGRAM_ALLOWED_CHAT_IDS` — your Telegram chat ID
- Leave `PAPER_TRADING=true` until fully tested

For live trading, additionally:
- `PRIVATE_KEY` — EVM wallet private key
- `POLYMARKET_API_KEY/SECRET/PASSPHRASE` — from Polymarket dashboard

### 4. Get Polymarket API Credentials (for live trading)

```
1. Go to polymarket.com and connect your MetaMask wallet
2. Go to Settings → API Keys
3. Generate L2 API credentials
4. Fund your wallet with USDC on Polygon
5. Approve USDC spending on Polymarket
```

### 5. Run in Paper Mode First

```bash
python main.py
```

Send `/start` to your Telegram bot. Watch logs:
```
logs/bot.log
```

Confirm paper trades appear before switching to live.

### 6. Run as a Service (systemd)

```bash
sudo nano /etc/systemd/system/polybot.service
```

```ini
[Unit]
Description=Polymarket Trading Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/polymarket_bot
ExecStart=/home/ubuntu/polymarket_bot/venv/bin/python main.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
EnvironmentFile=/home/ubuntu/polymarket_bot/.env

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable polybot
sudo systemctl start polybot
sudo journalctl -u polybot -f  # Live logs
```

---

## Latency Optimization Guide

### 1. VPS Colocation
Host your bot in AWS `us-east-1` or similar US-East region. Polymarket's infrastructure is US-based. A $10/mo DigitalOcean or Vultr VPS in New York will outperform a local machine in Europe by 50–150ms.

```bash
# Test latency to Polymarket API
ping clob.polymarket.com
traceroute clob.polymarket.com
```

### 2. WebSocket vs Polling
This bot uses WebSocket by default. **Never use polling** for price data in a time-sensitive strategy:
- Polling at 1s intervals = you're always 0–1000ms stale
- WebSocket pushes updates within ~10–50ms of orderbook change
- The WS feed here auto-reconnects with exponential backoff

### 3. Pre-signed Orders (py_clob_client)
`py_clob_client.create_order()` is called **before** sending. The EIP-712 signing is done locally on your machine, not on-chain. This means:
- Signing takes ~1–5ms
- HTTP round-trip to submit = ~50–200ms depending on VPS location
- Total order latency: ~55–210ms

To reduce further:
```python
# Pre-sign orders during low-activity periods, cache signed objects
# Only valid if you know the exact price and size in advance
signed = client.create_order(order_args)
# ... trigger condition fires ...
client.post_order(signed)  # Only the HTTP call remains
```

### 4. Async Order Placement
All orders in this bot use `async/await`. The event loop is never blocked. Key points:
- `aiohttp` for non-blocking HTTP
- `websockets` for non-blocking price feed
- Position monitor runs as a background task (1s tick)
- All callbacks are `async` to avoid blocking the price feed

### 5. TCP Connection Reuse
The `aiohttp.ClientSession` is reused across requests (connection pooling). Opening a new TCP connection per order would add 50–150ms.

### 6. DNS Caching
`TCPConnector(ttl_dns_cache=300)` caches DNS resolution for 5 minutes. DNS lookup on every request would add 10–50ms.

---

## Risk Warnings and Failure Cases

### ⚠️ Critical Risks

**1. Held into resolution**
The most dangerous failure mode. If the exit order fails and the monitor loop doesn't catch it, you settle at 0.
- Mitigation: Position monitor runs every 1s independently of WS feed
- Mitigation: Emergency exit at 50% of price if first exit fails
- Mitigation: Set `FORCED_EXIT_SECONDS=30` for safety margin

**2. Partial fills**
If your sell order is partially filled as the market closes, remaining shares settle at 0.
- Current behavior: We send the full size in one order
- For large positions, consider splitting into 2–3 smaller sell orders

**3. WebSocket disconnect near expiry**
If the WS drops 30 seconds before close, you miss price ticks.
- Mitigation: Position monitor loop runs on 1s timer regardless of WS state
- Mitigation: WS reconnects automatically with exponential backoff
- Mitigation: Set `FORCED_EXIT_SECONDS` high enough to survive reconnect

**4. API rate limits**
Polymarket CLOB API has rate limits. Excessive requests will result in 429 errors.
- Order retry logic handles this (3 attempts, 200ms delay)
- Scanner runs every 15s (not aggressive)

**5. Slippage on exit**
When you need to exit urgently, you sell at bid. In illiquid markets, bid may be far below mid.
- Mitigation: Safety exit triggers before liquidity collapses
- Mitigation: Only enter if ask liquidity > $50

**6. Polymarket early market resolution**
Some markets close early if the outcome is certain early. Your position could be settled before your forced exit triggers.
- Mitigation: `seconds_left < 0` check in monitor loop forces immediate exit
- This is a genuine risk with no perfect mitigation

**7. Stale orderbook**
If the WS feed isn't updating, your bot may act on stale prices.
- `OrderbookState.last_update` tracks when the last update arrived
- Consider adding a staleness check: if `time.monotonic() - book.last_update > 5`, treat as stale

**8. Smart contract / USDC approval expiry**
Live trading requires USDC approval on Polygon. If it expires or gets revoked, orders silently fail.
- Check your on-chain approval before each session

### 📋 Operational Checklist (before live trading)

- [ ] Run paper mode for at least 48 hours
- [ ] Verify all Telegram commands work
- [ ] Confirm positions close before expiry in paper mode
- [ ] Test `/stop` command — all positions should exit immediately
- [ ] Check logs for any ERROR or WARNING messages
- [ ] Verify USDC balance and approval on Polygon
- [ ] Set conservative `MAX_POSITION_USD` (start with $2–5)
- [ ] Set `FORCED_EXIT_SECONDS=30` (extra safety margin)
- [ ] Enable log rotation (already configured via RotatingFileHandler)
- [ ] Set up monitoring/alerting on the systemd service

---

## File Structure

```
polymarket_bot/
├── main.py                    # Entry point
├── config.py                  # All configuration
├── requirements.txt
├── .env.example
├── core/
│   ├── models.py              # Domain models
│   ├── market_scanner.py      # Finds eligible markets
│   └── trading_engine.py      # Entry/exit logic
├── exchange/
│   ├── polymarket_client.py   # REST API client
│   └── websocket_feed.py      # WebSocket price feed
├── telegram/
│   └── bot.py                 # Telegram interface
├── utils/
│   └── logger.py              # Structured logging
└── logs/
    └── bot.log                # Rotating log file
```

---

## Strategy Notes

This strategy exploits a specific microstructure inefficiency in Polymarket's short-duration markets:

1. Near expiry, market makers widen spreads and reduce liquidity
2. Any minor price move causes the losing side to reprice sharply (e.g., 1% → 5%)
3. You enter at 1–3% and exit at 5–8% — a 2–8x return on the position
4. The key edge: you never need to predict the outcome, only the price trajectory within the next few minutes

**Expected hit rate:** 20–40% of positions reach profit target before forced exit. The large asymmetry (e.g., buy at 2 cents, sell at 6 cents) means even a 25% hit rate is profitable if losing positions don't hold to 0.

**The bot's job is to ensure losing positions are exited for 0.5–1.5 cents (not 0), by selling before resolution.**
