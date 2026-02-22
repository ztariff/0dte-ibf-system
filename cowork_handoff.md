# SPX 0DTE Iron Butterfly Backtest — Project Handoff

## What This Is
A 0-DTE SPX Iron Butterfly (IBF) strategy backtest. Entry at 10am daily, exit at 50% profit target or 3:30pm time stop. Wings at ±40pts from ATM. $100K total budget split into three $33.3K tranches — P1 at entry, P2/P3 as "ladder adds" if SPX drifts 15+ pts.

## Key Files
- `backtest_v4.py` — the current backtest script (Polygon API required, pass key as first arg)
- `backtest_v3_results_new.csv` — last full run results (171 days, Jun 2025–Feb 2026)
- `backtest_v4_results.html` — interactive dashboard (4 tabs: Overview, What Changed, Deep Analysis, All Trades). After re-running the backtest, update the trade data array in this file with the new CSV results and regenerate.

## How to Run
```
python3 backtest_v4.py YOUR_POLYGON_KEY [LOOKBACK_DAYS]
```
Default lookback is 171 days. Outputs a CSV + prints a trade-by-trade log.

---

## Architecture Summary

### Scoring (score_entry)
Scores each day 0–100 across: vol premium (VIX/RV ratio), regime (VIX level), term structure (VIX9D/VIX slope), range % (morning range), VWAP slope. GO if score ≥ 70, WAIT if ≥ 45, SKIP otherwise.

### Entry Gate
GO signals are further blocked if:
- VWAP slope is RISING (bad for short-vol)
- RV slope is RISING (realized vol accelerating)

### Pricing (price_ibf)
Black-Scholes. Entry at 10am SPX price, IV = 10am VIX/100, T = mins to 3:30pm close.

### Dynamic IV (adj_iv) — added in v4
```python
def adj_iv(iv_entry, S_entry, S_now):
    move = S_now - S_entry
    if move < 0:
        return iv_entry + abs(move) * 0.0005   # +0.5 vol pts per 10pt adverse move
    else:
        return max(iv_entry * 0.92, iv_entry - move * 0.0002)
```

### Exit Logic (run_position) — current v4 state
Three exits in priority order:
1. **TIME_STOP** — 3:30pm, exit at mark
2. **TARGET** — val >= 50% of credit collected
3. **WING_STOP** — SPX crosses either wing strike (NEW in v4, replaces old 90%-of-max-loss stop)

```python
if S_now <= wp or S_now >= wc:
    return round(val * scale, 3), "WING_STOP", bar_t
```

### Ladder Adds (P2, P3)
After entry, scan bars while P1 is alive. If SPX drifts 15+ pts from last IBF center AND slopes are still OK (STABLE or FALLING RV, non-RISING VWAP), open another IBF at current price using refreshed IV. Max 2 adds. Each add sized to its own $33.3K tranche.

---

## Last Run Results (v3 — pre-WING_STOP)

| Metric | Value |
|---|---|
| Days scanned | 171 (Jun 2025–Feb 2026) |
| GO signals | 73 (42.7%) |
| Win rate | 78.1% (57W / 16L) |
| Avg winner | +$38,994 |
| Avg loser | -$16,022 |
| Total P1 | $1,966,323 |
| Total combined (w/ adds) | $3,668,812 |
| Max drawdown P1 | -$59,910 |
| Profit factor | 8.67x |
| HARD_STOP exits | 4 (old stop — now replaced) |

---

## What Just Changed in v4

### WING_STOP replaces HARD_STOP
The old stop (90% of max loss) was nearly useless — due to the long wings offsetting losses, reaching 90% max loss intraday requires a 60–100pt SPX move. It only fired in the last 30 minutes, essentially identical to TIME_STOP.

The new WING_STOP fires the moment SPX crosses a wing strike (40pts from ATM). This is the real risk threshold — past this point you're short a deeply ITM straddle leg.

### Projected Impact of WING_STOP (not yet confirmed with live run)
Back-solving from exit P&L, 6 of the 16 loss days had SPX definitively cross the wing post-entry:

| Date | Old P&L | Wing Stop Est. | Saved |
|---|---|---|---|
| 2025-10-10 | -$32,915 | ~-$4,500 | ~+$28K |
| 2025-10-16 | -$32,852 | ~+$3,600 | ~+$36K |
| 2025-08-07 | -$32,608 | ~-$4,500 | ~+$28K |
| 2025-12-10 | -$29,801 | ~-$2,800 | ~+$27K |
| 2025-09-23 | -$25,417 | ~-$5,200 | ~+$20K |
| 2025-07-07 | -$19,020 | ~-$4,100 | ~+$15K |

**Estimated total savings: ~$155K on P1** — but this assumes clean break-through moves, not touch-and-reverse. Need a live re-run to confirm.

The other 10 loss days had SPX stop within ~5–9pts of the wing and reverse — WING_STOP would NOT have triggered on those, and they'd still exit at TIME_STOP.

---

## Known Remaining Issues / Next Steps

1. **Re-run the backtest** with `backtest_v4.py` to get actual WING_STOP outcomes. This is the most urgent thing.

2. **Add-on position behavior at WING_STOP** — currently unspecified. If P1 hits WING_STOP, should an open P2 also close? Probably yes, but the code doesn't handle this yet.

3. **Sample bias** — Jun 2025–Feb 2026 was abnormally calm (VIX 13–22, no crash events, no 40pt gap opens). Profit factor of 8.67x is regime-specific. Consider re-running on a volatile stretch (e.g., Aug 2024 VIX spike, early 2022 drawdown) once the current run is confirmed.

4. **Slippage / commissions not modeled** — Real-world friction would reduce avg P&L by $120–320/trade. Apply a ~35–40% haircut to P1 totals for live expectations.

5. **P2 outperforming P1 by 1.45x** is structurally real (later entry = faster theta, adds often enter in elevated IV), but the magnitude may compress in higher-vol regimes.

---

## The One Thing to Do First
Run `backtest_v4.py` and compare WING_STOP outcomes against the loss-day table above. That tells you whether the wing stop is saving money or triggering on reversals.
