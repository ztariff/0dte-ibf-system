# Strategies to Replicate — A+ and A Tier

These 10 strategies were independently discovered on 2024 data and blind-validated on 2025 data. Each includes exact parameters for replication. All follow forward-walk rules: entry decisions use only data available at or before entry time.

Refer to `DATA_REFERENCE.md` for the data structure and file locations.

---

## Grading Methodology

Each strategy was graded on a composite score (0–100) based on:
- Statistical significance (combined p-value across both years)
- Annualized Sharpe ratio on combined data
- Profit factor on combined data
- Cross-year stability (profitable in both 2024 AND 2025 independently)
- Half-split stability on combined data (both halves profitable)
- Monthly consistency (% of months profitable)

A+ = score >= 80, A = score >= 70.

---

## Strategy #5 — IBF_75w 15:15 [GRADE: A+, score: 100]

**The single best risk-adjusted strategy found.**

### Structure
ATM Iron Butterfly with 75-point wings.

At entry time T:
1. Get SPX price from `spx_1min/{date}.json` at time T
2. Round to nearest 5 → `ATM`
3. Sell 1 call at ATM, sell 1 put at ATM
4. Buy 1 call at ATM+75, buy 1 put at ATM-75
5. Credit = `option_chains/{date}.json → strikes[ATM].C[T].c + strikes[ATM].P[T].c - strikes[ATM+75].C[T].c - strikes[ATM-75].P[T].c`
6. Max risk per spread = 75 - credit

### Entry
- Time: **15:15 ET** (use the 15:15 option bar)
- Every trading day, no pre-open filter, no intraday filter

### Exit Rules (checked at each 5-min option bar after entry)
Priority order:
1. **Profit target**: Exit if current P&L >= 50% of entry credit
   - P&L = entry_credit - current_value_of_structure
   - current_value = strikes[ATM].C[T2].c + strikes[ATM].P[T2].c - strikes[ATM+75].C[T2].c - strikes[ATM-75].P[T2].c
2. **Wing stop**: Exit if SPX (from 1-min bars) crosses ATM+75 or ATM-75 at any point after entry
3. **Time stop**: Exit at **15:30** if neither target nor wing stop triggered
4. If no option bar at 15:30, use intrinsic value: call = max(0, SPX-strike), put = max(0, strike-SPX)

### Slippage
Deduct $1 per spread from final P&L (flat slippage model).

### Expected Results
| Metric | 2024 | 2025 | Combined |
|--------|------|------|----------|
| Trades | 98 | 173 | 271 |
| Win Rate | 76.5% | 80.3% | 79.0% |
| Avg $/trade | $211 | $220 | $217 |
| Profit Factor | 3.67 | 4.95 | 4.38 |
| Sharpe (ann.) | 4.62 | 7.55 | 8.42 |
| Max Drawdown | -$1,787 | -$1,663 | -$1,787 |
| Calmar | 11.58 | 22.92 | 32.91 |
| Max Consec Losses | 3 | 4 | 4 |
| Losing Months | 2/12 | 0/12 | 2/24 |
| 2025 edge retention | 104% of 2024 |

---

## Strategy #7 — MID-WIDE 15:00 IBF [GRADE: A+, score: 93]

