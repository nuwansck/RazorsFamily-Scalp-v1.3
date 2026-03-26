# RF Scalp Bot — Settings Reference

All bot behaviour is controlled by `settings.json`. No hardcoded values exist
in the code — everything is read from this file and has a safe fallback default.

> **Note:** JSON does not support comments. This file documents every key.
> Edit `settings.json` and redeploy to change any setting. Changes take effect
> on the next Railway container restart.

---

## Bot Identity

| Key | Default | Description |
|---|---|---|
| `bot_name` | `"RF Scalp v1.6"` | Shown in Telegram alerts and logs. Change when deploying a new version. |
| `demo_mode` | `true` | `true` = OANDA demo account. Set to `false` for live trading. |
| `trade_gold` | `true` | Master on/off switch for trading. Set to `false` to pause without stopping the bot. |
| `enabled` | `true` | Secondary on/off switch. `false` = bot skips all trade cycles but stays running. Use `trade_gold` for normal pausing. |

---

## Signal Engine

These control how the bot scores each 15-minute candle and decides whether to trade.

| Key | Default | Description |
|---|---|---|
| `signal_threshold` | `4` | Minimum score (out of 6) required to place a trade. Raise to 5 for higher-quality signals only. |
| `session_thresholds` | `{"London": 5, "US": 4}` | Per-session score threshold. London is stricter (5) due to historically lower win rates. |
| `ema_fast_period` | `9` | Fast EMA period. Used for crossover detection on M15 candles. |
| `ema_slow_period` | `21` | Slow EMA period. EMA fast crossing above/below slow is the primary direction signal. |
| `m5_candle_count` | `40` | Number of M15 candles fetched from OANDA per cycle (key name retained for compatibility). |

### Scoring breakdown (max 6 points)
- **EMA fresh cross** (9 just crossed 21 this M15 candle): **+3 pts**
- **EMA trend only** (9 already above/below 21, no fresh cross): **+1 pt**
- **ORB break** (price outside opening range, time-decayed): **+2 / +1 / +0 pts**
- **CPR bias** (price on correct side of daily pivot): **+1 pt**

### Pre-score hard blocks (new v1.6 — do not affect score, kill the signal entirely)

**H1 trend filter** — fetched before any scoring:

| Key | Default | Description |
|---|---|---|
| `h1_trend_filter_enabled` | `true` | Enable H1 EMA trend hard block. `false` = both directions always allowed. |
| `h1_ema_fast_period` | `9` | Fast EMA period for the H1 trend filter. |
| `h1_ema_slow_period` | `21` | Slow EMA period for the H1 trend filter. |
| `h1_candle_count` | `30` | Number of H1 candles fetched to compute the H1 trend EMAs. |

Logic: H1 EMA9 > EMA21 → only BUY signals pass. H1 EMA9 < EMA21 → only SELL signals pass. H1 flat → both allowed.

**ORB direction lock** — applied after ORB is fetched:

| Key | Default | Description |
|---|---|---|
| `orb_direction_lock` | `true` | Block trades against the confirmed ORB direction. `false` = disable. |

Logic: if price < ORB low (session is bearish) → BUY signals blocked. If price > ORB high (session is bullish) → SELL signals blocked. If price inside ORB → both directions allowed.

---

## Opening Range Breakout (ORB)

Controls how the ORB signal is scored based on how fresh the breakout is.

| Key | Default | Description |
|---|---|---|
| `orb_formation_minutes` | `15` | Minutes after session open before the ORB is considered formed. ORB = first completed M15 candle. |
| `orb_fresh_minutes` | `60` | ORB break within this many minutes of session open scores **+2 pts** (full weight). |
| `orb_aging_minutes` | `120` | ORB break between `orb_fresh_minutes` and this value scores **+1 pt** (half weight). After this it scores **+0 pts** (expired). |

**Example with defaults:**
```
London ORB forms at 16:15 SGT
  16:15 – 17:15 (0–60 min):   ORB break = +2 pts  [fresh]
  17:15 – 18:15 (60–120 min): ORB break = +1 pt   [aging]
  18:15+ (120+ min):           ORB break = +0 pts  [stale — expired]
```

After the ORB expires, only a **fresh EMA cross (+3) + CPR (+1) = 4** can reach the threshold.

---

## ATR (Average True Range)

Used for the exhaustion penalty — prevents trading when price is over-stretched.

| Key | Default | Description |
|---|---|---|
| `atr_period` | `14` | Lookback period for ATR calculation on M15 candles. Standard value — rarely needs changing. |
| `exhaustion_atr_mult` | `3.0` | If price is stretched more than this many ATR from the EMA midpoint, score is penalised by −1. Does not apply to ORB breakouts. |

---

## Stop Loss & Take Profit

| Key | Default | Description |
|---|---|---|
| `sl_mode` | `"pct_based"` | SL calculation method. `pct_based` = percentage of entry price. |
| `sl_pct` | `0.0025` | SL as a fraction of entry price. `0.0025` = 0.25%. At $4650 gold this is ~$11.60. |
| `sl_min_usd` | `2.0` | Minimum SL in USD. Prevents extremely tight stops on small price moves. |
| `sl_max_usd` | `15.0` | Maximum SL in USD. Caps the SL size on volatile candles. |
| `tp_mode` | `"rr_multiple"` | TP calculation method. `rr_multiple` = SL × `rr_ratio`. `fixed_usd` = fixed dollar amount. |
| `tp_pct` | `0.0035` | TP percentage — only used when `tp_mode` is `scalp_pct`. |
| `rr_ratio` | `2.5` | TP = SL × this value when `tp_mode` is `rr_multiple`. At SL=$11.60, TP = $29.00. |
| `min_rr_ratio` | `2.0` | Minimum acceptable RR before a trade is blocked. If actual RR < this, signal is rejected. |
| `fixed_tp_usd` | `null` | Fixed TP in USD — only used when `tp_mode` is `fixed_usd`. |
| `breakeven_enabled` | `false` | Move SL to breakeven when trade reaches `breakeven_trigger_usd` profit. Off by default. |
| `breakeven_trigger_usd` | `5.0` | Profit in USD needed before SL moves to breakeven. Only active if `breakeven_enabled` is `true`. |

