# RF Scalp v1.6 — Technical Specification & Operations Wiki

**Bot Name:** RF Scalp v1.6
**Instrument:** XAU/USD (Gold)
**Exchange:** OANDA (practice & live)
**Deployment:** Railway (PaaS)
**Signal Timeframe:** M15 (15-minute candles)
**Cycle Interval:** Every 15 minutes
**Status:** Demo mode (switch `demo_mode: false` for live)

---

## 1. Purpose & Scope

RF Scalp v1.6 is a fully automated gold scalping bot for XAU/USD. It was forked from RF v2.4
(CPR breakout) and uses a four-layer signal engine: H1 trend filter, EMA 9/21 crossover on M15,
Opening Range Breakout, and CPR pivot bias. All infrastructure — Railway deployment, OANDA order
execution, Telegram alerts, news filtering, database, and reporting — is inherited from RF v2.4.

v1.6 specifically addresses the low London win rates observed in v1.4–v1.5 (38% → 25% → 0%
across three sessions) by adding two pre-score hard blocks, upgrading the signal candle from
M5 to M15, and tightening session and daily trade caps.

This document covers strategy logic, signal engine internals, configuration, deployment,
operations, and troubleshooting.

---

## 2. Architecture Overview

```
scheduler.py  (APScheduler — every 15 min)
      |
      v
bot.py  run_bot_cycle()
      |
      +---> session check (London / US / dead zone)
      +---> news_filter.py (economic calendar)
      +---> signals.py  SignalEngine.analyze()
      |         |
      |         +-- [PRE-BLOCK] H1 trend filter (new v1.6)
      |         +-- [PRE-BLOCK] ORB direction lock (new v1.6)
      |         +-- Layer 1: EMA 9/21 crossover on M15
      |         +-- Layer 2: ORB session range breakout (time-decayed)
      |         +-- Layer 3: CPR daily pivot bias
      |
      +---> risk checks (caps, cooldown, spread, margin)
      +---> oanda_trader.py (place order)
      +---> database.py (record trade/signal)
      +---> telegram_templates.py (send alert)
```

---

## 3. Signal Engine — signals.py

### 3.1 Data Inputs

| Data | Source | Granularity | Count | Cache |
|---|---|---|---|---|
| H1 candles (H1 trend filter) | OANDA REST API | H1 | 30 candles | None (live each cycle) |
| M15 candles (EMA signal) | OANDA REST API | M15 | 40 candles | None (live each cycle) |
| M15 candles (ORB detection) | OANDA REST API | M15 | 12 candles + timestamps | `orb_cache.json` per session/day |
| Daily candles (CPR) | OANDA REST API | D | 3 candles | None (live each cycle) |

### 3.2 Pre-Score Hard Blocks (new v1.6)

These run before any scoring. If either block fires, the signal returns score=0 immediately
and no trade is placed. They are binary pass/fail gates — not scored.

#### Block 1 — H1 Trend Filter

Fetches H1 EMA9 and EMA21 before evaluating any M15 signal. If the M15 signal direction
disagrees with the H1 trend, the signal is blocked entirely.

```
H1 EMA9 > EMA21 (bullish)  →  only BUY signals pass
H1 EMA9 < EMA21 (bearish)  →  only SELL signals pass
H1 EMAs flat   (neutral)   →  both directions allowed
```

This is the primary fix for consecutive SL hits on trending days, where M15 EMA repeatedly
crosses against the macro H1 direction before the move exhausts.

**Log signature:** `Signal H1 BLOCKED | dir=BUY but H1 BEARISH`
**Disable:** set `h1_trend_filter_enabled: false` in settings.json

#### Block 2 — ORB Direction Lock

After the ORB is formed, if price has confirmed a session direction, trades in the opposite
direction are blocked.

```
Price > ORB high  →  SELL signals blocked (session is bullish)
Price < ORB low   →  BUY signals blocked  (session is bearish)
Price inside ORB  →  both directions allowed
```

**Log signature:** `Signal ORB LOCKED | dir=BUY but price below ORB low`
**Disable:** set `orb_direction_lock: false` in settings.json

### 3.3 Layer 1 — EMA 9/21 Crossover

