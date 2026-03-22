# Project: SPX 0DTE IBF System — Master Reference

---

## Polygon API
- **API Key:** Set via `POLYGON_API_KEY` env var (Railway/cloud) or `cockpit_config.json` locally
- **Plan:** Top-tier paid plan. Always assume full access to all endpoints, tick-level data, unlimited calls, and options data. Never throttle, downsample, or limit requests based on free-tier assumptions.
- **SPX index ticker:** `I:SPX` on aggs endpoints
- **SPXW options format:** `O:SPXW{YYMMDD}{C/P}{strike*1000:08d}`

---

## Core Rules

### Never fabricate data
Never generate synthetic, placeholder, or simulated data to stand in for real market data — not even temporarily, not even as a fallback, not even "until the real data loads." This includes seeded random numbers, normal distributions, dummy P&L values, fake price series, or any other invented numbers presented as if they reflect reality. If real data is unavailable (API error, missing contract, rate limit), surface that failure clearly rather than silently filling in made-up values. The user must always be able to trust that every number on screen came from a real data source.

### Never use theoretical pricing models as a substitute for real data
Do not use Black-Scholes, binomial models, Greeks-based estimation, or any other theoretical pricing model to generate option prices, P&L, or trade outcomes when real market data (actual prints, OHLC bars, trades) is available or obtainable. Theoretical models are acceptable only for supplementary analysis (e.g., estimating Greeks for context) — never as the source of truth for P&L, entry/exit prices, or backtest results.

### Be thorough — never cut corners
Always prioritize completeness and precision in data analysis and collection. Never skip steps, truncate datasets, use approximations, or reduce granularity to save compute, tokens, API calls, or time. If a task requires processing every row, fetching every contract, or checking every date — do exactly that. Do not summarize when the user expects exhaustive output. Do not sample when the user expects the full population.

### Never silently accept missing data when it can be obtained
If a backtest, scan, or analysis identifies dates/contracts/signals that should be priced or evaluated but the required data is not in the local cache, do not silently skip those dates and present partial results as if they are complete. Instead: (1) quantify exactly how many dates/contracts are missing, (2) surface this gap to the user immediately, and (3) offer to fetch the missing data from the API before proceeding. Partial results are acceptable only if the user explicitly chooses to proceed without the missing data. Never present a backtest with more than 5% of signal days missing as a finished product.

### Surface problems, don't hide them
If something looks wrong — P&L doesn't match, data is missing, a calculation contradicts expectations — flag it immediately. Never silently "fix" discrepancies by smoothing over them, and never present results that paper over known issues. The user would rather see an ugly truth than a polished lie.

### Scripts meant for local execution must write output to a shared file
When generating a script that the user will run outside the sandbox (e.g., because outbound API calls are blocked), always write all results, logs, and summaries to a file inside the project directory. Never rely on the user to copy-paste terminal output back. The script's output file should contain everything needed to continue analysis.

### Forward-walking signal detection only
Never use hindsight or peak detection for entry/exit timing. All signal logic must match what is observable at the moment of entry in real time. All backtest logic must match live strategy logic exactly.

---

## System Architecture

### Deployment
- **Platform:** Railway — auto-deploys from GitHub pushes to `main`
- **Main server:** `cockpit_feed.py` — runs as a `SimpleHTTPRequestHandler` on port `$PORT`
- **File serving:** Any committed file is accessible at its path (e.g., `/strategy_calendar.html`, `/cockpit_state.json`)
- **Live URL:** `https://web-production-fb57f.up.railway.app/`

