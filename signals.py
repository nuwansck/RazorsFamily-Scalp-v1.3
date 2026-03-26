"""Signal engine for EMA crossover + ORB scalping on XAU/USD — RF Scalp v1.6.0

Timeframe stack:
  H1  — Trend filter (hard block — only trade WITH H1 EMA9/21 direction)
  M15 — EMA signal (entry trigger — upgraded from M5 to reduce noise)

Strategy: EMA crossover + Opening Range Breakout (ORB) + CPR Bias

v1.6.0 changes vs v1.5:
  - H1 trend filter: hard block — if M15 direction disagrees with H1 EMA9/21,
    the signal is blocked entirely. No counter-trend trades.
    H1 EMA9 > EMA21 (bullish) → only BUY signals pass
    H1 EMA9 < EMA21 (bearish) → only SELL signals pass
    H1 EMAs flat   (neutral)  → both directions allowed
    Disable via h1_trend_filter_enabled: false in settings.json
  - ORB direction lock: if ORB formed and price confirmed a side (e.g. below
    ORB low), block trades in the opposite direction entirely.
    Disable via orb_direction_lock: false in settings.json
  - Signal candle: M5 → M15 (cuts noise signals by ~60%; same logic, M15 data)
  - cycle_minutes: 5 → 15 (aligns cycle to M15 candle close)
  - Daily/session caps tightened (max_trades_day 20→10, london cap 10→4)
  - consecutive_sl_block_minutes: 60 → 90 (matches Aurum; avoids re-entry into
    same trending move before it exhausts)

Scoring (Bull — BUY):
  EMA cross     — fresh EMA9 crosses above EMA21 (last 2 M15 candles): +3
                  EMA9 already above EMA21 (aligned, no fresh cross):   +1
  ORB           — price above ORB high, time-weighted:
                    0–orb_fresh_minutes:              +2 (fresh break)
                    orb_fresh_minutes–orb_aging_minutes: +1 (aging)
                    orb_aging_minutes+:               +0 (stale, expired)
  CPR bias      — price above CPR pivot: +1

Scoring (Bear — SELL):
  EMA cross     — fresh EMA9 crosses below EMA21 (last 2 M15 candles): +3
                  EMA9 already below EMA21 (aligned, no fresh cross):   +1
  ORB           — price below ORB low, same time-weighted decay
  CPR bias      — price below CPR pivot: +1

Max score: 6  |  Min threshold: signal_threshold (default 4)

Pre-score hard blocks (do not affect score — either pass or kill the signal):
  H1 trend filter  — direction vs H1 EMA9/21 (new v1.6.0)
  ORB direction lock — direction vs confirmed ORB side (new v1.6.0)

All scoring parameters are read from settings.json — no hardcoded values.
Key settings: ema_fast_period, ema_slow_period, orb_fresh_minutes,
orb_aging_minutes, orb_formation_minutes, min_rr_ratio, sl_pct, tp_pct,
rr_ratio, tp_mode, exhaustion_atr_mult, signal_threshold,
h1_trend_filter_enabled, h1_ema_fast_period, h1_ema_slow_period,
h1_candle_count, orb_direction_lock.

ORB definition:
  London session ORB — first completed M15 candle from 16:00 SGT (08:00 GMT)
  US session ORB     — first completed M15 candle from 21:00 SGT (13:00 GMT)
  ORB cached per session per SGT day in orb_cache.json.

CPR: fetched from OANDA daily candles, cached per SGT day.
CPR used only as directional bias filter (+1), NOT as primary signal.
"""

import time
import logging
from datetime import datetime as _dt
import pytz as _pytz
from config_loader import load_secrets, load_settings, DATA_DIR
from state_utils import load_json, save_json
from oanda_trader import make_oanda_session

log = logging.getLogger(__name__)

_CPR_CACHE_FILE = DATA_DIR / "cpr_cache.json"
_ORB_CACHE_FILE = DATA_DIR / "orb_cache.json"
_SGT = _pytz.timezone("Asia/Singapore")
_UTC = _pytz.utc