**Implementation:**
- EMA series computed on completed M15 candles only (live candle excluded)
- Uses exponential smoothing: `EMA = price × k + prev_EMA × (1 − k)` where `k = 2 / (period + 1)`
- Fresh cross detected by comparing `[-1]` and `[-2]` values of both series

**Scoring logic:**

| Condition | Direction | Score |
|---|---|---|
| Fresh bull cross (EMA9 just crossed above EMA21) | BUY | +3 |
| Fresh bear cross (EMA9 just crossed below EMA21) | SELL | +3 |
| EMA9 > EMA21 (aligned, no fresh cross) | BUY | +1 |
| EMA9 < EMA21 (aligned, no fresh cross) | SELL | +1 |
| EMA9 == EMA21 | NONE | 0 — exit |

**Why M15 over M5:** M5 generated 8–14 EMA crosses per session on gold, mostly wick noise.
M15 requires 15 minutes of sustained directional pressure to form a cross.

### 3.4 Layer 2 — Opening Range Breakout (ORB)

**ORB definition:** High and low of the first completed M15 candle at or after session open.

| Session | Open (SGT) | Open (GMT) |
|---|---|---|
| London | 16:00 | 08:00 |
| US | 21:00 | 13:00 |

**Cache:** `data/orb_cache.json` — key format: `YYYY-MM-DD_London` / `YYYY-MM-DD_US`

**Scoring (time-decayed):**

| Age of ORB break | Score |
|---|---|
| 0 – 60 min (fresh) | +2 |
| 60 – 120 min (aging) | +1 |
| 120+ min (stale) | +0 |
| Price inside ORB / ORB not formed | +0 |

### 3.5 Layer 3 — CPR Pivot Bias

Calculated from previous day OANDA candle: `Pivot = (PDH + PDL + PDC) / 3`

Only the pivot is used for scoring. Price above pivot on BUY = +1. Price below pivot on SELL = +1.

### 3.6 Exhaustion Penalty

If price is stretched more than `exhaustion_atr_mult` (default 3.0) ATR from the EMA midpoint,
score is reduced by 1. Does not apply when an ORB breakout contributed to the score.

### 3.7 Score-to-Decision Flow

```
Pre-score blocks (v1.6):
  H1 trend disagrees with direction   →  BLOCKED (score=0)
  ORB direction lock triggered        →  BLOCKED (score=0)

Scoring (max 6):
  score >= 5  →  $100 full position
  score >= 3  →  $66  partial position
  score < 4   →  No trade

Post-score blockers:
  R:R < 2.0, spread exceeded, news block, daily/session cap,
  cooldown active, direction block active, Friday cutoff
```

---

## 4. SL / TP Logic

Default mode: `pct_based` SL + `rr_multiple` TP

```
SL = entry × 0.0025  (0.25%)
TP = SL × 2.5
```

At gold $3,300: SL ≈ $8.25, TP ≈ $20.63. Clamped: SL min $2, max $15.

---

## 5. Session & Risk Controls

### Session Windows

| Window | SGT | Threshold | Cap |
|---|---|---|---|
| London | 16:00 – 20:59 | Score ≥ 5 | 4 trades |
| US | 21:00 – 00:59 | Score ≥ 4 | 10 trades |
| Dead Zone | 01:00 – 15:59 | N/A | 0 |

### Daily Limits

| Limit | Value |
|---|---|
| Max trades/day | 10 |
| Max losses/day | 5 |
| Max losses/session | 2 |
| Loss cooldown | 30 min |
| Consecutive SL direction block | 90 min (after 2 SLs in same direction) |
| Friday cutoff | 23:00 SGT |

### Spread Control

London and US: 130 pip limit. Global hard cap: 150 pips.

### News Filter

Blocks ±30 minutes around high-impact events. Medium-impact events apply −1 score penalty.
Calendar from TradingEconomics API, refreshed every 60 minutes.

---

## 6. Order Execution

- Instrument: `XAU_USD`, market order with attached SL and TP
- Units: `position_usd / sl_usd` (risk-per-unit sizing)
- Margin safety: `margin_safety_factor = 0.6`
- Auto-scale on margin reject

---

## 7. Database & State Files