### Structure
ATM Iron Butterfly with **60-point** wings (same construction as #5 but narrower wings: ATM±60).

### Entry
- Time: **15:00 ET**
- Unfiltered (every trading day)

### Exit Rules
Same as #5: 50% profit target → wing stop → 15:30 time stop.
Wing strikes are ATM+60 and ATM-60.

### Expected Results
| Metric | 2024 | 2025 | Combined |
|--------|------|------|----------|
| Trades | 219 | 236 | 455 |
| Win Rate | 73.5% | 74.6% | 74.1% |
| Avg $/trade | $136 | $118 | $126 |
| Profit Factor | 2.49 | 2.40 | 2.44 |
| Sharpe | 5.00 | 5.05 | 5.29 |
| Max Drawdown | -$2,610 | -$2,436 | -$3,263 |
| Losing Months | 1/14 | 1/12 | 2/26 |
| 2025 retention | 87% |

---

## Strategy #4 — WIDE POWER HOUR IBF [GRADE: A+, score: 92]

### Structure
ATM Iron Butterfly with **75-point** wings.

### Entry
- Time: **15:00 ET**
- Unfiltered

### Exit Rules
Same as #5: 50% target → wing stop → 15:30 time stop.

### Expected Results
| Metric | 2024 | 2025 | Combined |
|--------|------|------|----------|
| Trades | 106 | 193 | 299 |
| Win Rate | 78.3% | 75.6% | 76.6% |
| Avg $/trade | $259 | $196 | $218 |
| Profit Factor | 3.44 | 3.05 | 3.19 |
| Sharpe | 4.65 | 6.02 | 6.95 |
| Max Drawdown | -$3,607 | -$2,169 | -$4,795 |
| Losing Months | 3/14 | 0/12 | 3/26 |
| 2025 retention | 76% |

**Note:** This and #7 both enter at 15:00 but with different wing widths (75 vs 60). They're not independent — running both doubles your exposure at 15:00. Pick one.

---

## Strategy #8 — IBF_60w 15:30 [GRADE: A+, score: 92]

### Structure
ATM Iron Butterfly with **60-point** wings.

### Entry
- Time: **15:30 ET**
- Unfiltered

### Exit Rules
Same framework: 50% target → wing stop → 15:30 time stop.
Since entry IS at 15:30, the time stop is effectively "hold to close."
Mark at the next available 5-min option bars (15:35, 15:40, etc.) or use intrinsic at settlement.

### Expected Results
| Metric | 2024 | 2025 | Combined |
|--------|------|------|----------|
| Trades | 155 | 211 | 366 |
| Win Rate | 78.1% | 69.7% | 73.2% |
| Avg $/trade | $169 | $65 | $109 |
| Profit Factor | 4.32 | 2.42 | 3.28 |
| Sharpe | 6.12 | 3.79 | 5.83 |
| Max Drawdown | -$2,304 | -$2,035 | -$2,304 |
| Losing Months | 1/14 | 1/12 | 2/26 |
| 2025 retention | 38% |

**Note:** Edge compressed significantly in 2025 (38% retention). The later entry gives less time for theta but also less time for the target to hit. Most trades exit on time stop.

---

## Strategy #16 — ULTRA-WIDE OTM CONDOR [GRADE: A+, score: 92]

**Different structure from all others — an OTM iron condor, not an ATM butterfly.**

### Structure
Iron Condor with shorts 35 points from ATM, 35-point wings.

At entry time T:
1. Get SPX, round to nearest 5 → ATM
2. Sell 1 call at ATM+35, sell 1 put at ATM-35
3. Buy 1 call at ATM+70, buy 1 put at ATM-70
4. Credit = `strikes[ATM+35].C[T].c + strikes[ATM-35].P[T].c - strikes[ATM+70].C[T].c - strikes[ATM-70].P[T].c`
5. Max risk = 35 - credit (per side)

### Entry
- Time: **14:30 ET**
- Unfiltered

### Exit Rules (different from IBF strategies)
1. **Profit target**: Exit if P&L >= **40%** of entry credit
2. **Wing stop**: Exit if SPX crosses ATM+70 or ATM-70 (the long wing strikes)
3. **Time stop**: Exit at **15:30**

### Expected Results
| Metric | 2024 | 2025 | Combined |
|--------|------|------|----------|
| Trades | 155 | 217 | 372 |
| Win Rate | 97.4% | 91.7% | 94.1% |
| Avg $/trade | $42 | $24 | $32 |
| Profit Factor | 11.24 | 1.93 | 2.85 |
| Sharpe | 5.39 | 2.30 | 3.72 |
| Max Drawdown | -$509 | -$1,229 | -$1,229 |
| Max Consec Losses | 1 | 2 | 2 |
| Losing Months | 0/14 | 2/12 | 2/26 |
| 2025 retention | 57% |

**Note:** Very high win rate but small $ per trade. The edge is in consistency — 94% wins with max 2 consecutive losses. PF dropped from 11.24 to 1.93 in 2025 due to higher-vol environment making the shorts closer to at-risk. Still profitable.

---

## Strategy #1 — WIDE AFTERNOON IBF [GRADE: A+, score: 83]

### Structure
ATM Iron Butterfly with **75-point** wings.

### Entry
- Time: **13:45 ET**
- Unfiltered

### Exit Rules
50% target → wing stop → 15:30 time stop.

### Expected Results
| Metric | 2024 | 2025 | Combined |
|--------|------|------|----------|
| Trades | 150 | 225 | 375 |
| Win Rate | 77.3% | 71.1% | 73.6% |
| Avg $/trade | $357 | $211 | $269 |
| Profit Factor | 2.64 | 1.72 | 2.03 |
| Sharpe | 4.91 | 3.21 | 4.50 |
| Max Drawdown | -$4,207 | -$8,487 | -$8,487 |
| Losing Months | 3/14 | 1/12 | 4/26 |
| 2025 retention | 59% |

**Highest total dollar P&L** across all strategies ($100,982 combined) due to large credit collected on 75-point wings and earlier entry capturing more theta. Tradeoff: larger drawdown than the 15:00+ entries.

---

## Strategy #2 — WIDE LATE AFT IBF [GRADE: A+, score: 83]

### Structure
ATM Iron Butterfly with **75-point** wings.

### Entry
- Time: **14:00 ET**
- Unfiltered

### Exit Rules
50% target → wing stop → 15:30 time stop.

### Expected Results
| Metric | 2024 | 2025 | Combined |
|--------|------|------|----------|
| Trades | 159 | 221 | 380 |
| Win Rate | 76.7% | 73.3% | 74.7% |
| Avg $/trade | $277 | $207 | $236 |
| Profit Factor | 2.17 | 1.84 | 1.97 |
| Sharpe | 3.44 | 3.48 | 3.99 |
| Max Drawdown | -$6,342 | -$9,736 | -$9,736 |
| Losing Months | 2/14 | 1/12 | 3/26 |
| 2025 retention | 75% |

---

## Strategy #3 — IBF_75w 14:30 [GRADE: A+, score: 83]

**The only strategy where 2025 was BETTER than 2024.**

### Structure
ATM Iron Butterfly with **75-point** wings.

### Entry
- Time: **14:30 ET**
- Unfiltered

### Exit Rules
50% target → wing stop → 15:30 time stop.

### Expected Results
| Metric | 2024 | 2025 | Combined |
|--------|------|------|----------|
| Trades | 132 | 216 | 348 |
| Win Rate | 72.7% | 71.8% | 72.1% |
| Avg $/trade | $179 | $229 | $210 |
| Profit Factor | 1.95 | 2.49 | 2.26 |
| Sharpe | 2.67 | 5.27 | 4.82 |
| Max Drawdown | -$7,829 | -$3,619 | -$7,829 |
| Losing Months | 3/14 | 0/12 | 3/26 |
| 2025 retention | **128%** |

**Note:** Edge INCREASED in 2025 because higher VIX = higher credit collected on 75-point wings. The wider wings absorbed 2025's larger intraday moves without getting stopped out. This is the most regime-robust strategy on the board.

---

## Strategy #6 — MID-WIDE AFT IBF [GRADE: A, score: 78]

### Structure
ATM Iron Butterfly with **60-point** wings.

### Entry
- Time: **14:00 ET**
- Unfiltered

### Exit Rules
50% target → wing stop → 15:30 time stop.

### Expected Results
| Metric | 2024 | 2025 | Combined |
|--------|------|------|----------|
| Trades | 253 | 242 | 495 |
| Win Rate | 74.3% | 71.5% | 72.9% |
| Avg $/trade | $218 | $101 | $161 |
| Profit Factor | 2.16 | 1.44 | 1.77 |
| Sharpe | 4.52 | 2.15 | 3.40 |
| Max Drawdown | -$5,526 | -$6,997 | -$6,997 |
| Losing Months | 2/14 | 2/12 | 4/26 |
| 2025 retention | 46% |

**Note:** Highest trade count (495) but 2025 edge compressed to 46% of 2024. The 60-point wings got breached more often in 2025's higher-vol environment. Consider the 75-point versions (#2, #3) which held up better.

---

## Strategy #17 — MORNING DECEL SCALP [GRADE: A, score: 73]

**The only morning strategy that survived, but barely.**

### Structure
ATM Iron Butterfly with **75-point** wings.

### Entry
- Time: **10:30 ET**
- Intraday filter: **Decelerating** — enter only when SPX price acceleration is negative
  - Compute acceleration: compare velocity (pts/min) over bars [T-20m, T-10m] vs [T-10m, T]
  - velocity = (last_bar_close - first_bar_close) / number_of_bars
  - acceleration = recent_velocity - prior_velocity
  - Enter only if acceleration < -0.05 (market was moving but is slowing down)
  - Uses 1-min SPX bars, 10-bar windows
  - Forward-walk safe: only bars at or before 10:30

### Exit Rules
1. **Profit target**: Exit if P&L >= **30%** of credit (lower target = faster exit)
2. **Wing stop**: SPX crosses ATM±75
3. **Time stop**: **11:30 ET** (60-minute maximum hold — get out before midday)

### Expected Results
| Metric | 2024 | 2025 | Combined |
|--------|------|------|----------|
| Trades | 118 | 131 | 249 |
| Win Rate | 77.1% | 64.1% | 70.3% |
| Avg $/trade | $218 | $11 | $109 |
| Profit Factor | 3.61 | 1.09 | 2.05 |
| Sharpe | 4.41 | 0.37 | 3.76 |
| Max Drawdown | -$2,981 | -$5,796 | -$5,796 |
| Losing Months | 2/14 | 4/12 | 6/26 |
| 2025 retention | **5%** |

**Honest caveat:** This barely survived. 2025 avg was only $11/trade (vs $218 in 2024). The morning deceleration signal worked in the low-vol 2024 environment but nearly broke even in 2025's higher vol. The combined stats look acceptable only because 2024 was so strong. Grade it A on combined metrics but treat it as fragile.

---

## How to Replicate

For each strategy:

1. Load `data/option_chains/{date}.json` for each trading day
2. At the specified entry time, compute ATM from SPX 1-min bars
3. Look up the 4 option legs using absolute strike keys in the chain file
4. Compute credit (all legs must have data or skip the day)
5. Walk forward through 5-min option bars checking exit rules in priority order
6. For wing stop detection, use 1-min SPX bars (higher resolution)
7. Apply $1/spread slippage deduction to final P&L
8. Never fabricate prices — if a leg can't be priced, skip that day entirely

The key difference vs. the old data format: strikes are absolute and fixed for the entire day. A position entered at strike 5800 can be marked at any later time using `strikes["5800"]` regardless of where ATM has moved. No re-centering, no survivorship bias.