# Default fallback constants — actual values are always read from settings.json.
# These exist only so the module can be imported without a settings file
# (e.g. unit tests). Live bot always passes settings explicitly.
MIN_TRADE_SCORE = 4       # overridden by settings["signal_threshold"]
EMA_FAST        = 9       # overridden by settings["ema_fast_period"]
EMA_SLOW        = 21      # overridden by settings["ema_slow_period"]
SCALP_SL_PCT    = 0.0025  # overridden by settings["sl_pct"]
SCALP_TP_PCT    = 0.0035  # overridden by settings["tp_pct"]

# ORB decay defaults — overridden by settings["orb_fresh_minutes"] / ["orb_aging_minutes"]
ORB_FRESH_MINUTES = 60    # 0–60 min: +2 pts
ORB_AGING_MINUTES = 120   # 60–120 min: +1 pt, 120+: 0 pts

# Session ORB open times in SGT (hour, minute)
ORB_SESSIONS = {
    "London": (16, 0),
    "US":     (21, 0),
}


def score_to_position_usd(score: int, settings: dict | None = None) -> int:
    """Return the risk-dollar position size for a given score."""
    full    = int((settings or {}).get("position_full_usd",    100))
    partial = int((settings or {}).get("position_partial_usd",  66))
    size_tiers = [
        (4, full),
        (2, partial),
    ]
    for threshold, size in size_tiers:
        if score > threshold:
            return size
    return 0


def _validate_cpr_levels(levels: dict) -> tuple:
    """Validate CPR levels for structural consistency."""
    required = {"pivot", "tc", "bc", "r1", "r2", "s1", "s2", "pdh", "pdl", "cpr_width_pct"}
    missing = required - set(levels.keys())
    if missing:
        return False, "missing keys: {}".format(missing)
    pivot = levels["pivot"]; tc = levels["tc"]; bc = levels["bc"]
    r1 = levels["r1"]; r2 = levels["r2"]; s1 = levels["s1"]
    s2 = levels["s2"]; pdh = levels["pdh"]; pdl = levels["pdl"]
    cpr_w = levels["cpr_width_pct"]
    if not (tc > bc):           return False, "TC must be > BC"
    if not (r1 > pivot):        return False, "R1 must be > pivot"
    if not (pivot > s1):        return False, "pivot must be > S1"
    if not (r2 > r1):           return False, "R2 must be > R1"
    if not (s2 < s1):           return False, "S2 must be < S1"
    if not (pdh > pdl):         return False, "PDH must be > PDL"
    if not (pdl <= pivot <= pdh): return False, "pivot must be between PDL and PDH"
    if not (cpr_w > 0):         return False, "cpr_width_pct must be > 0"
    return True, ""


