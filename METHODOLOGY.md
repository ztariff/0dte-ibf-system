# SPX 0DTE Iron Butterfly System — Complete Methodology

## Purpose of This Document

This document describes the full end-to-end process used to discover, validate, and productionize the SPX 0DTE iron butterfly trading system. It is written for an AI assistant (Claude or equivalent) that needs to understand how every piece fits together — from raw market data through strategy discovery, P&L simulation, factor analysis, sizing optimization, and live deployment. Every design decision is explained with its rationale.

---

## 1. Data Infrastructure

### 1.1 Primary Data Source: Polygon.io

All market data comes from Polygon.io's top-tier paid API. This is critical — the paid plan provides tick-level and bar-level data with no rate limits, full options data, and historical coverage back to 2023. The system assumes full API access at all times and never throttles, downsamples, or limits requests.

### 1.2 Data Types Collected

**SPX Index Prices**
- Ticker: `I:SPX` on the aggregates endpoint
- Granularities used: 1-minute bars (for intraday RV calculation, morning range), 5-minute bars (for trade simulation entry/exit timing), daily bars (for prior day stats, weekly range, multi-day returns)
- The 1-minute bars within a trading day typically number ~390 (9:30 AM to 4:00 PM ET)

**SPXW 0DTE Option Chains**
- Contract format: `O:SPXW{YYMMDD}{C/P}{strike*1000:08d}`
- Example: `O:SPXW250115C05850000` = SPXW Jan 15, 2025, Call, strike 5850.00
- For each trading day, the system pulls 5-minute OHLC bars for every available strike's puts and calls
- A single day's option chain data covers dozens of strikes × 2 (put/call) × ~78 five-minute bars = thousands of data points
- This data is stored per-day as JSON files in `data/option_chains/{YYYY-MM-DD}.json`
- Across ~776 trading days, this is hundreds of megabytes of raw option bar data

**VIX Daily Close**
- Used for regime classification (VIX buckets: LOW <15, MID 15-20, ELEVATED 20-25, HIGH 25+)
- Pulled from Polygon daily aggregates

**VIX9D Daily Values**
- Stored in `vix9d_daily.json`
- Used to compute term structure ratio: VIX9D / VIX
- Classification: <0.90 = INVERTED, >1.02 = CONTANGO, else FLAT
- This ratio captures whether short-term implied vol is above or below the VIX term structure baseline

### 1.3 Why Real Option Data, Not Models

This is a foundational design constraint: **every P&L number in the system comes from actual 5-minute option bar data from Polygon, never from Black-Scholes, binomial models, or Greeks-based estimation.**

Theoretical pricing models fail to capture real-world 0DTE dynamics:
- Bid-ask spread behavior changes dramatically in the last 2 hours of trading
- Gamma acceleration near expiry causes non-linear price moves that models underestimate
- Pin risk near round strikes creates clustering effects models don't predict
- Intraday implied vol shifts (vol-of-vol) are not captured by a static model
- Real market microstructure (order flow, market maker positioning) affects prices

By using actual traded bars from Polygon, the backtested P&L reflects what you would have received trading those contracts at those times. This is not a simulation of what a model says the price "should" have been — it is what the price actually was.

### 1.4 Data Integrity Rules

These rules are enforced throughout the system:

1. **Never fabricate data.** No synthetic, placeholder, or simulated data stands in for real market data — not temporarily, not as a fallback.
2. **Never silently accept missing data.** If a backtest identifies dates where option chain data is missing, those gaps are quantified and surfaced, not silently skipped.
3. **Surface problems, don't hide them.** If P&L doesn't match expectations or data looks wrong, flag it immediately rather than smoothing over it.

---

## 2. The Iron Butterfly Structure

### 2.1 What It Is

An iron butterfly is a four-leg options position:
- **Sell 1 ATM put** (at-the-money, nearest strike to current SPX)
- **Sell 1 ATM call** (same strike)
- **Buy 1 wing put** (ATM - wing_width)
- **Buy 1 wing call** (ATM + wing_width)

The short ATM straddle collects a large credit. The long wings cap the maximum loss at `wing_width - credit` per spread. On a 0DTE contract (expires same day), theta decay is extreme — the position loses time value by the hour. If SPX stays near the ATM strike through expiry, the entire credit is profit. The bet is: "SPX won't move more than [wing_width] points between entry and exit."

### 2.2 Adaptive Wing Width

The wing width is not fixed — it adapts to the current volatility environment:

```
daily_sigma = SPX_price * (VIX / 100) / sqrt(252)
raw_wing = daily_sigma * 0.75
wing_width = max(40, round(raw_wing / 5) * 5)
```

This means in a VIX=12 environment with SPX at 5800, the wing might be 50 points. In a VIX=25 environment, it might be 80 points. The adaptive width ensures the wings are always proportional to the expected daily move, giving the position room to breathe without making the wings so wide that the credit becomes tiny.

For the newer strategies (Phoenix 75, Firebird 60, Ironclad 35), the wing width is fixed at 75, 60, or 35 points respectively. These were discovered by scanning fixed wing widths at different entry times (see Section 5).

