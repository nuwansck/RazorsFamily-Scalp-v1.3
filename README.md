# RF Scalp v1.6 — XAU/USD 15-Minute Scalping Bot

> **Deployed on Railway · OANDA API · Telegram Alerts**

RF Scalp v1.6 is an automated gold scalping bot for XAU/USD built on the RF infrastructure.
It uses a four-layer signal engine with pre-score hard blocks to maximise win-rate accuracy:
**H1 trend filter** as a macro direction guard, **EMA 9/21 crossover** on M15 as the primary
trigger, **Opening Range Breakout (ORB)** as session momentum confirmation, and **CPR pivot
bias** as a directional filter.

v1.6 specifically addresses the low London win rates (38% → 25% → 0%) observed in v1.4–v1.5
by adding two hard blocks (H1 trend, ORB direction lock), upgrading the signal candle from
M5 to M15, and tightening session/daily trade caps.

---

## Table of Contents

1. [Strategy Overview](#strategy-overview)
2. [Signal Scoring](#signal-scoring)
3. [Hard Blocks](#hard-blocks)
4. [Trading Sessions](#trading-sessions)
5. [Risk Management](#risk-management)
6. [Settings Reference](#settings-reference)
7. [Railway Deployment](#railway-deployment)
8. [Environment Variables](#environment-variables)
9. [File Structure](#file-structure)
10. [Telegram Alerts](#telegram-alerts)
11. [Differences from RF v2.4](#differences-from-rf-v24)

---

## Strategy Overview

RF Scalp v1.6 operates on **M15 (15-minute) candles** and runs a 15-minute cycle.
Every cycle the signal engine first applies two hard blocks, then scores the remaining
components. A trade is only placed when the combined score reaches the configured
threshold (default: 4/6) and no blockers are active.

### Hard Blocks (pre-score, run before any scoring)

**Block 1 — H1 Trend Filter (new v1.6)**
- Fetches H1 EMA9/21 before evaluating any M15 signal
- If H1 EMA9 > EMA21 (bullish): only BUY signals are allowed
- If H1 EMA9 < EMA21 (bearish): only SELL signals are allowed
- If H1 EMAs are flat (neutral): both directions allowed
- This eliminates counter-trend trades — the primary cause of consecutive SL hits
- Disable via `h1_trend_filter_enabled: false` in settings.json

**Block 2 — ORB Direction Lock (new v1.6)**
- If ORB has formed and price has confirmed a side, blocks the opposite direction
- Price > ORB high → SELL signals blocked (session is bullish)
- Price < ORB low → BUY signals blocked (session is bearish)
- Price inside ORB range → both directions allowed
- Disable via `orb_direction_lock: false` in settings.json

### The Three Scoring Layers

**Layer 1 — EMA 9/21 Crossover (Primary Signal)**
- Uses Exponential Moving Averages on completed M15 candles (excludes the live candle)
- A **fresh cross** (EMA9 just crossed EMA21 in the last two candles) scores +3
- An **aligned but no fresh cross** (EMA9 above/below EMA21) scores +1
- Direction is set entirely by this layer — BUY when EMA9 above EMA21, SELL when below

**Layer 2 — Opening Range Breakout (Momentum Filter)**
- The ORB is defined as the first completed M15 candle at or after session open
- London ORB: first M15 candle from 16:00 SGT (08:00 GMT)
- US ORB: first M15 candle from 21:00 SGT (13:00 GMT)
- Price breaking above ORB high (BUY) or below ORB low (SELL) scores +2 (fresh), +1 (aging)
- ORB is cached per session per SGT day in `orb_cache.json`

**Layer 3 — CPR Pivot Bias (Directional Filter)**
- Daily CPR (Central Pivot Range) levels are fetched from OANDA each cycle
- Price above CPR pivot on a BUY signal scores +1
- Price below CPR pivot on a SELL signal scores +1
- CPR is a bias filter only — it cannot block a trade, only reduce the score

---

## Signal Scoring

### Scoring Table

| Component | Condition | Score |
|---|---|---|
| EMA Cross | Fresh EMA9 cross above/below EMA21 | +3 |
| EMA Cross | EMA9/21 aligned but no fresh cross | +1 |
| ORB | Price breaks session ORB high (BUY) / low (SELL) | +2 |
| CPR Bias | Price on correct side of daily pivot | +1 |
| Exhaustion | Price stretched >2× ATR from EMA midpoint | −1 |

**Maximum score: 6**

### Score-to-Action Table

| Score | Action | Position Size |
|---|---|---|
| 5 – 6 | Trade — Full size | $100 |
| 3 – 4 | Trade — Partial size | $66 |
| < 4 | No trade — Watch | $0 |

### Mandatory Blockers (trade is skipped regardless of score)

- Score below session threshold (default: 4)
- R:R ratio < 1:2
- Spread above session limit (London: 130 pips, US: 130 pips)
- News event within ±30 minutes
- Daily loss cap reached (3 losses/day)
- Session loss sub-cap reached (2 losses/session)
- Active cooldown period (30 min after loss streak)
- Friday cutoff (23:00 SGT)

---

## Trading Sessions

| Session | SGT Time | GMT Equivalent | Max Trades |
|---|---|---|---|
| London Window | 16:00 – 20:59 | 08:00 – 13:00 | 4 |
| US Window | 21:00 – 00:59 | 13:00 – 17:00 EDT | 4 |
| Dead Zone | 01:00 – 15:59 | — | 0 (no new entries) |

The Asian session is **disabled** — XAU/USD does not produce sufficient momentum for
scalp-quality ORB breakouts or clean EMA crosses during Asian hours.

---

## Risk Management

### Stop Loss
- Mode: `pct_based` (default)
- SL = 0.15% of entry price
- At gold price of ~$2,400: SL ≈ $3.60 per unit
- Clamped: min $2.00, max $8.00 (ATR mode)

### Take Profit
- Mode: `rr_multiple` (default)
- TP = SL × RR ratio (default 2.5)
- Effective TP ≈ 0.35% of entry
- At gold price of ~$2,400: TP ≈ $8.40 per unit

### Comparision: RF v2.4 vs RF Scalp v1.6

| Parameter | RF v2.4 | RF Scalp v1.6 |
|---|---|---|
| SL % | 0.25% | 0.25% |
| TP % | 0.75% | 0.625% (RR 2.5×) |
| RR ratio | 3.0 | 2.5 |
| Signal timeframe | M15 | M15 |
| Strategy | CPR breakout | EMA + ORB + CPR bias + H1 filter |

### Daily Limits (v1.6)

| Limit | Value |
|---|---|
| Max trades per day | 10 |
| Max losing trades per day | 5 |
| Max losing trades per session | 2 |
| Max trades — London | 4 |
| Max trades — US | 10 |
| Loss streak cooldown | 30 minutes |
| Consecutive SL direction block | 90 minutes |
| Max concurrent trades | 1 |

---

## Settings Reference

All settings live in `settings.json`. Key scalp-specific values:

```json
{
  "bot_name":                  "RF Scalp v1.6",
  "sl_pct":                    0.0025,
  "tp_pct":                    0.0035,
  "rr_ratio":                  2.5,
  "sl_min_usd":                2.0,
  "sl_max_usd":                15.0,
  "atr_sl_multiplier":         0.3,
  "signal_threshold":          4,
  "cycle_minutes":             15,
  "exhaustion_atr_mult":       3.0,
  "session_thresholds":        { "London": 5, "US": 4 },
  "h1_trend_filter_enabled":   true,
  "orb_direction_lock":        true
}
```

Full settings reference:

| Key | Default | Description |
|---|---|---|
| `bot_name` | RF Scalp v1.6 | Display name in Telegram |
| `demo_mode` | true | true = OANDA practice, false = live |
| `signal_threshold` | 4 | Minimum score to place a trade |
| `sl_pct` | 0.0025 | Stop loss as % of entry (0.25%) |
| `tp_pct` | 0.0035 | Take profit % (scalp_pct mode only) |
| `rr_ratio` | 2.5 | TP multiplier when tp_mode = rr_multiple |
| `sl_min_usd` | 2.0 | Minimum SL in USD |
| `sl_max_usd` | 15.0 | Maximum SL in USD |
| `atr_sl_multiplier` | 0.3 | ATR multiplier when sl_mode = atr_based |
| `exhaustion_atr_mult` | 3.0 | Stretch penalty threshold (×ATR from EMA midpoint) |
| `cycle_minutes` | 15 | Bot cycle interval in minutes |
| `max_trades_day` | 10 | Max trades per SGT trading day |
| `max_losing_trades_day` | 5 | Max losses before day halt |
| `max_losing_trades_session` | 2 | Max losses per session block |
| `max_trades_london` | 4 | Trade cap for London window |
| `max_trades_us` | 10 | Trade cap for US window |
| `loss_streak_cooldown_min` | 30 | Cooldown minutes after consecutive losses |
| `consecutive_sl_block_count` | 2 | SLs in same direction to trigger direction block |
| `consecutive_sl_block_minutes` | 90 | Minutes to block a direction after consecutive SLs |
| `max_spread_pips` | 150 | Global spread ceiling in pips |
| `spread_limits.London` | 130 | London-specific spread ceiling |
| `spread_limits.US` | 130 | US-specific spread ceiling |
| `news_filter_enabled` | true | Enable economic calendar filter |
| `news_block_before_min` | 30 | Minutes before high-impact news to pause |
| `news_block_after_min` | 30 | Minutes after high-impact news to pause |
| `session_only` | true | Only trade during defined session windows |
| `session_thresholds.London` | 5 | Min score required during London session |
| `session_thresholds.US` | 4 | Min score required during US session |
| `breakeven_enabled` | false | SL move to breakeven |
| `friday_cutoff_hour_sgt` | 23 | Stop new trades after this hour on Friday (SGT) |
| `h1_trend_filter_enabled` | true | **[v1.6]** H1 EMA hard block — no counter-trend trades |
| `h1_ema_fast_period` | 9 | **[v1.6]** H1 fast EMA period for trend filter |
| `h1_ema_slow_period` | 21 | **[v1.6]** H1 slow EMA period for trend filter |
| `h1_candle_count` | 30 | **[v1.6]** H1 candles fetched for trend filter |
| `orb_direction_lock` | true | **[v1.6]** Block trades against confirmed ORB side |
| `orb_fresh_minutes` | 60 | Minutes after session open for fresh ORB break (+2 pts) |
| `orb_aging_minutes` | 120 | Minutes after session open for aging ORB break (+1 pt) |
| `orb_formation_minutes` | 15 | Minutes after open before ORB is considered formed |

---

## Railway Deployment

### First Deploy

1. Push the `RF Scalp v1.6` folder to a new GitHub repository
2. In Railway: **New Project → Deploy from GitHub Repo**
3. Select the repository
4. Add all environment variables (see below)
5. Railway auto-detects `Procfile` and starts the bot

### Procfile

```
web: python scheduler.py
```

### railway.json

```json
{
  "$schema": "https://railway.app/railway.schema.json",
  "build": { "builder": "NIXPACKS" },
  "deploy": { "startCommand": "python scheduler.py", "restartPolicyType": "ON_FAILURE" }
}
```

### Health Check

The bot exposes a health endpoint at `GET /health` (port 8080 by default).
Railway can poll this for uptime monitoring. Returns:

```json
{
  "status": "ok",
  "scheduler_running": true,
  "last_cycle_started": "2025-01-01 16:05:00",
  "last_cycle_status": "COMPLETED",
  "oanda_failures": 0,
  "uptime_s": 3600
}
```

---

## Environment Variables

Set these in Railway → Variables:

| Variable | Required | Description |
|---|---|---|
| `OANDA_API_KEY` | Yes | OANDA API key (practice or live) |
| `OANDA_ACCOUNT_ID` | Yes | OANDA account ID |
| `TELEGRAM_BOT_TOKEN` | Yes | Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Yes | Telegram chat/channel ID |
| `TRADINGECONOMICS_API_KEY` | Recommended | Economic calendar API key |
| `PORT` | No | Health server port (default 8080) |

---

## File Structure

```
RF Scalp v1.6/
├── signals.py              ← H1 filter + ORB lock + M15 EMA signal engine (v1.6)
├── bot.py                  ← Main orchestrator
├── scheduler.py            ← APScheduler — runs bot cycle every 15 min
├── settings.json           ← All tunable parameters
├── settings.json.example   ← Template copy of settings.json
├── version.py              ← Bot name: RF Scalp | version: 1.6.0
├── oanda_trader.py         ← OANDA REST API wrapper
├── telegram_templates.py   ← All Telegram message formats
├── telegram_alert.py       ← Telegram send helper
├── news_filter.py          ← Economic calendar news filter
├── calendar_fetcher.py     ← TradingEconomics calendar fetch
├── database.py             ← SQLite trade + signal database
├── reporting.py            ← Daily / weekly / monthly reports
├── reconcile_state.py      ← OANDA vs local state reconciliation
├── config_loader.py        ← Settings + secrets loader
├── state_utils.py          ← JSON state file helpers
├── analyze_trades.py       ← CLI trade analysis tool
├── logging_utils.py        ← Structured logging setup
├── startup_checks.py       ← Env var and config validation
├── Procfile                ← Railway start command
├── railway.json            ← Railway project config
├── requirements.txt        ← Python dependencies
├── README.md               ← This file
├── CHANGELOG.md            ← Full version history
├── SETTINGS.md             ← Complete settings documentation
└── CONFLUENCE_READY.md     ← Full Confluence wiki page
```

### Key Data Files (auto-created at runtime)

```
data/
├── cpr_cache.json      ← Daily CPR levels (refreshed each SGT day)
├── orb_cache.json      ← ORB high/low per session per day (NEW)
├── orb_cache.json      ← Session Opening Range Breakout levels
├── trade_history.json  ← Rolling 90-day trade log
├── signal_cache.json   ← Signal dedup cache
├── ops_state.json      ← Operational state (session, cooldown, etc.)
└── runtime_state.json  ← Cycle health state
```

---

## Telegram Alerts

RF Scalp v1.6 sends the following Telegram notifications:

| Alert | Trigger |
|---|---|
| Startup | Bot starts / Railway redeploys |
| Session Open | London (16:00 SGT) and US (21:00 SGT) open |
| Scalp Signal Update | Every 15-min cycle during active session |
| H1 Block | Signal blocked due to H1 trend disagreement (new v1.6) |
| ORB Lock | Signal blocked due to ORB direction conflict (new v1.6) |
| Trade Opened | New order placed |
| Trade Closed | TP hit, SL hit, or manually closed |
| News Block | High-impact news event pausing trading |
| Spread Skip | Spread too wide to trade |
| Daily Cap | Max trades or losses for the day reached |
| Session Cap | Max trades or losses for the session reached |
| Cooldown Started | Loss streak cooldown activated |
| Direction Block | Consecutive SL direction block activated |
| Friday Cutoff | No new trades after 23:00 SGT on Friday |
| Daily Report | Mon–Fri at 15:30 SGT |
| Weekly Report | Every Monday at 08:15 SGT |
| Monthly Report | First Monday of each month at 08:00 SGT |

---

## Differences from RF v2.4

| Area | RF v2.4 | RF Scalp v1.6 |
|---|---|---|
| **Strategy** | CPR breakout | EMA 9/21 + ORB + CPR bias + H1 filter |
| **Signal timeframe** | M15 candles | M15 candles |
| **Trend filter** | None | H1 EMA9/21 hard block (new v1.6) |
| **Session direction** | None | ORB direction lock (new v1.6) |
| **Primary signal** | Price breaks CPR/PDH/R1 | EMA9 crosses EMA21 |
| **Secondary signal** | SMA20/SMA50 alignment | ORB session range breakout |
| **Tertiary filter** | CPR width | CPR pivot bias |
| **SL %** | 0.25% | 0.25% |
| **RR ratio** | 3.0 | 2.5 |
| **Cycle** | 15 min | 15 min |
| **New cache file** | — | `orb_cache.json` |
| **Bot name** | CPR Gold Bot v2.4 | RF Scalp v1.6 |

---

*RF Scalp v1.6 — Built on RF v2.4 infrastructure · EMA + ORB + CPR + H1 trend filter*