---

## Position Sizing

| Key | Default | Description |
|---|---|---|
| `position_full_usd` | `100` | Risk amount in USD for high-conviction signals (score 5–6). |
| `position_partial_usd` | `66` | Risk amount in USD for standard signals (score 4). |
| `account_balance_override` | `0` | Override the live balance for sizing calculations. `0` = use real balance from OANDA. |

---

## Risk Controls & Caps

| Key | Default | Description |
|---|---|---|
| `max_concurrent_trades` | `1` | Maximum open trades at any time. Bot skips new signals if already in a trade. |
| `max_trades_day` | `20` | Maximum total trades per trading day (resets at `trading_day_start_hour_sgt`). |
| `max_losing_trades_day` | `8` | Maximum losing trades per day. Bot stops trading for the day after this many losses. |
| `max_trades_london` | `10` | Maximum trades in the London session (16:00–20:59 SGT). |
| `max_trades_us` | `10` | Maximum trades in the US session (21:00–00:59 SGT). |
| `max_losing_trades_session` | `4` | Maximum losing trades per session window. Resets when session changes. |
| `loss_streak_cooldown_min` | `30` | Minutes to pause after 2 consecutive losses. Prevents revenge-trading streaks. |
| `sl_reentry_gap_min` | `5` | Minutes to wait after a stop-loss before entering a new trade. |

---

## Session Windows

| Key | Default | Description |
|---|---|---|
| `session_only` | `true` | `true` = only trade during London and US windows. `false` = trade any time (not recommended). |
| `trading_day_start_hour_sgt` | `8` | Hour (SGT) when daily counters reset (trade count, loss count). |
| `friday_cutoff_hour_sgt` | `23` | Stop trading on Friday after this hour SGT. |
| `cycle_minutes` | `15` | How often the bot runs its trade evaluation loop. Aligned to M15 candle close timing. |

---

## Spread Filter

| Key | Default | Description |
|---|---|---|
| `spread_limits` | `{"London": 130, "US": 130}` | Maximum spread in pips per session. XAU/USD pips are in cents — 130 pips = $1.30 spread. |
| `max_spread_pips` | `150` | Global spread cap across all sessions. |

---

## News Filter

| Key | Default | Description |
|---|---|---|
| `news_filter_enabled` | `true` | Block trading around high-impact USD/gold news events. |
| `news_block_before_min` | `30` | Minutes before a news event to stop entering new trades. |
| `news_block_after_min` | `30` | Minutes after a news event before resuming trading. |
| `news_lookahead_min` | `120` | How far ahead (in minutes) to look for upcoming news events. |
| `news_medium_penalty_score` | `-1` | Score penalty for medium-impact news within the lookahead window. |

---

## Economic Calendar

| Key | Default | Description |
|---|---|---|
| `calendar_refresh_interval_min` | `60` | Minutes between calendar fetches from Forex Factory. |
| `calendar_retry_after_min` | `15` | Minutes to wait before retrying after a failed fetch. |
| `calendar_prune_days_ahead` | `21` | Days ahead to keep events in the local cache. Events beyond this are pruned. |

---

## Margin & OANDA

| Key | Default | Description |
|---|---|---|
| `xau_margin_rate_override` | `0.05` | Minimum margin rate for XAU/USD. Used as a floor — takes the higher of live OANDA rate and this value. `0.05` = 5% margin. |
| `margin_safety_factor` | `0.6` | Fraction of free margin available for new trades. `0.6` = use at most 60% of free margin. |
| `margin_retry_safety_factor` | `0.4` | Reduced safety factor used when retrying after a margin rejection. |
| `auto_scale_on_margin_reject` | `true` | Automatically reduce position size and retry if OANDA rejects due to insufficient margin. |
| `telegram_show_margin` | `true` | Include margin details in Telegram trade alerts. |

---

## Reporting & Maintenance

| Key | Default | Description |
|---|---|---|
| `startup_dedup_seconds` | `90` | Suppress duplicate startup Telegram messages sent within this window (seconds). Prevents spam on rapid Railway restarts. |
| `db_retention_days` | `90` | Days of trade history to keep in the local SQLite database. Older records are purged. |
| `db_cleanup_hour_sgt` | `0` | Hour (SGT) when the daily database cleanup runs. |
| `db_cleanup_minute_sgt` | `15` | Minute past the hour when cleanup runs. Default: 00:15 SGT. |
| `db_vacuum_weekly` | `true` | Run SQLite VACUUM weekly (Sundays) to reclaim disk space. |

---

## Quick Reference — Most Commonly Tuned

```json
"signal_threshold": 4,          ← raise to 5 = fewer but better trades
"session_thresholds": {
  "London": 4,                  ← raise to 5 if London win rate < 45%
  "US": 4
},
"orb_fresh_minutes": 60,        ← lower = stricter ORB freshness
"orb_aging_minutes": 120,       ← lower = ORB expires sooner
"rr_ratio": 2.5,                ← raise for bigger wins, fewer hits
"sl_pct": 0.0025,               ← 0.25% stop loss
"loss_streak_cooldown_min": 30, ← raise to 45 if streaks persist
"sl_reentry_gap_min": 5         ← raise to 10 to slow re-entry after SL
```