### Key Files
| File | Purpose |
|------|---------|
| `cockpit_feed.py` | Main server — polls Polygon, serves API endpoints, writes `cockpit_state.json` |
| `strategy_calendar.py` | Generates `strategy_calendar.html` (static shell only — no baked-in data) |
| `strategy_calendar.html` | Dynamic calendar UI — fetches all data client-side via `/api/calendar` |
| `compute_stats.py` | Reads `research_all_trades.csv` + `spx_gap_cache.json` → writes `strategy_trades.json` + `strategy_stats.json` |
| `strategy_trades.json` | All individual trades (518 as of 2026-03-05), one record per trade |
| `strategy_stats.json` | Aggregated per-strategy stats (win rate, P&L, drawdown, etc.) |
| `research_all_trades.csv` | Master backtest data source for V3–V14. Last date: **2026-03-05** |
| `spx_gap_cache.json` | SPX overnight gap % by date — required by `compute_stats.py`. Must exist on Railway |
| `real_fills.json` | Real broker fills for N17/N18 keyed by `{"N17": {"2026-01-15": {...}}, "N18": {...}}` |
| `vix9d_daily.json` | VIX9D index values by date — required for N15 term structure filter |
| `pull_real_fills.py` | Fetches N17/N18 real fills from broker and appends to `real_fills.json` |
| `cockpit_config.json` | Local config (Polygon API key, poll interval) — not used on Railway |
| `cockpit_state.json` | Live cockpit state written every poll cycle |

