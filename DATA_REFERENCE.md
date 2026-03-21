# SPX 0DTE Options Data — Structure Reference

## Overview

This dataset contains comprehensive SPX 0DTE option chain data pulled from Polygon.io's top-tier API. It is designed for researching intraday vol-selling strategies (iron butterflies, iron condors, credit spreads) with no survivorship bias.

**Coverage:** 315+ trading days (2023-11-06 to 2025-02-07, still expanding)
**Disk:** ~194MB
**Pull script:** `pull_comprehensive_data.py` (resumable, rate-limited to 80 req/sec)

---

## Core Principles (from CLAUDE.md)

These rules govern all analysis done on this data:

### Never fabricate data
Never generate synthetic, placeholder, or simulated data to stand in for real market data. If real data is unavailable, surface that failure clearly rather than silently filling in made-up values. Every number must come from a real data source.

### Never use theoretical pricing models as a substitute for real data
Do not use Black-Scholes, binomial models, Greeks-based estimation, or any other theoretical pricing model to generate option prices, P&L, or trade outcomes when real market data is available. All option prices in this dataset are real Polygon prints — use them, not models.

### Forward-walking signal detection only
Never use hindsight or peak detection for entry/exit timing. All signal logic must match what is observable at the moment of entry in real time. At any entry decision at time T on date D, you can use:
- All daily data from D-1 and earlier (fully settled)
- All intraday data from D, but only bars at or before time T
- Nothing from after time T on day D

### Be thorough — never cut corners
Process every row, every contract, every date. Do not sample when the full population is available.

### Never silently accept missing data
If data is missing, quantify the gap, surface it, and offer to fetch it. Never present partial results as complete.

### Surface problems, don't hide them
Flag discrepancies immediately. Never paper over known issues.

### Statistical standards
- Use t-test on dollar P&L (not binomial test on win rate)
- Apply Bonferroni or Holm-Bonferroni correction across ALL tests
- Never present small-n results (n < 30) as statistically significant without explicit caveats

---

## File Structure

```
data/
  spx_1min/YYYY-MM-DD.json         315 files  — SPX 1-minute OHLC bars
  option_chains/YYYY-MM-DD.json     315 files  — Full 0DTE option chain (5-min bars)
  vix_1min/YYYY-MM-DD.json          315 files  — VIX 1-minute OHLC bars
  quotes/YYYY-MM-DD.json            315 files  — Bid/ask snapshots at 20 time windows
  spx_daily.json                    1 file     — SPX daily OHLC (776 dates, back to 2023-02)
  spx_weekly.json                   1 file     — SPX weekly OHLC (162 weeks)
  vix_daily.json                    1 file     — VIX daily OHLC (797 dates)
  daily_context.json                1 file     — Pre-computed regime signals (62 fields/day)
  pull_log.json                     1 file     — Progress tracker
```

---

## 1. SPX 1-Minute Bars — `data/spx_1min/{YYYY-MM-DD}.json`

One file per trading day. ~390 bars per day covering 09:30–16:01 ET.

```json
{
  "09:30": {"o": 5297.15, "h": 5298.00, "l": 5295.50, "c": 5296.28},
  "09:31": {"o": 5296.28, "h": 5297.10, "l": 5293.00, "c": 5294.50},
  ...
  "16:01": {"o": 5283.10, "h": 5283.40, "l": 5283.00, "c": 5283.40}
}
```

Fields: `o` (open), `h` (high), `l` (low), `c` (close). Some bars include `v` (volume).

---

## 2. Option Chains — `data/option_chains/{YYYY-MM-DD}.json`

One file per trading day. Contains 5-minute OHLCV bars for ALL SPXW 0DTE option contracts within ±200 points of ATM at 5-point strike spacing. 81 strikes × 2 sides (call + put) = 162 contracts per day.

**CRITICAL:** Strikes are ABSOLUTE, not re-centered per time slot. If SPX opens at 5295 (ATM = 5295), the strike grid is fixed at 5095–5495 for the entire day. This means you can always mark-to-market a position entered at any strike, at any later time, regardless of where SPX has moved. No survivorship bias.

```json
{
  "date": "2024-06-03",
  "spx_at_open": 5296.28,
  "atm": 5295,
  "strike_range": [5095, 5495],
  "strikes": {
    "5095": {
      "C": {
        "09:30": {"o": 201.5, "h": 202.0, "l": 200.0, "c": 201.0, "v": 50, "vw": 201.2, "n": 12},
        "09:35": {...},
        ...
        "15:55": {...}
      },
      "P": {
        "09:30": {"o": 0.05, ...},
        ...
      }
    },
    "5100": {...},
    ...
    "5495": {...}
  }
}
```