class SignalEngine:
    def __init__(self, demo: bool = True):
        secrets = load_secrets()
        self.api_key    = secrets.get("OANDA_API_KEY",    "")
        self.account_id = secrets.get("OANDA_ACCOUNT_ID", "")
        self.base_url   = (
            "https://api-fxpractice.oanda.com" if demo else "https://api-fxtrade.oanda.com"
        )
        self.headers = {
            "Authorization": "Bearer {}".format(self.api_key),
            "Content-Type":  "application/json",
        }
        self.session = make_oanda_session(allowed_methods=["GET"])

    def analyze(self, asset: str = "XAUUSD", settings: dict | None = None):
        """Run the RF Scalp EMA + ORB (time-decayed) + CPR-bias scoring engine.

        Returns (score, direction, details, levels, position_usd)
        """
        if settings is None:
            settings = load_settings()
        if asset != "XAUUSD":
            return 0, "NONE", "Only XAUUSD supported", {}, 0

        instrument = "XAU_USD"

        # -- 1. CPR levels (bias filter only) ---------------------------------
        levels, pivot, tc, bc, cpr_width_pct = self._get_cpr_levels(instrument)
        if levels is None:
            return 0, "NONE", "Could not fetch CPR levels", {}, 0

        # -- 2. H1 trend filter — fetch before scoring ------------------------
        # Hard block: if H1 EMA direction disagrees with the M5 signal,
        # the trade is blocked entirely. No counter-trend trades.
        # H1 EMA9 > EMA21 (bullish)  → only BUY signals pass
        # H1 EMA9 < EMA21 (bearish)  → only SELL signals pass
        # H1 EMAs flat   (neutral)   → both directions allowed
        # Disable via h1_trend_filter_enabled: false in settings.json
        _h1_enabled  = bool((settings or {}).get("h1_trend_filter_enabled", True))
        _h1_ema_fast = int((settings or {}).get("h1_ema_fast_period", 9))
        _h1_ema_slow = int((settings or {}).get("h1_ema_slow_period", 21))
        _h1_count    = int((settings or {}).get("h1_candle_count",    30))

        h1_trend    = "NEUTRAL"   # BULLISH | BEARISH | NEUTRAL
        h1_ema_fast = None
        h1_ema_slow = None

        if _h1_enabled:
            h1_closes, _, _ = self._fetch_candles(instrument, "H1", _h1_count)
            if len(h1_closes) >= _h1_ema_slow + 2:
                h1_fast_series = self._ema_series(h1_closes[:-1], _h1_ema_fast)
                h1_slow_series = self._ema_series(h1_closes[:-1], _h1_ema_slow)
                if h1_fast_series and h1_slow_series:
                    h1_ema_fast = round(h1_fast_series[-1], 2)
                    h1_ema_slow = round(h1_slow_series[-1], 2)
                    if h1_ema_fast > h1_ema_slow:
                        h1_trend = "BULLISH"
                    elif h1_ema_fast < h1_ema_slow:
                        h1_trend = "BEARISH"
            else:
                log.warning("Not enough H1 candles for trend filter — treating as NEUTRAL")

        levels["h1_trend"]    = h1_trend
        levels["h1_ema_fast"] = h1_ema_fast
        levels["h1_ema_slow"] = h1_ema_slow

        # -- 3. M5 candles for EMA + price ------------------------------------
        # Read EMA periods from settings (falls back to module-level defaults)
        _ema_fast      = int((settings or {}).get("ema_fast_period",  EMA_FAST))
        _ema_slow      = int((settings or {}).get("ema_slow_period",  EMA_SLOW))
        _atr_period    = int((settings or {}).get("atr_period",       14))
        _m5_count      = int((settings or {}).get("m5_candle_count",  40))

        m15_closes, m15_highs, m15_lows = self._fetch_candles(instrument, "M15", _m5_count)
        # Alias so remainder of signal logic is unchanged
        m5_closes, m5_highs, m5_lows = m15_closes, m15_highs, m15_lows
        if len(m5_closes) < _ema_slow + 3:
            return 0, "NONE", "Not enough M5 data (need {} candles)".format(_ema_slow + 3), levels, 0

        current_close = m5_closes[-1]
        atr_val = self._atr(m5_highs, m5_lows, m5_closes, _atr_period)
        levels["atr"]           = round(atr_val, 2) if atr_val else None
        levels["current_price"] = round(current_close, 2)

        # -- 3. EMA (fast/slow periods from settings) on M5 -------------------
        ema_fast_series = self._ema_series(m5_closes[:-1], _ema_fast)
        ema_slow_series = self._ema_series(m5_closes[:-1], _ema_slow)

        if len(ema_fast_series) < 2 or len(ema_slow_series) < 2:
            return 0, "NONE", "Not enough EMA data", levels, 0

        ema_fast_now  = ema_fast_series[-1]
        ema_slow_now  = ema_slow_series[-1]
        ema_fast_prev = ema_fast_series[-2]
        ema_slow_prev = ema_slow_series[-2]

        levels[f"ema{_ema_fast}"]  = round(ema_fast_now, 2)
        levels[f"ema{_ema_slow}"] = round(ema_slow_now, 2)

        # -- 4. ORB -----------------------------------------------------------
        now_sgt      = _dt.now(_SGT)
        session_name = self._get_active_session(now_sgt)
        orb_high, orb_low, orb_formed = self._get_orb(session_name, instrument, now_sgt)

        # ORB age — minutes since session open (used for time-decay scoring)
        _orb_age_min = 0
        if orb_formed and session_name in ORB_SESSIONS:
            import datetime as _dt_mod
            oh, om = ORB_SESSIONS[session_name]
            open_sgt = now_sgt.replace(hour=oh, minute=om, second=0, microsecond=0)
            if now_sgt.hour == 0 and session_name == "US":
                open_sgt = open_sgt - _dt_mod.timedelta(days=1)
            _orb_age_min = max(0, int((now_sgt - open_sgt).total_seconds() / 60))

        levels["orb_high"]    = round(orb_high, 2) if orb_high else None
        levels["orb_low"]     = round(orb_low, 2)  if orb_low  else None
        levels["orb_age_min"] = _orb_age_min
        levels["orb_formed"] = orb_formed
        levels["session"]    = session_name

        # -- 5. Scoring -------------------------------------------------------
        score     = 0
        direction = "NONE"
        setup     = "No Setup"
        reasons   = []

        reasons.append(
            "EMA9={:.2f} EMA21={:.2f} | Price={:.2f} | CPR pivot={:.2f} | CPR width={:.2f}%".format(
                ema_fast_now, ema_slow_now, current_close, pivot, cpr_width_pct
            )
        )

        # 5a. EMA crossover (sets direction) ----------------------------------
        fresh_bull = (ema_fast_now > ema_slow_now) and (ema_fast_prev <= ema_slow_prev)
        fresh_bear = (ema_fast_now < ema_slow_now) and (ema_fast_prev >= ema_slow_prev)
        bull_align = ema_fast_now > ema_slow_now
        bear_align = ema_fast_now < ema_slow_now

        if fresh_bull:
            direction = "BUY";  score += 3;  setup = "EMA Fresh Cross Up"
            reasons.append(
                "EMA9 fresh cross ABOVE EMA21 | prev({:.2f}/{:.2f}) -> now({:.2f}/{:.2f}) (+3)".format(
                    ema_fast_prev, ema_slow_prev, ema_fast_now, ema_slow_now
                )
            )
        elif fresh_bear:
            direction = "SELL"; score += 3;  setup = "EMA Fresh Cross Down"
            reasons.append(
                "EMA9 fresh cross BELOW EMA21 | prev({:.2f}/{:.2f}) -> now({:.2f}/{:.2f}) (+3)".format(
                    ema_fast_prev, ema_slow_prev, ema_fast_now, ema_slow_now
                )
            )
        elif bull_align:
            direction = "BUY";  score += 1;  setup = "EMA Trend Up"
            reasons.append(
                "EMA9={:.2f} above EMA21={:.2f} | aligned bull, no fresh cross (+1)".format(
                    ema_fast_now, ema_slow_now
                )
            )
        elif bear_align:
            direction = "SELL"; score += 1;  setup = "EMA Trend Down"
            reasons.append(
                "EMA9={:.2f} below EMA21={:.2f} | aligned bear, no fresh cross (+1)".format(
                    ema_fast_now, ema_slow_now
                )
            )
        else:
            reasons.append("No EMA bias (+0)")
            return 0, "NONE", " | ".join(reasons), levels, 0

        # 5b-pre. H1 trend hard block (Fix #1 — v1.6.0) -----------------------
        # If H1 EMA direction is clear and opposes the M15 signal, block it
        # entirely. Primary cause of consecutive SL hits on trending days.
        if _h1_enabled and h1_trend != "NEUTRAL":
            if direction == "BUY" and h1_trend == "BEARISH":
                block_reason = (
                    "H1 TREND BLOCK — H1 EMA bearish ({:.2f}<{:.2f}), "
                    "no BUY until H1 turns bullish".format(
                        h1_ema_fast or 0, h1_ema_slow or 0))
                reasons.append(block_reason)
                levels["h1_blocked"] = True
                log.info("Signal H1 BLOCKED | dir=BUY but H1 BEARISH | %s", block_reason)
                return 0, "NONE", " | ".join(reasons), levels, 0
            if direction == "SELL" and h1_trend == "BULLISH":
                block_reason = (
                    "H1 TREND BLOCK — H1 EMA bullish ({:.2f}>{:.2f}), "
                    "no SELL until H1 turns bearish".format(
                        h1_ema_fast or 0, h1_ema_slow or 0))
                reasons.append(block_reason)
                levels["h1_blocked"] = True
                log.info("Signal H1 BLOCKED | dir=SELL but H1 BULLISH | %s", block_reason)
                return 0, "NONE", " | ".join(reasons), levels, 0
            reasons.append("H1 {} confirms {} direction ✓".format(h1_trend, direction))
            levels["h1_blocked"] = False
        else:
            levels["h1_blocked"] = False
            if not _h1_enabled:
                reasons.append("H1 filter disabled")
            else:
                reasons.append("H1 NEUTRAL — both directions allowed")

        # 5b-pre2. ORB direction lock (Fix #2 — v1.6.0) -----------------------
        # If ORB is formed and price confirmed a session direction, block trades
        # going against that structure.
        _orb_lock = bool((settings or {}).get("orb_direction_lock", True))
        if _orb_lock and orb_formed and orb_high and orb_low:
            if direction == "BUY" and current_close < orb_low:
                reasons.append(
                    "ORB DIRECTION LOCK — price {:.2f} below ORB low {:.2f}, no BUY".format(
                        current_close, orb_low))
                log.info("Signal ORB LOCKED | dir=BUY but price below ORB low")
                return 0, "NONE", " | ".join(reasons), levels, 0
            if direction == "SELL" and current_close > orb_high:
                reasons.append(
                    "ORB DIRECTION LOCK — price {:.2f} above ORB high {:.2f}, no SELL".format(
                        current_close, orb_high))
                log.info("Signal ORB LOCKED | dir=SELL but price above ORB high")
                return 0, "NONE", " | ".join(reasons), levels, 0

        # 5b. ORB confirmation (time-decayed) ----------------------------------
        # v1.2.6: ORB points decay based on how long ago the session opened.
        # Fresh break gets full weight; aging break gets half; stale gets none.
        # Windows are configurable via orb_fresh_minutes / orb_aging_minutes.
        _orb_fresh_min = int((settings or {}).get("orb_fresh_minutes", ORB_FRESH_MINUTES))
        _orb_aging_min = int((settings or {}).get("orb_aging_minutes", ORB_AGING_MINUTES))

        if orb_formed and orb_high and orb_low:
            # Determine ORB point value based on age
            if _orb_age_min < _orb_fresh_min:
                _orb_pts   = 2
                _orb_label = "fresh (<{}min)".format(_orb_fresh_min)
            elif _orb_age_min < _orb_aging_min:
                _orb_pts   = 1
                _orb_label = "aging ({}-{}min)".format(_orb_fresh_min, _orb_aging_min)
            else:
                _orb_pts   = 0
                _orb_label = "stale (>{}min)".format(_orb_aging_min)

            if direction == "BUY" and current_close > orb_high:
                score += _orb_pts
                reasons.append(
                    "Price {:.2f} > ORB high {:.2f} | bullish ORB break (+{}) [{}]".format(
                        current_close, orb_high, _orb_pts, _orb_label
                    )
                )
            elif direction == "SELL" and current_close < orb_low:
                score += _orb_pts
                reasons.append(
                    "Price {:.2f} < ORB low {:.2f} | bearish ORB break (+{}) [{}]".format(
                        current_close, orb_low, _orb_pts, _orb_label
                    )
                )
            else:
                reasons.append(
                    "Price {:.2f} inside ORB [{:.2f}-{:.2f}] | no break (+0)".format(
                        current_close, orb_low, orb_high
                    )
                )
        else:
            reasons.append("ORB not yet formed for {} session (+0)".format(session_name or "N/A"))

        # 5c. CPR bias --------------------------------------------------------
        if direction == "BUY" and current_close > pivot:
            score += 1
            reasons.append("Price {:.2f} above CPR pivot {:.2f} | bullish bias (+1)".format(current_close, pivot))
        elif direction == "SELL" and current_close < pivot:
            score += 1
            reasons.append("Price {:.2f} below CPR pivot {:.2f} | bearish bias (+1)".format(current_close, pivot))
        else:
            reasons.append("CPR bias against direction (pivot={:.2f}) (+0)".format(pivot))

        # 5d. Exhaustion penalty ----------------------------------------------
        # v1.2 fix: skip exhaustion penalty when an ORB break contributed +2 to
        # the score. An ORB breakout by definition stretches price — penalising
        # it as "exhaustion" incorrectly zeroes out the best entries of the day.
        # The penalty still fires on EMA-only setups where stretch IS a concern.
        _orb_contributed = orb_formed and (
            (direction == "BUY"  and orb_high and current_close > orb_high) or
            (direction == "SELL" and orb_low  and current_close < orb_low)
        )
        _exhaust_mult = float((settings or {}).get("exhaustion_atr_mult", 3.0))
        if _exhaust_mult > 0 and atr_val and atr_val > 0 and not _orb_contributed:
            ema_mid  = (ema_fast_now + ema_slow_now) / 2
            _stretch = abs(current_close - ema_mid) / atr_val
            if _stretch > _exhaust_mult:
                score = max(score - 1, 0)
                reasons.append(
                    "Exhaustion: stretch={:.2f}x ATR (>{:.1f}x threshold) | score -1 -> {}/6".format(
                        _stretch, _exhaust_mult, score
                    )
                )
            else:
                reasons.append(
                    "Stretch {:.2f}x ATR (ok, no exhaustion penalty)".format(_stretch)
                )
        elif _orb_contributed:
            reasons.append(
                "Exhaustion check skipped — ORB breakout in progress (stretch not penalised)"
            )

        # -- 6. Position size -------------------------------------------------
        position_usd = score_to_position_usd(score, settings)

        # -- 7. Scalp SL/TP ---------------------------------------------------
        # v1.2.4 fix: respect tp_mode setting.
        # When tp_mode="rr_multiple" (the default), TP = SL × rr_ratio.
        # When tp_mode="scalp_pct" or any other value, TP = entry × tp_pct.
        # Previously, TP was ALWAYS computed as entry × tp_pct, meaning
        # tp_mode and rr_ratio were silently ignored — with sl_pct=0.0025 and
        # tp_pct=0.0035 this gives RR=1.40 which always failed the RR≥2 check.
        entry        = current_close
        sl_pct_used  = float((settings or {}).get("sl_pct", SCALP_SL_PCT))
        tp_pct_used  = float((settings or {}).get("tp_pct", SCALP_TP_PCT))
        sl_usd_rec   = round(entry * sl_pct_used, 2)

        _tp_mode     = str((settings or {}).get("tp_mode", "rr_multiple")).lower()
        _rr_ratio    = float((settings or {}).get("rr_ratio", 2.5))
        if _tp_mode == "rr_multiple" and _rr_ratio > 0:
            tp_usd_rec = round(sl_usd_rec * _rr_ratio, 2)
            tp_source  = "rr_multiple"
        else:
            tp_usd_rec = round(entry * tp_pct_used, 2)
            tp_source  = "scalp_pct"
        sl_source    = "scalp_pct"

        rr_ratio  = (tp_usd_rec / sl_usd_rec) if sl_usd_rec > 0 else 0
        _min_rr   = float((settings or {}).get("min_rr_ratio", 2.0))
        rr_skip   = rr_ratio < _min_rr
        blockers  = []
        if rr_skip:
            blockers.append("R:R {:.2f} < 1:{:.1f}".format(rr_ratio, _min_rr))

        # -- 8. Levels dict ---------------------------------------------------
        levels["score"]        = score
        levels["position_usd"] = position_usd
        levels["entry"]        = round(entry, 2)
        levels["setup"]        = setup
        levels["sl_usd_rec"]   = sl_usd_rec
        levels["sl_source"]    = sl_source
        levels["sl_pct_used"]  = sl_pct_used
        levels["tp_usd_rec"]   = tp_usd_rec
        levels["tp_source"]    = tp_source
        levels["tp_pct_used"]  = tp_pct_used
        levels["rr_ratio"]     = round(rr_ratio, 2)
        _min_score = int((settings or {}).get("signal_threshold", MIN_TRADE_SCORE))
        levels["mandatory_checks"] = {"score_ok": score >= _min_score, "rr_ok": not rr_skip}
        levels["quality_checks"]   = {"tp_ok": True}
        levels["signal_blockers"]  = blockers

        # Build the details label — when tp_mode is rr_multiple, show the
        # multiplier (e.g. "2.5x RR") instead of the raw tp_pct percentage.
        _tp_label = (
            "{:.1f}x RR".format(_rr_ratio)
            if _tp_mode == "rr_multiple"
            else "{:.2f}%".format(tp_pct_used * 100)
        )
        reasons.append(
            "SL=${:.2f} ({} {:.2f}%) | TP=${:.2f} ({} {}) | R:R 1:{:.1f}".format(
                sl_usd_rec, sl_source, sl_pct_used * 100,
                tp_usd_rec, tp_source, _tp_label,
                rr_ratio,
            )
        )
        if blockers:
            reasons.append("BLOCKED: " + " | ".join(blockers))

        details = " | ".join(reasons)
        if blockers:
            log.info("Scalp signal BLOCKED | setup=%s dir=%s score=%s/6 blockers=%s",
                     setup, direction, score, "; ".join(blockers))
        else:
            log.info("Scalp signal | setup=%s dir=%s score=%s/6 position=$%s",
                     setup, direction, score, position_usd)

        return score, direction, details, levels, position_usd

    # -- CPR helper -----------------------------------------------------------

    def _get_cpr_levels(self, instrument: str):
        today_str = _dt.now(_SGT).strftime("%Y-%m-%d")
        # CPR cache REMOVED — always fetch fresh levels each cycle.
        # The old cpr_cache.json file is no longer read or written.

        closes, highs, lows = self._fetch_candles(instrument, "D", 3)
        if len(closes) < 2:
            return None, None, None, None, None

        ph = highs[-2]; pl = lows[-2]; pc = closes[-2]
        pivot = (ph + pl + pc) / 3
        bc    = (ph + pl) / 2
        tc    = (pivot - bc) + pivot
        dr    = ph - pl

        # v1.1.1 — TC < BC when the prior day closed below its range midpoint
        # (bearish session: PDC < (PDH+PDL)/2). In that case tc and bc are
        # mathematically correct values but the labels are inverted. Swap them
        # so TC is always the top of the CPR band. The pivot is unaffected.
        # This ensures _validate_cpr_levels always passes for real market data,
        # and the CPR bias filter (which only uses `pivot`) is never skipped.
        if tc < bc:
            tc, bc = bc, tc
            log.debug("CPR TC/BC swapped — bearish prior-day close (PDC=%.2f < mid=%.2f)", pc, (ph+pl)/2)

        lv = {
            "pivot":         round(pivot, 2),
            "tc":            round(tc, 2),
            "bc":            round(bc, 2),
            "r1":            round((2 * pivot) - pl, 2),
            "r2":            round(pivot + dr, 2),
            "s1":            round((2 * pivot) - ph, 2),
            "s2":            round(pivot - dr, 2),
            "pdh":           round(ph, 2),
            "pdl":           round(pl, 2),
            "cpr_width_pct": round(abs(tc - bc) / pivot * 100, 3),
        }

        # Sanity check — should always pass after the swap above.
        # If it fails something unexpected happened with the candle data.
        ok, reason = _validate_cpr_levels(lv)
        if not ok:
            log.warning("CPR validation failed after swap — skipping CPR bias | %s | "
                        "PDH=%.2f PDL=%.2f PDC=%.2f → pivot=%.2f TC=%.2f BC=%.2f",
                        reason, ph, pl, pc, pivot, tc, bc)
            return None, None, None, None, None

        log.info("CPR fetched | pivot=%.2f TC=%.2f BC=%.2f width=%.3f%%",
                 pivot, tc, bc, lv["cpr_width_pct"])
        return lv, lv["pivot"], lv["tc"], lv["bc"], lv["cpr_width_pct"]

    # -- ORB helper -----------------------------------------------------------

    def _get_active_session(self, now_sgt: _dt):
        h = now_sgt.hour
        if 16 <= h <= 20:   return "London"
        if h >= 21 or h == 0: return "US"
        return None

    def _get_orb(self, session_name, instrument: str, now_sgt: _dt):
        """Return (orb_high, orb_low, formed) for the current session ORB.

        Cache key uses the SESSION-OPEN date, not the calendar date.
        This fixes the US midnight continuation (00:00-00:59 SGT):
        at 00:05 SGT Mar 20 the session opened Mar 19, so the key is
        '2025-03-19_US' -- matching the entry cached at 21:xx Mar 19.
        """
        if session_name not in ORB_SESSIONS:
            return None, None, False

        import datetime as _dt_mod
        open_h, open_m = ORB_SESSIONS[session_name]
        open_sgt = now_sgt.replace(hour=open_h, minute=open_m, second=0, microsecond=0)

        # US midnight continuation (00:00-00:59): session opened yesterday
        if now_sgt.hour == 0 and session_name == "US":
            open_sgt = open_sgt - _dt_mod.timedelta(days=1)

        # Key by session-open date so midnight lookups match the cached entry
        session_date_str = open_sgt.strftime("%Y-%m-%d")
        cache_key = session_date_str + "_" + session_name
        orb_cache = load_json(_ORB_CACHE_FILE, {})

        if cache_key in orb_cache and orb_cache[cache_key].get("formed"):
            c = orb_cache[cache_key]
            return c["high"], c["low"], True

        minutes_since_open = (now_sgt - open_sgt).total_seconds() / 60
        _orb_form_min = int(load_settings().get("orb_formation_minutes", 15))

        if minutes_since_open < _orb_form_min:
            log.debug("ORB not yet formed for %s (%.0f min since open, need %d)", session_name, minutes_since_open, _orb_form_min)
            return None, None, False

        open_utc = open_sgt.astimezone(_UTC)
        closes, highs, lows, times = self._fetch_candles_with_time(instrument, "M15", 12)

        for i, t in enumerate(times):
            try:
                candle_dt = _dt.fromisoformat(t.replace("Z", "+00:00")).replace(tzinfo=_UTC)
            except Exception:
                continue
            if candle_dt >= open_utc:
                orb_cache[cache_key] = {
                    "high": round(highs[i], 2),
                    "low":  round(lows[i], 2),
                    "formed": True,
                    "candle_time": t,
                }
                save_json(_ORB_CACHE_FILE, orb_cache)
                log.info("ORB formed | %s high=%.2f low=%.2f candle=%s",
                         session_name, highs[i], lows[i], t)
                return highs[i], lows[i], True

        return None, None, False

    # -- EMA helper -----------------------------------------------------------

    def _ema_series(self, closes: list, period: int) -> list:
        """Return full EMA series for given closes and period."""
        if len(closes) < period:
            return []
        k   = 2.0 / (period + 1)
        ema = sum(closes[:period]) / period
        series = [ema]
        for price in closes[period:]:
            ema = price * k + ema * (1 - k)
            series.append(ema)
        return series

    # -- Data helpers ---------------------------------------------------------

    def _fetch_candles(self, instrument: str, granularity: str, count: int = 60):
        url    = "{}/v3/instruments/{}/candles".format(self.base_url, instrument)
        params = {"count": str(count), "granularity": granularity, "price": "M"}
        for attempt in range(3):
            try:
                r = self.session.get(url, headers=self.headers, params=params, timeout=15)
                if r.status_code == 200:
                    candles  = r.json().get("candles", [])
                    complete = [c for c in candles if c.get("complete")]
                    return (
                        [float(c["mid"]["c"]) for c in complete],
                        [float(c["mid"]["h"]) for c in complete],
                        [float(c["mid"]["l"]) for c in complete],
                    )
                log.warning("Fetch candles %s %s: HTTP %s", instrument, granularity, r.status_code)
            except Exception as e:
                log.warning("Fetch candles error (%s %s) attempt %s: %s",
                            instrument, granularity, attempt + 1, e)
            time.sleep(1)
        return [], [], []

    def _fetch_candles_with_time(self, instrument: str, granularity: str, count: int = 12):
        url    = "{}/v3/instruments/{}/candles".format(self.base_url, instrument)
        params = {"count": str(count), "granularity": granularity, "price": "M"}
        for attempt in range(3):
            try:
                r = self.session.get(url, headers=self.headers, params=params, timeout=15)
                if r.status_code == 200:
                    candles  = r.json().get("candles", [])
                    complete = [c for c in candles if c.get("complete")]
                    return (
                        [float(c["mid"]["c"]) for c in complete],
                        [float(c["mid"]["h"]) for c in complete],
                        [float(c["mid"]["l"]) for c in complete],
                        [c["time"] for c in complete],
                    )
                log.warning("Fetch candles+time %s %s: HTTP %s", instrument, granularity, r.status_code)
            except Exception as e:
                log.warning("Fetch candles+time error (%s %s) attempt %s: %s",
                            instrument, granularity, attempt + 1, e)
            time.sleep(1)
        return [], [], [], []

    def _atr(self, highs: list, lows: list, closes: list, period: int = 14):
        n = len(closes)
        if n < period + 2 or len(highs) < n or len(lows) < n:
            return None
        trs = [
            max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
            for i in range(1, n)
        ]
        atr = sum(trs[:period]) / period
        for tr in trs[period:]:
            atr = (atr * (period - 1) + tr) / period
        return atr