### API Endpoints in cockpit_feed.py
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/calendar` | GET | Returns `{"trades": [...], "stats": {...}}` from the two JSON files |
| `/api/refresh_stats` | POST | Runs `compute_stats.py` server-side; returns `{"ok": true}` or error |
| `/cockpit_state.json` | GET (file) | Live cockpit state (SPX price, VIX, signals, etc.) |

### Calendar Architecture (Dynamic Fetch)
`strategy_calendar.html` is a **static shell** — no data baked in. On every page load:
1. JS fetches `/api/calendar`
2. `renderStats(data.stats)` populates the stats table
3. `renderCalendar(data.trades)` builds the month/day grid
4. The **↻ Refresh Data** button calls `POST /api/refresh_stats`, then re-fetches `/api/calendar` and re-renders in place

### EOD Auto-Refresh (Daily at 5:30 PM ET on Weekdays)
In `cockpit_feed.py`'s poll loop: at 17:30 ET, launches a background thread that runs:
1. `pull_real_fills.py` (fetches N17/N18 new fills)
2. `compute_stats.py` (regenerates both JSON files)

Controlled by `_eod_refresh_done` global to prevent double-triggering within the same day.

---

## Strategy Definitions

All strategies are SPX 0DTE iron butterflies or spreads using the opening print (or afternoon) regime. Entry is at a fixed time; exit is the first of: profit target hit, time stop, or wing stop.

### P&L Mechanics
- **Bear call spread:** `(credit_received - debit_to_close) * contracts * 100`
- **Single-leg long:** `(exit_price - entry_price) * contracts * 100`
- **Single-leg short:** `-(exit_price - entry_price) * contracts * 100`
- **Slippage assumption:** $1/spread (in `compute_stats.py`: `dollar_pnl = qty * (pnl_per_spread * 100 - 100)`)
- **Stop loss:** Per-strategy (see Stop Rules table below). NOT uniform across strategies.

### Exit Types
| Code | Meaning |
|------|---------|
| `TARGET` | Profit target hit (e.g., 50% of credit) |
| `TIME` | Time stop reached (e.g., 15:30) |
| `WING_STOP` | Price-based wing stop — SPX breaches long wing strike |
| `LOSS_STOP_70%` | P&L-based stop — position P&L < -70% of max risk per spread |
| `LOSS_STOP_50%` | P&L-based stop — position P&L < -50% of max risk per spread |
| `CLOSE` | Held to market close (16:15 ET) |

### Adaptive Wing Width Formula
Mirrors backtest exactly — must never diverge:
```
daily_sigma = SPX_price * (VIX / 100) / sqrt(252)
raw_wing = daily_sigma * 0.75
wing_width = max(40, round(raw_wing / 5) * 5)
```

### Strategy Table

| Version | Display Name | Short | Color | Entry | Mech | VIX | Prior Day | Range | Gap | Filter | Risk Budget |
|---------|-------------|-------|-------|-------|------|-----|-----------|-------|-----|--------|-------------|
| V3 | PHOENIX | PHX | #f59e0b | 10:00 | 50%/close/1T | any | any | any | any | 5-signal confluence (fire count ≥1) | Tiered $25K–$100K (VP-scaled) |
| N15 | PHOENIX CLEAR | CLR | #22c55e | 10:00 | 50%/close/1T | any | any | any | any | V3 signals + VIX9D/VIX < 1.0 | Same as V3 tiered |
| V6 | QUIET REBOUND | QRB | #06b6d4 | 10:00 | 50%/1530/1T | <15 | DOWN | IN | GFL | VP ≤ 1.7 | $75K (VP-scaled) |
| V7 | FLAT-GAP FADE | FGF | #a855f7 | 10:00 | 40%/close/1T | <15 | FLAT | IN | GUP | none | $25K |
| V9 | BREAKOUT STALL | BKT | #eab308 | 10:00 | 70%/1545/1T | 15–20 | UP | OT | GFL | RV slope ≠ RISING; VP ≤ 2.0 | $100K (VP-scaled) |
| V12 | BULL SQUEEZE | BSQ | #f97316 | 10:00 | 40%/close/1T | <15 | UP | OT | GUP | 5dRet > 1% | $75K |
| N17 | AFTERNOON LOCK | AFT | #7c3aed | 13:00 | 50%/close/1T | any | any | any | any | VVIX < 100 | $50K fixed |
| N18 | LATE SQUEEZE | LSQ | #0ea5e9 | 14:00 | 50%/close/1T | any | any | any | any | 5dRet>1% + VP<2 + Prior≠FLAT | $50K fixed |

**Removed strategies:** V8 (STRESS SNAP), V10 (BREAKDOWN PAUSE), V14 (ORDERLY DIP) — all flipped to unprofitable when re-backtested at correct entry times with 5-min resolution.

**Gap classification:** GUP = gap > +0.25%, GDN = gap < -0.25%, GFL = otherwise
**Range classification:** IN = SPX within prior week's high/low, OT = outside

### Per-Strategy Stop Rules (Optimized via 776-day backtest)
Each strategy uses the stop configuration that maximized return-to-drawdown ratio on the full 2023-2026 dataset:

| Strategy | Stop Type | Description |
|----------|-----------|-------------|
| V3 PHOENIX | wing + loss_stop(70%) | Both price-based wing breach AND P&L < -70% max risk |
| N15 PHOENIX CLEAR | loss_stop(50%) | P&L < -50% max risk (tighter stop, better for N15's subset profile) |
| V6 QUIET REBOUND | none | Time stop at 15:30 is sole protection (stops only hurt on this strategy) |
| V7 FLAT-GAP FADE | wing_stop | Price-based wing breach only (safety net; n=5, stops never fired) |
| V9 BREAKOUT STALL | loss_stop(70%) | P&L < -70% max risk (time stop at 15:45 is primary defense) |
| V12 BULL SQUEEZE | loss_stop(50%) | P&L < -50% max risk (caps tail losses to $20K vs $35K) |
| N17 AFTERNOON LOCK | — | Real broker fills; stop rules applied at execution |
| N18 LATE SQUEEZE | — | Real broker fills; stop rules applied at execution |
| PHX 75 Power Close | loss_stop(70%) | Insurance only — 15 min hold, rarely fires |
| PHX 75 Last Hour | loss_stop(50%) | Marginal improvement; caps worst-case losses |
| Firebird 60 Last Hour | loss_stop(50%) | Marginal improvement; caps worst-case losses |
| PHX 75 Afternoon | loss_stop(50%) | Catches 4 additional stop exits over 776 days |
| Ironclad 35 Condor | wing_stop | Safety net; 94% hit target, stops never fire |
| Firebird 60 Final Bell | wing_stop | Safety net; 15-min hold, stops never fire |
| PHX 75 Early Afternoon | loss_stop(50%) | Better risk profile (max loss $30K vs $48K) |
| PHX 75 Midday | loss_stop(50%) | $506K vs $475K wing-only; catches 13 stops |
| Firebird 60 Midday | loss_stop(70%) | $557K vs $524K wing-only; 60pt wing benefits from wider stop |
| Morning Decel Scalp | none | 11:30 time stop does all the work; stops barely trigger |

### PHOENIX Fire Count (V3/N15 Signal Confluence)
Five binary signals — fire count = sum of True signals. Any fire count ≥ 1 qualifies:
- G1: VIX ≤ 20 AND VP ≤ 1.0 AND 5dRet > 0
- G2: VP ≤ 1.3 AND prior day DOWN AND 5dRet > 0
- G3: VP ≤ 1.2 AND 5dRet > 0 AND RV_1d_change > 0
- G4: VP ≤ 1.5 AND outside prior week range AND 5dRet > 0
- G5: VP ≤ 1.3 AND RV slope ≠ RISING AND 5dRet > 0

### PHOENIX Tiered Risk Budget (V3/N15)
| Fire Count | Risk Budget |
|-----------|-------------|
| 0 | $0 (no trade) |
| 1 | $25,000 |
| 2 | $50,000 |
| 3 | $75,000 |
| 4–5 | $100,000 |

### VP-Scaled Budget (Regime Strategies V6/V9/V10/V12/V14)
Within the strategy's max budget, VP ratio scales the actual deployment:
- VP ≤ 1.0 → 100% of max
- VP 1.0–1.2 → 75%
- VP 1.2–1.5 → 50%
- VP > 1.5 → 25%

### N17/N18 Data Source
N17 and N18 use **real broker fills** from `real_fills.json`, not backtested prices from `research_all_trades.csv`. Their P&L is exact — no slippage model needed. The EOD refresh calls `pull_real_fills.py` to append new fills automatically.

---

## Data Pipeline

### V3–V12 (Backtested Legacy Strategies — V8/V10/V14 removed)
**Source:** `research_all_trades.csv` (historical, 570 rows ending 2026-03-05) + `refresh_legacy_strategies.py` (incremental, for new dates).

**Incremental update pipeline:** `refresh_legacy_strategies.py` pulls Polygon data for new trading days, computes regime signals from intraday bars, evaluates strategy filters, and simulates trades via the research engine at 5-min option bar resolution. Uses CSV-sourced `in_prior_week_range` for dates the CSV covers; computes from SPX bars for new dates.

**Legacy regeneration (deprecated):** `compute_stats.py` reads the CSV + `spx_gap_cache.json`. This is kept for backward compatibility but the research engine pipeline is now the primary source of truth.

### N17/N18 (Real-Fill Strategies)
**Source:** `real_fills.json` — populated by `pull_real_fills.py` which calls the broker API.
**Update:** Handled automatically by the EOD refresh at 5:30 PM ET. Can also be triggered manually via the Refresh Data button in the calendar.

### Required Cache Files (Must Exist on Railway)
- `spx_gap_cache.json` — overnight gap % by date. Without this, `compute_stats.py` crashes. Must be committed to the repo.
- `vix9d_daily.json` — VIX9D daily values. Without this, all N15 trades are skipped.
- `real_fills.json` — N17/N18 broker fills. Without this, N17/N18 show zero trades.

---

## Statistical Standards

### Correct Significance Test
**Use t-test on dollar P&L** — not binomial test on win rate. The binomial test (z-test on win rate) ignores the magnitude of wins and losses, making it inappropriate for trading strategies where tail outcomes dominate. The t-test on mean dollar P&L (H0: mean = 0) is the correct test.

### Known Results (as of 2026-03-05 data)
- V3 (PHOENIX): n=113, p≈0.0086 from t-test on dollar P&L
- Bonferroni correction for 8 legacy strategies (V8/V10/V14 removed): threshold = 0.05/8 ≈ **0.00625**
- V3's p=0.0086 does **NOT** survive Bonferroni correction — this is a known limitation to flag, not hide

### Distribution Considerations
V3 has fat left tails (WING_STOP events). The normal-assumption t-test is approximate. A bootstrap or permutation test is preferred for rigorous significance testing. Never claim stronger statistical evidence than the data supports.

### Sample Size Awareness
- V7 (n=5), V12 (n=10): too few trades to draw reliable conclusions. Results are directional only.
- Never present small-n results as statistically significant without explicit caveats.
- Never apply Bonferroni correction post-hoc to only the strategies that look good — apply it to all 8.

### Bull Market Bias
The backtest period (roughly 2023–2025) is predominantly a bull market with compressed VIX. Regime strategies that require low VIX or upward prior-day returns may be over-represented. Always flag this when presenting results.

---

## Known Issues and Structural Concerns

### V3/N15 Double-Counting Risk
N15 (PHOENIX CLEAR) is a **strict subset** of V3 (PHOENIX) — same entry mechanics, same fire-count logic, just with an additional VIX9D/VIX < 1.0 filter. Approximately 93 of V3's 113 trades also qualify as N15 (82% overlap). Running both simultaneously at full size = **2× PHOENIX exposure** on the majority of trading days. This is not diversification — it is concentration. N15 should logically function as a sizing modifier within V3, not a separate additive strategy. This structural issue has been identified but not yet resolved in the live system.

### Missing Data: 2026-03-06 to Present
`research_all_trades.csv` ends 2026-03-05. V3–V14 show no trades after that date. The fix requires building a daily data-pull pipeline (Polygon bars + option prices → CSV append). N17/N18 are kept current automatically via `pull_real_fills.py`.

### compute_stats.py Railway Dependency
`compute_stats.py` requires `spx_gap_cache.json` at startup. If this file is missing on Railway, the Refresh Data button will fail with `FileNotFoundError`. Ensure this file is always committed to the repo and up to date.

---

## Chart Implementation (LightweightCharts v4)

The day-modal chart in `strategy_calendar.html` uses **LightweightCharts v4**. Key implementation decisions:

- **Entry/exit markers:** Use `series.setMarkers([{shape: 'circle', ...}])` at the exact entry and exit bars. Color-code by strategy using the color from the STRATEGIES list.
- **No `createPriceLine` for ATM/strike levels** — this was tried and rejected. It created a wall of horizontal labels on the right axis that cluttered the chart. Circle markers at execution bars only is the correct approach.
- **Two chart panes:** SPX candlestick (top) + strategy P&L intraday (bottom, if available)

---

## Cockpit Feed Conventions

- `cockpit_feed.py` is the single process running on Railway. It serves HTTP AND runs the polling loop in the same process.
- All JSON responses use `self.send_json(status_code, dict)` helper
- The `poll()` loop runs every `POLL_SEC` seconds (default 10). The EOD trigger fires inside `poll()` — it checks `et_now.hour == 17 and et_now.minute >= 30` on weekdays.
- `_eod_refresh_done` is a module-level global (not per-request) — set to `today_str` after the first EOD trigger fires, preventing double runs.
- All file paths use `os.path.join(_DIR, filename)` where `_DIR = os.path.dirname(os.path.abspath(__file__))`.

---

## Display and UI Conventions

- **Strategy names on calendar tags:** 3-character abbreviations (PHX, CLR, QRB, FGF, SNP, BKT, BKD, BSQ, DIP, AFT, LSQ)
- **Month order:** Newest month first (reverse chronological)
- **Stats table:** Shows friendly display names (PHOENIX, QUIET REBOUND, etc.), not version codes (v3, v6, etc.)
- **Time format:** 12-hour format with AM/PM for all displayed timestamps
- **Win color:** green (`#22c55e`), Loss color: red (`#ef4444`), neutral: amber (`#f59e0b`)
- **Dark theme:** Background `#0d0d14`, cards `#1a1a24`, borders `#333`