**Per-strike bar fields:** `o` (open), `h` (high), `l` (low), `c` (close), `v` (volume), `vw` (VWAP), `n` (number of trades). Bars are keyed by "HH:MM" at 5-minute intervals.

**Strike coverage per time slot:** ~78 bars per contract per day (09:30–15:55), though far OTM contracts may have fewer bars (low liquidity).

**Using this data to price structures:**

For an iron butterfly at ATM with W-point wings:
```
credit = strikes[ATM]["C"][time]["c"] + strikes[ATM]["P"][time]["c"]
       - strikes[ATM+W]["C"][time]["c"] - strikes[ATM-W]["P"][time]["c"]
```

To mark-to-market at a later time, use the same absolute strike keys — they don't change.

---

## 3. VIX 1-Minute Bars — `data/vix_1min/{YYYY-MM-DD}.json`

Same format as SPX 1-min. VIX trades longer hours so bars may start at 03:30 and run through 16:14.

```json
{
  "03:30": {"o": 13.26, "h": 13.27, "l": 13.26, "c": 13.26},
  ...
  "16:14": {"o": 13.10, "h": 13.11, "l": 13.10, "c": 13.11}
}
```

---

## 4. Bid/Ask Quotes — `data/quotes/{YYYY-MM-DD}.json`

Real bid/ask snapshots at 20 specific time windows per day. Covers key strikes around ATM (±80 points). Use for realistic execution cost modeling.

```json
{
  "date": "2024-06-03",
  "atm": 5295,
  "times": {
    "09:31": {...},
    "10:00": {
      "5295": {
        "C": {"bid": 6.1, "ask": 6.7, "mid": 6.4, "spread": 0.6},
        "P": {"bid": 13.1, "ask": 14.5, "mid": 13.8, "spread": 1.4}
      },
      "5285": {...},
      "5305": {...},
      ...
    },
    "13:00": {...},
    ...
    "15:55": {...}
  }
}
```

**Time windows:** 09:31, 09:35, 09:45, 10:00, 10:15, 10:30, 10:45, 11:00, 11:30, 12:00, 12:30, 13:00, 13:30, 14:00, 14:30, 15:00, 15:15, 15:30, 15:45, 15:55

---

## 5. SPX Daily Bars — `data/spx_daily.json`

776 daily bars from 2023-02-14 to 2026-03-19. Includes ~2 years of lookback before the intraday data window for computing ATR, trends, and other context signals.

```json
{
  "ticker": "I:SPX",
  "timespan": "day",
  "bars": {
    "2024-06-03": {"o": 5297.15, "h": 5302.11, "l": 5234.32, "c": 5283.40},
    ...
  }
}
```

---

## 6. SPX Weekly Bars — `data/spx_weekly.json`

162 weekly bars. Same format as daily.

---

## 7. VIX Daily Bars — `data/vix_daily.json`

797 daily bars. Same structure as SPX daily but with VIX OHLC values.

---

## 8. Daily Context — `data/daily_context.json`

Pre-computed regime signals for each trading day. **Every field is forward-walk safe** — computed ONLY from data settled before 9:30 AM on that day. This is what a trader would know sitting down before the open.

62 fields per day:

