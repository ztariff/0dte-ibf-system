# Top Strategies From Cursor Analysis

This file summarizes the strongest strategies I found and validated on local data in `C-0DTE-IC/data`.

## Scope

- Data window used for validation: `2024-01-02` to `2025-12-31`
- Pricing/exits:
  - Option bars at 5-minute resolution
  - Wing stop from 1-minute SPX bars
  - Flat slippage: `$1` per spread
- Walk-forward: rolling `6m train / 2m test` on combined 2024+2025 trade stream

---

## A) Highest Confidence (Cross-Year + Walk-Forward Strong)

### 1) `IBF_75w @ 15:15` (from replicate set #5)

- **Structure:** ATM Iron Butterfly, wings `+/-75`
- **Entry:** `15:15`
- **Exit priority:** `50% target -> wing stop -> 15:30 time stop`
- **2024:** `n=95`, `avg=$221`, `WR=77.9%`, `PF=3.94`
- **2025:** `n=173`, `avg=$220`, `WR=80.3%`, `PF=4.95`
- **Combined:** `n=268`, `avg=$221`, `WR=79.5%`, `PF=4.52`, `DD=$-1,787`
- **Walk-forward (6/2):** `8/8` positive splits, `avg OOS=$227`, `worst OOS=$135`
- **Half split:** `H1=$234`, `H2=$207`

### 2) `IBF_75w @ 15:00` (replicate #4)

- **Structure:** ATM Iron Butterfly, wings `+/-75`
- **Entry:** `15:00`
- **Exit:** `50% target -> wing stop -> 15:30`
- **2024:** `n=102`, `avg=$268`, `WR=78.4%`, `PF=3.56`
- **2025:** `n=193`, `avg=$196`, `WR=75.6%`, `PF=3.05`
- **Combined:** `n=295`, `avg=$221`, `WR=76.6%`, `PF=3.23`, `DD=$-4,795`
- **Walk-forward:** `9/9` positive splits, `avg OOS=$198`, `worst OOS=$6`
- **Half split:** `H1=$246`, `H2=$195`

### 3) `IBF_60w @ 15:00` (replicate #7)

- **Structure:** ATM Iron Butterfly, wings `+/-60`
- **Entry:** `15:00`
- **Exit:** `50% target -> wing stop -> 15:30`
- **2024:** `n=203`, `avg=$125`, `WR=72.9%`, `PF=2.33`
- **2025:** `n=236`, `avg=$118`, `WR=74.6%`, `PF=2.40`
- **Combined:** `n=439`, `avg=$121`, `WR=73.8%`, `PF=2.36`, `DD=$-3,263`
- **Walk-forward:** `8/9` positive splits, `avg OOS=$107`, `worst OOS=$-8`
- **Half split:** `H1=$129`, `H2=$114`

---

## B) Best Condor Candidate (From Cursor Discovery + Stability Pass)

### 4) `IC_so15_ww30 @ 11:35, tgt60%, tstop15:40`

- **Structure:** Iron Condor
  - Short call: `ATM+15`
  - Short put: `ATM-15`
  - Long call: `ATM+45`
  - Long put: `ATM-45`
- **Entry:** `11:35`
- **Exit priority:** `60% target -> wing stop -> 15:40 time stop`
- **Full-universe discovery rank:** top stable survivor
- **Overall (full universe 2023-11 to 2026-03):**
  - `avg=$73`
  - `WF positive splits=82%`
  - `avg OOS=$98`
  - `DD=$-6,844`
  - `H1/H2 avg=$43/$104`

Notes:
- This condor profile looked more stable in full-universe walk-forward than many late-day condor variants.
- It is still structurally correlated with other SPX short-vol entries.

---

## C) Additional Robust Candidate (Different Shape)

### 5) `IC_35otm_35w @ 14:30` (replicate #16)

- **Structure:** Iron Condor
  - Shorts: `ATM +/-35`
  - Wings: `+/-35` beyond shorts (`ATM+70` / `ATM-70`)
- **Entry:** `14:30`
- **Exit:** `40% target -> wing stop -> 15:30`
- **2024:** `n=145`, `avg=$43`, `WR=97.2%`, `PF=10.93`
- **2025:** `n=217`, `avg=$24`, `WR=91.7%`, `PF=1.93`
- **Combined:** `n=362`, `avg=$32`, `WR=93.9%`, `PF=2.82`, `DD=$-1,229`
- **Walk-forward:** `7/9` positive splits, `avg OOS=$27`, `worst OOS=$-18`
- **Half split:** `H1=$36`, `H2=$27`

---

## Not Recommended As Primary

### `IBF_75w @ 10:30 + Decelerating filter` (replicate #17)

- 2025 degraded significantly (`avg=$11`, weak significance)
- Half-split near collapse (`H1=$212`, `H2=$-1`)
- Treat as fragile / regime-specific only

---

## Practical Deployment Notes

- Avoid doubling exposure at the same timestamp with highly correlated structures (e.g., `IBF_75w@15:00` and `IBF_60w@15:00` simultaneously) unless you deliberately want leverage.
- If choosing one core strategy: start with `IBF_75w @ 15:15`.
- If building a small basket:
  1. `IBF_75w @ 15:15`
  2. `IBF_75w @ 15:00` **or** `IBF_60w @ 15:00` (choose one)
  3. `IC_35otm_35w @ 14:30` (diversifies structure type)

