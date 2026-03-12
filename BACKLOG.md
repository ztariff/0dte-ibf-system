# PHOENIX IBF Strategy Backlog

Research items to revisit once the core strategy is stable.

---

## 1. Intraday Regime Shift as Exit Signal
**Context:** Currently the regime is classified once at entry time and latched for the day. VIX bucket and weekly range (IN/OT) can change intraday. If the regime shifts mid-trade, the original strategy no longer matches the current environment.

**Research question:** Does exiting early when the regime shifts away from the entry regime save money on losers without giving back too much on winners?

**Approach:**
- Compare entry-time VIX bucket vs. midday VIX bucket for each historical trade
- Check if trades that experienced a regime shift (especially VIX MID->ELEV) performed worse
- Test an early exit rule: "if regime no longer matches your strategy, close immediately"
- Measure impact on total P&L, profit factor, and max drawdown

**Priority:** Medium -- only 2 of 4 regime components can shift intraday (VIX bucket, range). VIX spikes often mean-revert, so this may be noise. But worth quantifying.

---

## 2. V3 Trailing Profit Stop
**Context:** V3 uses a fixed 50% profit target. On strong pinning days the butterfly might hit 50% by noon and keep going to 80-90%, but we cap the gain. A trailing mechanism could capture more upside.

**Research question:** After V3 hits 30-40% profit, does switching to a trailing stop (e.g., "close if profit drops 15% from intraday peak") improve total P&L vs. the fixed 50% target?

**Approach:**
- Using pnl_at_XXXX columns, track the intraday profit high-water mark for each V3 trade
- Simulate trailing stop: once profit >= 30%, close if profit retreats by 15% from peak
- Test different trigger thresholds (30%, 40%) and trail widths (10%, 15%, 20%)
- Compare total P&L, profit factor, and win rate vs. fixed 50%

**Priority:** Medium -- could unlock more upside on the best days, but adds complexity to live execution.

---

## 3. V3 Signal Strength Sizing
**Context:** V3 currently uses tiered sizing based on how many of the 5 PHOENIX signals fire. Could test more aggressive scaling with signal count.

**Research question:** Does proportional or sharper-tiered sizing based on signal count (e.g., 1 signal = 40% size, 5 signals = 200% size) improve risk-adjusted returns?

**Approach:**
- From strategy_v3_PHOENIX.json, get the signal count per day
- Test sizing models: flat, linear proportional, exponential, custom tiers
- Measure impact on total P&L, Sharpe, max drawdown, and Calmar

**Priority:** Medium -- current tiered approach works, but there may be alpha in leaning harder into high-confluence days.

---

## 4. V3 VIX Term Structure Filter
**Context:** V3 fires based on VP ratio, 5dRet, RV change, etc. but doesn't consider VIX term structure. Backwardation (VIX > VIX futures) signals fear and tends to crush butterflies.

**Research question:** Does adding a VIX contango/backwardation filter as a 6th signal or universal filter improve V3's profit factor?

**Approach:**
- Source VIX futures data (VX1, VX2) or VIX9D vs VIX
- Compute term structure slope for each V3 trade day
- Check if V3 trades taken during backwardation underperform
- Test as a filter: skip entry when term structure is in backwardation

**Priority:** Medium -- requires sourcing VIX futures data. Strong theoretical basis but may not have enough backwardation days in sample for statistical significance.

---

## 5. V3 Entry Timing Refinement
**Context:** V3 enters at 10:00 fixed. The first 30 minutes after open (9:30-10:00) can be choppy. Waiting slightly longer could give a cleaner read on the day's character.

**Research question:** Does entering at 10:15 or 10:30 instead of 10:00 improve V3 performance by avoiding early morning whipsaws?

**Approach:**
- Using pnl_at_XXXX columns, simulate entry at 10:15 and 10:30 (use credit at those times)
- Compare P&L, win rate, and profit factor vs. 10:00 entry
- Check if the signal filters (VP, 5dRet, etc.) are more accurate with 30-60 extra minutes of data

**Priority:** Medium -- simple to test but may reduce the number of valid trading days if signals change by 10:30.

---

## 6. Dynamic Wing Width by VIX/Regime
**Context:** All strategies use fixed 50pt wings. In low VIX (tight ranges), narrower wings (30-40pt) collect less credit but have tighter breakevens. In elevated VIX, wider wings (60-70pt) give more room for the butterfly to work.

**Research question:** Does varying wing width by VIX bucket or regime improve risk-adjusted returns for V3?

**Approach:**
- Requires re-pricing the option chain at different wing widths (30, 40, 50, 60, 70pt)
- For each V3 trade day, compute credit and P&L at each wing width
- Test rules like: VIX<15 = 40pt wings, VIX 15-20 = 50pt, VIX>20 = 60pt
- Compare total P&L, profit factor, max drawdown across configurations

**Priority:** Medium-High -- strong theoretical basis but requires option chain data at multiple strikes. Could be a significant edge if the relationship holds.