```json
{
  "2024-06-03": {
    "date": "2024-06-03",
    "today_open": 5297.15,

    // Prior day candle
    "prior_close": 5277.51,
    "prior_open": 5243.21,
    "prior_high": 5280.33,
    "prior_low": 5191.68,
    "prior_day_return": 0.8028,          // % close-to-close
    "prior_day_range": 88.65,            // high - low in points
    "prior_day_range_pct": 1.6798,       // range / close as %
    "prior_day_body_pct": 0.3869,        // |close-open| / range
    "prior_day_direction": "UP",         // UP / DOWN / FLAT
    "prior_day_upper_wick": 0.0318,      // wick / range ratio
    "prior_day_lower_wick": 0.5813,
    "prior_day_candle": "NORMAL",        // DOJI/HAMMER/SHOOTING_STAR/
                                         // ENGULF_BULL/ENGULF_BEAR/
                                         // MARUBOZU_BULL/MARUBOZU_BEAR/NORMAL
    "inside_day": false,

    // Gap analysis
    "gap_pct": 0.3721,                   // (today_open - prior_close) / prior_close * 100
    "gap_pts": 19.64,                    // today_open - prior_close in points
    "gap_direction": "GUP",              // GUP (>+0.25%) / GDN (<-0.25%) / GFL
    "gap_vs_prior_range": 0.2215,        // |gap| / prior_day_range (significance)
    "gap_into_new_5d_high": false,
    "gap_into_new_5d_low": false,
    "gap_into_new_20d_high": false,
    "gap_into_new_20d_low": false,

    // Streaks
    "consecutive_up_days": 1,
    "consecutive_down_days": 0,

    // ATR
    "atr_5": 51.13,
    "atr_10": 44.9,
    "atr_20": 42.11,

    // Distance from extremes
    "dist_from_5d_high_pct": -0.7276,
    "dist_from_5d_low_pct": 1.6263,
    "dist_from_20d_high_pct": -1.2197,
    "dist_from_20d_low_pct": 3.3404,

    // Multi-day returns
    "prior_2d_return": 0.8028,
    "prior_5d_return": -0.5129,
    "prior_10d_return": -0.4857,
    "prior_20d_return": 2.9198,

    // Weekly context
    "prior_week_high": 5375.08,
    "prior_week_low": 5234.32,
    "prior_week_close": 5346.99,
    "prior_week_range": 140.76,
    "prior_week_return": 1.3165,
    "prior_week_direction": "UP",        // UP / DOWN / FLAT
    "in_prior_week_range": true,         // today's open within last week's H/L
    "inside_week": false,                // prior week range inside week-before range
    "weekly_consecutive_up": 1,
    "weekly_consecutive_down": 0,
    "dist_from_weekly_high_pct": -1.4712,
    "dist_from_weekly_low_pct": 1.1861,

    // VIX context
    "vix_prior_close": 12.92,
    "vix_5d_avg": 13.39,
    "vix_10d_avg": 12.79,
    "vix_20d_avg": 12.84,
    "vix_1d_change": -1.55,
    "vix_5d_change": 0.99,
    "vix_percentile_60d": 25.0,          // where VIX sits in last 60 days
    "vix9d_vix_ratio": 1.0766,           // VIX9D/VIX (term structure)

    // Expiry calendar
    "expiry_type": "MONTH_START",        // QUAD_WITCH / MONTHLY_OPEX / PRE_OPEX /
                                         // POST_OPEX / OPEX_WEEK / MONTH_END /
                                         // MONTH_START / REGULAR
    "is_opex_day": false,
    "is_quad_witch": false,
    "is_opex_week": false,
    "days_to_next_opex": 18,
    "days_since_last_opex": 17
  }
}
```

---

## How to Price an Iron Butterfly

Given a date and entry time, to price an ATM iron butterfly with W-point wings:

1. Get SPX price: `spx_1min/{date}.json[time]["c"]`
2. Round to nearest 5 for ATM: `atm = round(spx / 5) * 5`
3. Look up option chain: `option_chains/{date}.json`
4. Get the 4 legs:
   - Short call: `strikes[atm]["C"][time]["c"]`
   - Short put: `strikes[atm]["P"][time]["c"]`
   - Long call: `strikes[atm+W]["C"][time]["c"]`
   - Long put: `strikes[atm-W]["P"][time]["c"]`
5. Credit = short_call + short_put - long_call - long_put
6. Max risk = W - credit

To mark-to-market at a later time T2, repeat step 4 using `[T2]` on the same absolute strikes. Because strikes are not re-centered, this works even if SPX has moved significantly.

---

## How to Detect a Wing Stop

Using SPX 1-min bars, check if SPX ever crosses a wing strike:

```python
for time, bar in sorted(spx_bars.items()):
    if time <= entry_time:
        continue
    if bar["h"] >= call_wing_strike or bar["l"] <= put_wing_strike:
        # Wing stop triggered at this time
        break
```

---

## Known Limitations

1. **Option bars are 5-min resolution.** Target/stop detection between 5-min marks uses SPX 1-min bars for wing stops but can only mark option P&L at 5-min boundaries.

2. **Far OTM contracts may have sparse bars.** Strikes 150+ points from ATM may have few or no trades in certain time periods. Check for `None` before using.

3. **No bars after 15:55 for most option contracts.** For settlement pricing (16:15), use intrinsic value: `call_value = max(0, SPX_close - strike)`, `put_value = max(0, strike - SPX_close)`.

4. **Daily context ends at the latest completed intraday date.** If intraday data is still being pulled, regenerate daily_context.json by running `compute_daily_context()` from `pull_comprehensive_data.py`.

5. **The data starts Nov 2023.** No 2022 bear market data. VIX range in this dataset is mostly 12-25. Strategies found here may not work in VIX 30+ environments.