| File | Purpose |
|---|---|
| `data/rf_scalp.db` | SQLite — signals + trades, 90-day rolling window |
| `data/orb_cache.json` | ORB high/low per session per SGT day |
| `data/signal_cache.json` | Telegram dedup cache |
| `data/ops_state.json` | Session state, cooldown flags, daily caps |
| `data/runtime_state.json` | Cycle health — last started, OANDA failure count |
| `data/trade_history.json` | Rolling 90-day trade log |

---

## 8. Telegram Reporting

### Real-time Alerts

| Message | When |
|---|---|
| Startup | Bot process starts |
| Session Open | London / US each day |
| Scalp Signal Update | Each 15-min cycle |
| H1 Block | H1 trend mismatch blocked signal (new v1.6) |
| ORB Lock | ORB direction conflict blocked signal (new v1.6) |
| Trade Opened / Closed | On fill / TP or SL hit |
| News Block / Penalty | Calendar event |
| Spread Skip | Spread above limit |
| Daily / Session Cap | Limit hit |
| Cooldown / Direction Block | Risk protection activated |
| Friday Cutoff | No new trades |
| Margin Adjusted | Position scaled down |

### Scheduled Reports

Daily: Mon–Fri 15:30 SGT · Weekly: Mon 08:15 · Monthly: First Mon 08:00

---

## 9. Railway Deployment

```
1. Push RF Scalp v1.6 folder to GitHub repo
2. Railway → New Project → Deploy from GitHub Repo
3. Add env vars (Variables tab)
4. Auto-deploys on push to main
```

**Required env vars:** `OANDA_API_KEY`, `OANDA_ACCOUNT_ID`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`, `TRADINGECONOMICS_API_KEY`

Health check: `GET /health` → `{"status": "ok", "scheduler_running": true, ...}`

---

## 10. Going Live Checklist

- [ ] 2+ weeks demo mode with acceptable win rate (> 40%) and R:R (> 1.8)
- [ ] Confirm H1 block logging correctly (`H1 BLOCKED` in Railway logs)
- [ ] Confirm ORB lock logging correctly (`ORB LOCKED` in Railway logs)
- [ ] Update OANDA live credentials in Railway Variables
- [ ] Set `demo_mode: false` in settings.json and redeploy
- [ ] Monitor first 3 live trades via Telegram

---

## 11. Differences from RF v2.4

| Component | RF v2.4 | RF Scalp v1.6 |
|---|---|---|
| Signal engine | CPR breakout | H1 filter + ORB lock + EMA M15 + ORB + CPR |
| Signal timeframe | M15 | M15 |
| H1 trend filter | None | Hard block (new v1.6) |
| ORB direction guard | None | Hard block (new v1.6) |
| Primary signal | CPR/PDH/R1 breakout | EMA9 crosses EMA21 |
| Secondary | SMA20/SMA50 | ORB time-decayed breakout |
| Tertiary | CPR width | CPR pivot bias |
| `cycle_minutes` | — | 15 |
| `max_trades_day` | — | 10 |
| `max_trades_london` | — | 4 |
| `consecutive_sl_block_minutes` | — | 90 |
| `h1_trend_filter_enabled` | — | true (new) |
| `orb_direction_lock` | — | true (new) |
| `version.py` | CPR Gold Bot v2.4 | RF Scalp v1.6 |

---

## 12. Troubleshooting

**Bot not trading during session hours:**
Check `data/ops_state.json` for `daily_cap_reached`. Check Railway logs for `H1 BLOCKED`
or `ORB LOCKED` — these are expected on trending sessions and mean the filter is working.

**H1 block firing every cycle:**
The H1 trend is clearly established. This is correct. Wait for H1 EMA9/21 to flip. Do not
disable the filter — this is it working as designed.

**ORB not forming:**
ORB requires 15+ minutes after session open. Check `data/orb_cache.json` for today's entry.

**Low signal score every cycle:**
Check for H1/ORB blocks first. Then: EMA may be flat (M15 needs sustained pressure), ORB
may not have formed, CPR may be against direction, or exhaustion penalty may be reducing score.

**R:R below 2.0 blocking trades:**
Ensure `tp_mode` is set to `rr_multiple` (default). With `rr_ratio: 2.5` this always passes.

---

*RF Scalp v1.6 — Technical Specification*
*Forked from RF v2.4 · H1 trend filter + ORB lock + EMA 9/21 M15 + ORB + CPR*
*Deployed on Railway · OANDA · Telegram*