### 2.3 P&L Mechanics

```
P&L per spread = (entry_credit - exit_spread_value) * 100
Dollar P&L = P&L_per_spread * contracts * 100 - slippage
Slippage = $1 per spread (i.e., $100 per contract in dollar terms)
Max profit = entry_credit * 100 per spread (if spread value goes to 0)
Max loss = (wing_width - entry_credit) * 100 per spread (if SPX at wing)
```

---

## 3. Regime Classification

### 3.1 The Four Regime Axes

Every trading day is classified along four observable dimensions, all computed from data available before entry time:

**VIX Level** (from prior day's VIX close or current intraday VIX):
- LOW: VIX < 15
- MID: VIX 15-20
- ELEVATED: VIX 20-25
- HIGH: VIX 25+

**Prior Day Direction** (from daily bars):
- UP: prior day close > prior day open (positive return)
- DOWN: prior day close < prior day open (negative return)
- FLAT: absolute return < threshold

**Range Position** (from daily bars):
- IN: SPX current price is within the prior week's high-low range
- OT (outside): SPX is above the week high or below the week low

**Gap Classification** (from open vs prior close):
- GUP: gap > +0.25%
- GDN: gap < -0.25%
- GFL: absolute gap <= 0.25%

This produces a regime label like `LOW_DN_IN_GFL` — every trading day maps to exactly one regime.

### 3.2 Why These Four Dimensions

These aren't arbitrary. Each captures a distinct market dynamic relevant to butterfly profitability:

- **VIX level** directly determines implied vol premium. Low VIX means the market expects small moves — ideal for selling butterflies. High VIX means the market expects large moves — dangerous for short gamma.
- **Prior day direction** captures short-term momentum/mean-reversion. After a down day in a calm market, mean reversion (rally) tends to keep SPX near the open — good for butterflies. After a sharp up day, continuation moves can blow through wings.
- **Range position** captures whether we're in a trending or consolidating market. Inside the prior week's range = consolidation (butterfly-friendly). Outside = breakout/breakdown (butterfly-dangerous).
- **Gap classification** captures overnight sentiment. A flat gap means the overnight session didn't move the needle — continuation of prior session's dynamics. A large gap means new information arrived overnight that could drive follow-through.

### 3.3 Additional Factors (Not Part of Regime Label, Used as Filters)

- **VP Ratio** (Volatility Premium): VIX / Realized Vol. When VP > 1.5, implied vol is expensive relative to actual movement — the butterfly credit is "rich" and the edge is larger.
- **RV Slope**: Whether realized vol is rising, falling, or flat over recent sessions. Rising RV while selling gamma is dangerous.
- **5-Day Return**: Cumulative SPX return over the past 5 trading days. Captures medium-term momentum.
- **Score Vol**: A composite vol premium score combining VP and RV slope.

---

## 4. Strategy Discovery Process (Legacy Regime Strategies)

### 4.1 The Exhaustive Scan

We ran the trade simulation engine (see Section 6) on every trading day from January 2023 through March 2026 (~776 trading days) with **no regime filters** — just a plain iron butterfly at 10:00 AM every day with adaptive wing width.

This produced 776 trade records, each tagged with:
- The regime label for that day
- All individual regime factors (VIX, VP, RV, prior day direction, etc.)
- The full P&L outcome at 5-minute resolution
- Entry credit, exit spread value, exit type, exit time

### 4.2 Grouping by Regime

We grouped these 776 trades by regime label and computed per-group statistics:
- Count (sample size)
- Mean P&L per spread
- Median P&L per spread
- Standard deviation
- Win rate (% of trades with positive P&L)
- Max single-day loss
- Cumulative P&L (sum of all trades in that regime)
- Max drawdown within the cumulative curve

### 4.3 Cumulative P&L Curve Analysis

This is the critical step. For each regime with positive total P&L, we plotted the **cumulative dollar P&L over time** — trade by trade in chronological order. This curve reveals things that summary statistics hide:

- A regime with +$80K total but a $50K drawdown in the middle is unreliable
- A regime that made all its money in a 2-month window and was flat the rest is likely noise
- A regime with steady upward slope across the full 3-year period is a real edge

We selected regimes where:
1. Cumulative P&L was positive
2. The curve was generally upward-sloping across the full period (not concentrated in one window)
3. Max drawdown was acceptable relative to total P&L (return-to-drawdown ratio > 3)
4. Sample size was at least 10-15 trades (enough to be meaningful, though small-n regimes were flagged)

### 4.4 Strategies That Emerged

| Strategy | Regime Label | Economic Logic |
|----------|-------------|----------------|
| V6 QUIET REBOUND | LOW_DN_IN_GFL | After a down day in calm markets, inside range, flat gap → mean reversion keeps SPX pinned near open |
| V7 FLAT-GAP FADE | LOW_FL_IN_GUP | Flat prior day + gap up in calm market → gap tends to fade, SPX consolidates near ATM |
| V9 BREAKOUT STALL | MID_UP_OT_GFL | Prior up day pushed outside range, but flat gap says no follow-through → stall/consolidation |
| V12 BULL SQUEEZE | LOW_UP_OT_GUP | Strong bull momentum (up day + gap up + outside range) in low vol → squeeze continuation keeps SPX near entry |

### 4.5 Adding Secondary Filters

Within each selected regime, we sliced by secondary factors to find subsets with meaningfully different outcomes:

Process for each factor:
1. Split the regime's trades into buckets (e.g., VP ≤ 1.0, 1.0-1.3, 1.3-1.7, 1.7+)
2. Compute mean P&L and standard deviation for each bucket
3. Run a **Welch's t-test** comparing each bucket's mean P&L against the complement (all trades NOT in that bucket)
4. If a bucket has significantly worse P&L (p < 0.05), consider adding a filter that excludes it
5. Re-plot the cumulative P&L curve with the filter applied
6. Keep the filter only if the curve is smoother (smaller drawdowns) with similar or better total P&L

Examples:
- V6 got `VP <= 1.7` because trades with VP > 1.7 had significantly negative mean P&L
- V9 got `RV slope != RISING` because selling butterflies into accelerating vol destroyed returns
- V12 got `5dRet > 1%` because the bull squeeze setup requires recent upward momentum to work

### 4.6 The PHOENIX Model (V3)

V3 was discovered differently. Instead of a single regime label, we built a **signal confluence model** — 5 binary conditions that each independently predicted positive butterfly outcomes:

| Signal | Conditions |
|--------|-----------|
| G1 | VIX ≤ 20 AND VP ≤ 1.0 AND 5dRet > 0 |
| G2 | VP ≤ 1.3 AND prior day DOWN AND 5dRet > 0 |
| G3 | VP ≤ 1.2 AND 5dRet > 0 AND RV_1d_change > 0 |
| G4 | VP ≤ 1.5 AND outside prior week range AND 5dRet > 0 |
| G5 | VP ≤ 1.3 AND RV slope ≠ RISING AND 5dRet > 0 |

The "fire count" is how many of these 5 are true. Any fire count ≥ 1 qualifies for a trade. Sizing scales with conviction:
- 1 signal → $25K budget
- 2 signals → $50K
- 3 signals → $75K
- 4-5 signals → $100K
- 0 signals → no trade

PHOENIX fires on many different regime labels (it's more general than the single-regime strategies) but requires at least one confluence condition to be true. It ended up with the highest trade count (113 trades) and strongest statistical significance (p ≈ 0.0086 on dollar P&L t-test, though this does NOT survive Bonferroni correction for 8 strategies at threshold 0.05/8 ≈ 0.00625).

### 4.7 Strategy Elimination

V8 (STRESS SNAP), V10 (BREAKDOWN PAUSE), and V14 (ORDERLY DIP) were initially included based on early testing. When we re-ran the full backtest at correct **5-minute entry timing resolution** (earlier runs had used coarser data that masked the true entry price), all three flipped to unprofitable. The cumulative P&L curves went flat or negative. They were removed entirely — keeping strategies built on data artifacts would have been intellectually dishonest and financially dangerous.

---

## 5. New Strategy Discovery (Afternoon/Late Session)

### 5.1 Different Hypothesis

The legacy strategies (V3-V12) vary the **regime** (which days to trade) while keeping the entry time fixed at 10:00 AM. The new strategies flip this: they vary the **entry time and wing width** while trading every day (no regime filter).

The thesis: afternoon and late-session butterflies have a structural advantage because:
- Less time remaining means less opportunity for SPX to move beyond the wings
- Theta decay is at its most extreme in the final 1-2 hours (options lose time value fastest near expiry)
- The credit-to-wing-width ratio is more favorable (you collect more premium relative to risk for the remaining time)

### 5.2 The Parameter Sweep

We tested every combination of:

| Parameter | Values Tested |
|-----------|---------------|
| Entry time | 10:30, 13:45, 14:00, 14:30, 15:00, 15:15, 15:30 |
| Wing width | 35, 60, 75 points (fixed, not adaptive) |
| Profit target | 30%, 40%, 50% |
| Time stop | 11:30, 15:00, 15:15, 15:30, close |

Each combination was run through the full 5-minute resolution simulation engine on all 776 trading days. With ~7 entry times × 3 wing widths × 3 targets × 5 time stops = 315 combinations, each producing 776 trade P&Ls from real option data, this was a substantial compute effort.

### 5.3 Selection Criteria

We ranked all combinations by:
1. Return-to-max-drawdown ratio (primary)
2. Total cumulative P&L
3. Win rate > 50%
4. Cumulative curve shape (steady upward, not lumpy)

### 5.4 Natural Clusters

The top performers clustered into families:

| Family | Wing Width | Entry Window | Character |
|--------|-----------|-------------|-----------|
| Phoenix 75 | 75 pts | 13:45 - 15:15 | Wide wings, afternoon entries, captures large theta decay |
| Firebird 60 | 60 pts | 14:00 - 15:30 | Medium wings, slightly tighter, later entries |
| Ironclad 35 | 35 pts | 14:30 | Narrow iron condor variant, very high win rate |
| Morning Decel | adaptive | 10:30 | Morning scalp, tight target (30%), early time stop (11:30) |

### 5.5 The 10 Final Strategies

| Strategy | Entry | Wing | Target | Time Stop | Base Budget | Grade |
|----------|-------|------|--------|-----------|-------------|-------|
| Phoenix 75 Power Close | 15:15 | 75 | 50% | 15:30 | $150K | S |
| Phoenix 75 Last Hour | 15:00 | 75 | 50% | 15:30 | $100K | A |
| Firebird 60 Last Hour | 15:00 | 60 | 50% | 15:30 | $100K | A |
| Phoenix 75 Afternoon | 14:30 | 75 | 50% | 15:30 | $75K | B+ |
| Ironclad 35 Condor | 14:30 | 35 | 40% | 15:30 | $75K | B+ |
| Firebird 60 Final Bell | 15:30 | 60 | 50% | 15:30 | $75K | B+ |
| Phoenix 75 Early Afternoon | 13:45 | 75 | 50% | 15:30 | $50K | B |
| Phoenix 75 Midday | 14:00 | 75 | 50% | 15:30 | $35K | C+ |
| Firebird 60 Midday | 14:00 | 60 | 50% | 15:30 | $35K | C+ |
| Morning Decel Scalp | 10:30 | adaptive | 30% | 11:30 | $20K | C |

The grade reflects relative strength based on return-to-drawdown ratio and cumulative P&L. Base budget scales with grade — higher-confidence strategies get more capital.

---

## 6. The Trade Simulation Engine

### 6.1 Per-Day Processing

For a given date and strategy, the engine executes these steps:

**Step 1: Regime Classification at Entry Time**

Pull SPX 1-minute bars from market open through the strategy's entry time. Compute:
- VIX level (from prior day close or current intraday)
- Prior day direction (from daily bars: close vs open)
- Range position (from 5 daily bars: is current SPX within prior week's high/low?)
- Gap percentage (today's open vs yesterday's close)

This produces the regime label. All data used is observable at entry time — no lookahead.

**Step 2: Entry Pricing from Real Option Data**

At the strategy's entry time (e.g., 10:00 AM), look up the 5-minute option bar that contains that timestamp. For the iron butterfly, price four contracts:
- ATM put: `O:SPXW{date}P{atm_strike}` — use the bar's close price
- ATM call: `O:SPXW{date}C{atm_strike}` — use the bar's close price
- Wing put: `O:SPXW{date}P{atm - wing_width}` — use the bar's close price
- Wing call: `O:SPXW{date}C{atm + wing_width}` — use the bar's close price

Entry credit = ATM_put + ATM_call - wing_put - wing_call

These are real traded prices from Polygon's 5-minute bars. Not mid-quotes, not models.

**Step 3: Intraday P&L Tracking at 5-Minute Resolution**

From entry through the strategy's time stop (or market close), the engine steps through every 5-minute bar:

```
For each 5-minute timestamp from entry to exit_deadline:
    current_spread_value = ATM_put_bar.close + ATM_call_bar.close
                          - wing_put_bar.close - wing_call_bar.close
    current_pnl_per_spread = entry_credit - current_spread_value
    profit_pct = current_pnl_per_spread / entry_credit * 100
    loss_pct = -current_pnl_per_spread / (wing_width - entry_credit) * 100

    Check exit conditions in priority order:
    1. TARGET: profit_pct >= strategy's target (e.g., 50%)
    2. WING_STOP: SPX has crossed ATM ± wing_width
    3. LOSS_STOP: loss_pct >= strategy's loss stop threshold (e.g., 70%)
    4. TIME_STOP: current time >= strategy's time stop

    If any condition triggers → record exit at this bar's spread value
```

If no condition triggers through all bars, the position is marked as held to close (16:15 ET) and the final bar's spread value is the exit price.

**Step 4: P&L Recording**

The trade record captures:
- Date, strategy version, regime label
- Entry credit, exit spread value, exit type (TARGET/TIME/WING_STOP/LOSS_STOP/CLOSE)
- P&L per spread, dollar P&L (after slippage of $1/spread)
- All regime factors at entry time (VIX, VP, RV, prior day, gap, range, 5d return, etc.)
- Entry and exit timestamps
- Fire count (for PHOENIX strategies)

### 6.2 The Forward-Walking Constraint

Every decision in the simulation engine uses only data observable at the moment of evaluation. There is no lookahead:
- Regime classification uses data up to (not beyond) entry time
- Entry pricing uses the bar at entry time
- Exit checks use only the current bar's data
- No peak detection, no hindsight optimization

The backtest logic must exactly match what the live trading system does. If the backtest says "exit at 50% profit," the live system must use the same 50% threshold on the same spread value calculation.

---

## 7. Stop Rule Optimization

### 7.1 Process

For each strategy, we tested four stop configurations against the strategy's full trade history:
1. **No stop** — only the time stop protects
2. **Wing stop only** — exit if SPX crosses ATM ± wing_width
3. **Loss stop at 50%** — exit if P&L < -50% of max risk per spread
4. **Loss stop at 70%** — exit if P&L < -70% of max risk per spread
5. **Wing stop + loss stop** combinations

Each configuration was run through the full 5-minute simulation. We recorded:
- Total cumulative P&L
- Max drawdown (largest peak-to-trough decline in the cumulative curve)
- Return-to-drawdown ratio
- Number of stop-triggered exits
- P&L on stop-triggered days vs. non-triggered days

### 7.2 Selection Criterion

The stop configuration that maximized **return-to-max-drawdown ratio** was selected. This is deliberately not "maximum total P&L" — we prefer giving up some upside if it substantially reduces the worst drawdowns.

### 7.3 Results

| Strategy | Best Stop | Rationale |
|----------|-----------|-----------|
| V3 PHOENIX | wing + loss_stop(70%) | Needs both layers: wing breach catches SPX breakouts, 70% loss stop catches vol explosions |
| N15 PHOENIX CLEAR | loss_stop(50%) | Tighter stop works because N15's filtered subset has better base quality |
| V6 QUIET REBOUND | none | Time stop at 15:30 does all the work — adding stops hurt by cutting winners that temporarily dipped |
| V7 FLAT-GAP FADE | wing_stop | Safety net only (n=5, stops never actually fired in the backtest) |
| V9 BREAKOUT STALL | loss_stop(70%) | Time stop at 15:45 is primary defense, 70% loss stop catches the rare blowup |
| V12 BULL SQUEEZE | loss_stop(50%) | Caps tail losses from $35K to $20K on worst days |
| Phoenix 75 Power Close | loss_stop(70%) | Insurance only — 15 min hold, rarely fires |
| Phoenix 75 Last Hour | loss_stop(50%) | Caps worst-case losses |
| Firebird 60 Last Hour | loss_stop(50%) | Caps worst-case losses |
| Phoenix 75 Afternoon | loss_stop(50%) | Catches 4 additional stop exits over 776 days |
| Ironclad 35 Condor | wing_stop | Safety net; 94% hit target, stops never fire |
| Firebird 60 Final Bell | wing_stop | Safety net; 15-min hold, stops never fire |
| Phoenix 75 Early Afternoon | loss_stop(50%) | Better risk profile (max loss $30K vs $48K) |
| Phoenix 75 Midday | loss_stop(50%) | $506K vs $475K wing-only; catches 13 stops |
| Firebird 60 Midday | loss_stop(70%) | $557K vs $524K wing-only; 60pt wing benefits from wider stop |
| Morning Decel Scalp | none | 11:30 time stop does all the work; stops barely trigger |

---

## 8. Factor Analysis for Sizing Scores

### 8.1 The Question

Within a strategy that has positive expected value on average, are there identifiable market conditions where the edge is much stronger or much weaker? If so, we can size proportionally — big on the best days, small on the worst — and improve risk-adjusted returns without changing the entry/exit rules.

### 8.2 The Factors Tested

For each strategy independently, we tested ~14 market-regime factors:

| Factor | Buckets | Source |
|--------|---------|--------|
| VIX level | <14, 14-17, 17-20, 20+ | Daily cache |
| Realized Vol (RV) | <8, 8-12, 12-18, 18+ | Computed from minute bars |
| VP Ratio (VIX/RV) | <1.0, 1.0-1.3, 1.3-1.7, 1.7+ | Computed |
| 5-Day Return | <-0.5%, -0.5-0.5%, 0.5-1.5%, >1.5% | Daily bars |
| Prior Day Return (magnitude) | <0.3%, 0.3-0.7%, 0.7-1.2%, >1.2% | Daily bars |
| Gap Size (magnitude) | <0.1%, 0.1-0.3%, 0.3-0.5%, >0.5% | Open vs prior close |
| Prior Day Direction | UP, DOWN, FLAT | Daily bars |
| RV Slope | RISING, FALLING, FLAT | Computed from windowed RV |
| Day of Week | Mon, Tue, Wed, Thu, Fri | Calendar |
| In Prior Week Range | true, false | Daily bars |
| Term Structure (VIX9D/VIX) | <0.90, 0.90-1.02, >1.02 | VIX9D cache |
| Prior Day Range | <0.6%, 0.6-1.0%, >1.0% | Daily bars |
| Credit/Wing Ratio | various | Entry pricing |

### 8.3 The Statistical Test

For each (strategy, factor, bucket) combination:

1. Split trades into two groups: trades IN the bucket vs. trades NOT in the bucket
2. Compute mean P&L and standard deviation for each group
3. Run a **Welch's t-test** comparing the two means

Why Welch's t-test (not Student's): the bucket sizes and variances are unequal. Welch's doesn't assume equal variance, making it appropriate for unbalanced splits.

The null hypothesis: the bucket's mean P&L equals the complement's mean P&L. A low p-value means there's a statistically meaningful difference.

Classification:
- **Significant**: p < 0.05
- **Marginal**: p < 0.15
- **Not significant**: p >= 0.15 (excluded from scoring)

We implemented the t-test manually (custom Welch's t implementation) to avoid requiring scipy in the production pipeline.

### 8.4 Why Not Bonferroni Correction Here

Bonferroni correction is appropriate when making independent binary claims ("is strategy X real?"). Here we're building a composite score where individual factors contribute incrementally. Correcting at the individual factor level would be overly conservative and eliminate genuine signal. The correction IS applied at the strategy level — when asking "is this strategy statistically significant?" we use Bonferroni across all 8 strategies (threshold 0.05/8 ≈ 0.00625).

### 8.5 Output

The factor analysis produces `sizing_factor_research_new.json` — for each strategy, every factor bucket with:
- Mean P&L in bucket
- Mean P&L outside bucket
- Sample sizes
- t-statistic
- p-value
- Effect direction (positive or negative)

---

## 9. Building Sizing Score Rubrics

### 9.1 From Factors to Weights

For each strategy, the significant and marginal factors are assigned integer weights:

| Condition | Weight Range |
|-----------|-------------|
| p < 0.01 with large positive effect | +3 |
| p < 0.05 with positive effect | +2 |
| p < 0.15 with positive effect | +1 |
| p < 0.15 with negative effect | -1 |
| p < 0.05 with negative effect | -2 |
| p < 0.01 with large negative effect | -3 |

Each strategy's scoring function checks 6-8 factors and sums the weights. The composite score typically ranges from -14 to +15 depending on the strategy.

### 9.2 Strategy-Specific Directions

Factor directions are NOT universal. For example:
- Most strategies: low VP ratio (VP < 1.0) is bad (vol is underpriced, credit is thin)
- Ironclad 35 Condor: high VP ratio is actually bad (inverted vs. other strategies)
- Most strategies: INVERTED term structure (VIX9D/VIX < 0.90) is negative
- Ironclad 35: CONTANGO (VIX9D/VIX > 1.05) is the BEST condition

This is why each strategy gets its own scoring function rather than a shared model.

### 9.3 Example: Phoenix 75 Power Close (Strongest Signal Set)

```python
def score_phx75_power_close(t):
    s = 0
    vix = t.get("vix", 0) or 0
    if vix < 14: s += 1
    elif 14 <= vix < 17: s += 2
    elif 17 <= vix < 20: s += 3      # Sweet spot: enough premium without excessive risk
    elif vix >= 20: s -= 3            # Too much gamma risk

    rv = t.get("rv", 0) or 0
    if rv < 8: s += 3                 # Low realized vol = market isn't actually moving
    elif 8 <= rv < 12: s += 1
    elif rv >= 18: s -= 3             # High realized vol = dangerous for short gamma

    vp = t.get("vp_ratio", 999)
    if vp > 1.7: s += 3              # Credit is rich relative to actual movement
    elif 1.3 <= vp < 1.7: s += 1
    elif vp < 1.0: s -= 2            # Credit is cheap — vol is underpriced

    r5d = t.get("prior_5d", 0) or 0
    if r5d > 1.5: s += 2             # Strong recent uptrend = continuation/stability
    elif r5d < -0.5: s -= 2          # Recent selloff = instability

    ts = t.get("ts_label", "")
    if ts == "FLAT": s += 2           # Normal term structure = stable regime
    elif ts == "INVERTED": s -= 2     # Inverted = stress signal

    # Prior day magnitude
    p1d = t.get("prior_1d")
    if p1d is not None:
        ar = abs(p1d)
        if ar < 0.3: s += 2          # Quiet prior day = likely quiet today
        elif ar > 1.2: s -= 1        # Wild prior day = momentum could continue

    return s
```

### 9.4 Scoring Functions Registry

All scoring functions are centralized in `sizing_scores.py`:

```python
SCORE_FUNCTIONS = {
    # Legacy strategies
    "v3": score_v3, "n15": score_n15, "v6": score_v6,
    "v7": None,  # too few trades for reliable scoring
    "v9": score_v9, "v12": score_v12,
    "n17": score_n17, "n18": score_n18,
    # New strategies
    "Phoenix 75 Power Close": score_phx75_power_close,
    "Phoenix 75 Last Hour": score_phx75_last_hour,
    # ... etc for all 10
}
```

---

## 10. Threshold Calibration

### 10.1 The Problem

A composite score of +5 is meaningless without knowing: is +5 good or bad for this strategy? If scores range from -2 to +8, then +5 is above average. If they range from -14 to +15, then +5 is middling.

### 10.2 Quartile-Based Thresholds

For each strategy, we scored every historical trade, then computed the quartiles (25th, 50th, 75th percentile) of the score distribution:

```
scores = [score_fn(trade) for trade in strategy_trades]
t25 = percentile(scores, 25)
t50 = percentile(scores, 50)
t75 = percentile(scores, 75)
```

These become the thresholds for sizing tiers:
- Score ≤ t25 → 25% of max budget (bottom quartile — worst conditions)
- Score ≤ t50 → 50% (below median)
- Score ≤ t75 → 75% (above median but not top quartile)
- Score > t75 → 100% (top quartile — best conditions, full size)

### 10.3 Calibrated Thresholds

```python
SCORE_THRESHOLDS = {
    "v3": (-2, 0, 3),
    "n15": (-2, -1, 2),
    "v6": (-3, 0, 2),
    "v9": (-3, -1, 3),
    "v12": (-3, 1, 2),
    "n17": (-2, 0, 3),
    "n18": (0, 1, 2),
    "Phoenix 75 Power Close": (-3, 3, 6),
    "Phoenix 75 Last Hour": (-2, 3, 6),
    "Phoenix 75 Midday": (0, 3, 4),
    "Phoenix 75 Early Afternoon": (-1, 1, 3),
    "Phoenix 75 Afternoon": (-1, 1, 2),
    "Firebird 60 Final Bell": (-2, 2, 5),
    "Firebird 60 Last Hour": (-1, 3, 6),
    "Firebird 60 Midday": (0, 3, 5),
    "Ironclad 35 Condor": (0, 1, 3),
    "Morning Decel Scalp": (-1, 2, 4),
}
```

### 10.4 The score_to_multiplier Function

```python
def score_to_multiplier(score, ver):
    thresholds = SCORE_THRESHOLDS.get(ver)
    if thresholds is None:
        return 1.0
    t25, t50, t75 = thresholds
    if score <= t25: return 0.25
    if score <= t50: return 0.50
    if score <= t75: return 0.75
    return 1.00
```

---

## 11. Backtest Validation (Flat vs. Scored)

### 11.1 The Comparison

For each strategy, we ran two parallel P&L simulations over the full trade history:

**Flat sizing**: Every trade gets 100% of the strategy's max budget. Contract count = `floor(budget / (max_risk_per_spread * 100))`.

**Scored sizing**: Each trade gets `score_to_multiplier(score, strategy)` × max budget. Contract count is recomputed with the scaled budget.

### 11.2 Metrics Compared

For each simulation:
- Total cumulative P&L
- Maximum drawdown
- Return-to-drawdown ratio
- Win rate
- Average P&L per trade
- P&L in top-quartile score trades vs. bottom-quartile

### 11.3 Acceptance Criterion

Scored sizing is kept for a strategy only if:
1. Return-to-drawdown ratio improves (primary)
2. Total P&L is similar or better (not dramatically worse)
3. The scoring concentrates capital correctly: top-quartile trades have higher mean P&L than bottom-quartile (the scoring function actually identifies better/worse days)

### 11.4 The Two-Layer Sizing Model (New Strategies)

New strategies stack two multipliers:

```
Layer 1: VIX Tier
    VIX < 20  → 1.00 (full size)
    VIX 20-25 → 0.50 (half size)
    VIX 25+   → 0.25 (quarter size)

Layer 2: Composite Score
    Score ≤ t25 → 0.25
    Score ≤ t50 → 0.50
    Score ≤ t75 → 0.75
    Score > t75 → 1.00

Final budget = base_budget × VIX_mult × score_mult
```

Example: Phoenix 75 Power Close ($150K base) on a VIX=22 day (×0.50) with a mediocre score (×0.50) deploys $37,500 instead of $150,000.

For legacy strategies (V3, V6, V9, V12), the existing VP-scaled sizing remains the primary layer, with the composite score as an overlay.

---

## 12. Live Production System

### 12.1 Architecture

The system runs on Railway (auto-deploys from GitHub pushes to `main`):

- **`cockpit_feed.py`**: Main server process. Polls Polygon every 10 seconds for live SPX, VIX, and option chain data. Computes regime classification, signal matches, and writes `cockpit_state.json`. Also serves HTTP endpoints.
- **`trading_cockpit.html`**: Live trading dashboard. Fetches `cockpit_state.json` every 3 seconds. Displays all 18 strategy signals, their match status, sizing scores, entry/exit forms, P&L tracking, and wing breach alerts.
- **`strategy_calendar.html`**: Historical view. Fetches trade data from `/api/calendar` and displays a month-by-month calendar with per-day trade outcomes.

### 12.2 Signal Persistence (Entry-Time Latching)

When a strategy reaches its entry time during market hours, the poll loop evaluates it once and stores the result in `_entry_regime[ver]` — a module-level dict. For the rest of the day, every subsequent poll cycle reuses the stored result without re-evaluating. This prevents:
- Signal flickering (matched/unmatched as market data shifts)
- Re-scoring with different market conditions than what existed at entry time
- Inconsistency between what the dashboard shows and what was true at entry

The `_entry_regime` dict clears at the start of each new trading day.

### 12.3 Data Pipeline (Keeping Results Current)

**Legacy strategies (V3-V12, N15):**
- Historical: `research_all_trades.csv` (570 rows through 2026-03-05)
- Incremental: `refresh_legacy_strategies.py` pulls new dates from Polygon and runs the full simulation engine
- Both feed into `strategy_trades.json` (479 trades) and `strategy_stats.json`

**New strategies (Phoenix 75, Firebird 60, Ironclad 35, Morning Decel):**
- Full regen: `generate_calendar_data.py` processes all dates through the simulation engine
- Incremental: `refresh_new_strategies.py` handles new dates
- Both feed into `calendar_trades.json` (5,073 trades)
- Both apply sizing scores at generation time

**Real-fill strategies (N17, N18):**
- Source: `real_fills.json` — actual broker execution data
- Updated by `pull_real_fills.py` which calls the broker API
- P&L is exact — no slippage model needed
- Provides ground truth for how backtested edge translates to real execution

### 12.4 EOD Auto-Refresh

At 5:30 PM ET on weekdays, `cockpit_feed.py` automatically:
1. Runs `pull_real_fills.py` (fetches new N17/N18 broker fills)
2. Runs `compute_stats.py` (regenerates stats from updated data)

This keeps the calendar and stats current without manual intervention.

---

## 13. How P&L Tracking Drives Continuous Validation

P&L tracking is not a reporting feature — it is the feedback loop that validates every decision:

| Stage | How P&L Tracking Was Used |
|-------|--------------------------|
| Strategy discovery | Cumulative P&L curves over time determine which regimes have real edge vs. noise |
| Filter addition | Before/after cumulative curves with each filter confirm it improves the equity path |
| Stop optimization | Cumulative curves per stop config show which reduces drawdowns without killing returns |
| Strategy elimination | V8/V10/V14 removed because their cumulative curves went flat at correct data resolution |
| Sizing score validation | Flat vs. scored cumulative curves confirm scoring concentrates capital correctly |
| Live monitoring | Calendar tracks daily outcomes; declining cumulative curve = investigate immediately |
| Real vs. backtested | N17/N18 real fills vs. backtested P&L shows execution quality and model accuracy |

The fundamental principle: **a strategy is only as real as its cumulative P&L curve.** Summary statistics (mean, win rate, Sharpe) can hide problems that the curve reveals — drawdown depth, recovery time, regime dependency, luck concentration.

---

## 14. Known Limitations and Honest Caveats

### 14.1 Bull Market Bias
The backtest period (2023-2026) is predominantly a bull market with compressed VIX. Strategies that require low VIX or upward momentum may be over-represented. Performance in a sustained bear market or VIX spike regime is unknown.

### 14.2 Statistical Significance
- V3 PHOENIX: p ≈ 0.0086 — does NOT survive Bonferroni correction (threshold 0.00625 for 8 strategies)
- V7 (n=5) and V12 (n=10): far too few trades for reliable statistical inference
- The fat left tail on V3 (WING_STOP events) means the normal-assumption t-test is approximate; bootstrap or permutation tests would be more rigorous

### 14.3 V3/N15 Overlap
N15 is a strict subset of V3 (same entry, additional VIX9D filter). ~82% of V3 trades also qualify as N15. Running both at full size = 2× PHOENIX exposure on most days. This is concentration, not diversification.

### 14.4 Sizing Score Overfitting Risk
The factor analysis and threshold calibration use the same dataset as the backtest. Walk-forward or out-of-sample validation would strengthen confidence. The scoring is deliberately simple (integer weights, quartile thresholds) to reduce overfitting risk, but it's not zero.

### 14.5 In-Memory State
`_entry_regime` (signal latching) is in-memory. A Railway redeploy mid-day clears it, causing strategies to re-evaluate at the next poll cycle with potentially different market conditions than existed at entry time. This is a known limitation shared by all strategies.

---

## 15. File Reference

| File | Role |
|------|------|
| `cockpit_feed.py` | Main server: polls Polygon, computes signals, serves cockpit state |
| `trading_cockpit.html` | Live dashboard: displays signals, entry/exit forms, P&L tracking |
| `strategy_calendar.html` | Historical calendar: month-by-month trade outcomes |
| `sizing_scores.py` | Scoring functions and thresholds for all 18 strategies |
| `generate_calendar_data.py` | Full regeneration of new strategy trades with scoring |
| `refresh_new_strategies.py` | Incremental update for new strategy dates |
| `refresh_legacy_strategies.py` | Incremental update for legacy strategy dates |
| `compute_stats.py` | Generates `strategy_trades.json` + `strategy_stats.json` from CSV |
| `research_all_trades.csv` | Master backtest data for legacy strategies |
| `calendar_trades.json` | All new strategy trades (5,073 records) |
| `strategy_trades.json` | Legacy strategy trades (479 records) |
| `strategy_stats.json` | Aggregated per-strategy statistics |
| `real_fills.json` | N17/N18 actual broker execution data |
| `spx_gap_cache.json` | SPX overnight gap % by date |
| `vix9d_daily.json` | VIX9D daily values for term structure |
| `pull_real_fills.py` | Fetches N17/N18 broker fills |
| `CLAUDE.md` | Project instructions and strategy definitions |
