"""
Per-trade sizing score module.
Each strategy gets a composite score based on market-regime factors.
Score maps to a sizing multiplier (25%, 50%, 75%, 100% of max budget).

Legacy factors derived from 776-day backtest factor analysis (sizing_factor_research.py).
New strategy factors derived from sizing_factor_research_new.py.
In-sample — must be validated forward.
"""

# ── Per-strategy scoring functions ─────────────────────────────────
# Each takes a dict with keys: prior_dir, prior_1d, fire_count, rv, dow,
# rv_slope, ts_label, vp_ratio, gap_pct, in_prior_week_range, prior_day_range
# Returns integer score.

def score_v3(t):
    """PHOENIX — Prior Day Dir (p=.014), |Ret| (p=.022), Fire Count (p=.022),
    RV level (p=.065), Day of Week (p=.061)."""
    s = 0
    pd = t.get("prior_dir", "")
    if pd == "DOWN":     s += 3
    elif pd == "UP":     s -= 1

    p1d = t.get("prior_1d")
    if p1d is not None:
        ar = abs(p1d)
        if ar < 0.3:     s += 2
        elif ar < 0.7:   s -= 2

    fc = t.get("fire_count", 0) or 0
    if fc >= 4:    s += 3
    elif fc == 3:  s += 2
    elif fc == 2:  s -= 1

    rv = t.get("rv", 0) or 0
    if rv > 18:    s += 2
    elif rv < 12:  s -= 1

    dow = t.get("dow", "")
    if dow == "Wednesday":  s += 1
    elif dow == "Tuesday":  s -= 2
    return s


def score_n15(t):
    """PHOENIX CLEAR — Prior Day Dir (p=.014), Day of Week (p=.034),
    RV Slope (marginal)."""
    s = 0
    pd = t.get("prior_dir", "")
    if pd == "DOWN":     s += 3
    elif pd == "UP":     s -= 2

    dow = t.get("dow", "")
    if dow in ("Wednesday", "Friday"):  s += 1
    elif dow == "Tuesday":              s -= 3

    rvs = t.get("rv_slope", "")
    if rvs == "FALLING":   s += 1
    elif rvs == "RISING":  s -= 1
    return s


def score_v6(t):
    """QUIET REBOUND — VP ratio (directional), |Ret| (p=.062),
    RV Slope (p=.067)."""
    s = 0
    vp = t.get("vp_ratio", 999)
    if vp is None: vp = 999
    if vp < 1.0:     s += 3
    elif vp < 1.3:   s += 1
    elif vp < 1.7:   s -= 1

    p1d = t.get("prior_1d")
    if p1d is not None:
        if abs(p1d) < 0.3:  s += 2
        else:                s -= 1

    rvs = t.get("rv_slope", "")
    if rvs == "RISING":   s += 1
    elif rvs == "STABLE": s -= 1
    return s


def score_v9(t):
    """BREAKOUT STALL — VP 1.3-1.7 (p=.076), |Ret| 0.7-1.2% (p=.039),
    RV Slope (p=.087), Day of Week (directional)."""
    s = 0
    vp = t.get("vp_ratio", 999)
    if vp is None: vp = 999
    if 1.3 <= vp < 1.7:  s += 2
    elif vp >= 1.7:       s -= 2

    p1d = t.get("prior_1d")
    if p1d is not None:
        ar = abs(p1d)
        if 0.7 <= ar < 1.2:   s += 2
        elif 0.3 <= ar < 0.7: s -= 2

    rvs = t.get("rv_slope", "")
    if rvs == "FALLING":   s += 2
    elif rvs == "STABLE":  s -= 1

    dow = t.get("dow", "")
    if dow == "Thursday":    s += 1
    elif dow == "Wednesday": s -= 1
    return s


def score_v12(t):
    """BULL SQUEEZE — VP sweet spot (directional), 5dRet (directional),
    |Ret| (directional). n=11, low confidence."""
    s = 0
    vp = t.get("vp_ratio", 999)
    if vp is None: vp = 999
    if 1.3 <= vp < 1.7:   s += 1
    elif 1.0 <= vp < 1.3:  s -= 2

    r5d = t.get("prior_5d", 0) or 0
    if r5d > 1.5: s += 1

    p1d = t.get("prior_1d")
    if p1d is not None:
        ar = abs(p1d)
        if ar < 0.3:     s += 1
        elif ar < 0.7:   s -= 2
    return s


def score_n17(t):
    """AFTERNOON LOCK — RV Slope (p=.008), Day of Week (p=.030),
    Term Structure (p=.049), Gap (p=.071), Prior Wk Range (p=.069)."""
    s = 0
    rvs = t.get("rv_slope", "")
    if rvs == "FALLING":   s += 3
    elif rvs == "STABLE":  s += 1
    elif rvs == "RISING":  s -= 3

    dow = t.get("dow", "")
    if dow in ("Wednesday", "Thursday"):  s += 2
    elif dow in ("Monday", "Friday"):     s -= 2

    ts = t.get("ts_label", "")
    if ts == "FLAT":       s += 1
    elif ts == "CONTANGO": s -= 1

    gap = t.get("gap_pct")
    if gap is not None:
        if gap < -0.25:   s += 1
        elif gap > 0.25:  s -= 1

    iwr = t.get("in_prior_week_range")
    if iwr is not None:
        if not iwr:  s += 1
        else:        s -= 1
    return s


def score_n18(t):
    """LATE SQUEEZE — Day of Week (p=.014), Prior Day Dir (directional),
    Gap (directional), Prior Day Range (directional)."""
    s = 0
    dow = t.get("dow", "")
    if dow == "Tuesday":     s += 2
    elif dow == "Wednesday": s += 1
    elif dow == "Monday":    s -= 2

    pd = t.get("prior_dir", "")
    if pd == "UP":     s += 1
    elif pd == "DOWN": s -= 1

    gap = t.get("gap_pct")
    if gap is not None:
        if gap > 0.25:    s += 1
        elif gap < -0.25: s -= 1

    pdr = t.get("prior_day_range")
    if pdr is not None and pdr > 1.0:
        s += 1
    return s


# ── New strategy scoring functions (10 strategies) ────────────────
# Factors from sizing_factor_research_new.py analysis.

def score_phx75_power_close(t):
    """Phoenix 75 Power Close — VIX(p=0), RV(p=0), VP(p=.0001), 5dRet(p=.0006),
    TS(p=.0012), |Ret|(p=.0023), DayRng(p=.0023). Strongest signal set."""
    s = 0
    # VIX: 17-20 best, >20 worst (p=0.0000)
    vix = t.get("vix", 0) or 0
    if vix < 14:         s += 1
    elif 14 <= vix < 17: s += 2
    elif 17 <= vix < 20: s += 3
    elif vix >= 20:      s -= 3

    # RV Level: <8 best, >18 worst (p=0.0000)
    rv = t.get("rv", 0) or 0
    if rv < 8:           s += 3
    elif 8 <= rv < 12:   s += 1
    elif 12 <= rv < 18:  s += 0
    elif rv >= 18:       s -= 3

    # VP Ratio: >1.7 best, <1.0 worst (p=0.0001)
    vp = t.get("vp_ratio", 999)
    if vp is None: vp = 999
    if vp > 1.7:         s += 3
    elif 1.3 <= vp < 1.7: s += 1
    elif 1.0 <= vp < 1.3: s -= 1
    elif vp < 1.0:       s -= 2

    # 5d Return: >1.5% best, <-0.5% worst (p=0.0006)
    r5d = t.get("prior_5d", 0) or 0
    if r5d > 1.5:        s += 2
    elif 0.5 <= r5d:     s += 1
    elif r5d < -0.5:     s -= 2

    # Term Structure: FLAT best, INVERTED worst (p=0.0012)
    ts = t.get("ts_label", "")
    if ts == "FLAT":       s += 2
    elif ts == "CONTANGO": s += 0
    elif ts == "INVERTED": s -= 2

    # |Ret|: <0.3% best, 0.3-0.7% worst (p=0.0023)
    p1d = t.get("prior_1d")
    if p1d is not None:
        ar = abs(p1d)
        if ar < 0.3:       s += 2
        elif 0.7 <= ar < 1.2: s += 1
        elif 0.3 <= ar < 0.7: s -= 1

    # Day Range: 0.3-0.6% best, >1.0% worst (p=0.0023)
    pdr = t.get("prior_day_range")
    if pdr is not None:
        if pdr < 0.6:     s += 1
        elif pdr > 1.0:   s -= 1
    return s


def score_phx75_last_hour(t):
    """Phoenix 75 Last Hour — RV(p=.0004), 5dRet(p=.005), |Ret|(p=.005),
    PriorDir(p=.025), VP(p=.035), VIX(p=.050)."""
    s = 0
    # RV Level: 8-12 best, >18 worst (p=0.0004)
    rv = t.get("rv", 0) or 0
    if 8 <= rv < 12:     s += 3
    elif rv < 8:         s += 2
    elif 12 <= rv < 18:  s += 1
    elif rv >= 18:       s -= 3

    # 5d Return: >1.5% best, <-0.5% worst (p=0.0046)
    r5d = t.get("prior_5d", 0) or 0
    if r5d > 1.5:        s += 2
    elif 0.5 <= r5d:     s += 1
    elif r5d < -0.5:     s -= 2

    # |Ret|: <0.3% best, 0.3-0.7% worst (p=0.0049)
    p1d = t.get("prior_1d")
    if p1d is not None:
        ar = abs(p1d)
        if ar < 0.3:       s += 2
        elif 0.7 <= ar < 1.2: s += 1
        elif 0.3 <= ar < 0.7: s -= 2

    # Prior Day Dir: UP best, DOWN worst (p=0.0253)
    pd = t.get("prior_dir", "")
    if pd == "UP":       s += 2
    elif pd == "DOWN":   s -= 1

    # VP Ratio: 1.3-1.7 best, <1.0 worst (p=0.0345)
    vp = t.get("vp_ratio", 999)
    if vp is None: vp = 999
    if 1.3 <= vp < 1.7:  s += 1
    elif vp > 1.7:       s += 1
    elif 1.0 <= vp < 1.3: s += 0
    elif vp < 1.0:       s -= 2

    # VIX: <14 best, >20 worst (p=0.0495)
    vix = t.get("vix", 0) or 0
    if vix < 14:         s += 1
    elif vix >= 20:      s -= 1
    return s


def score_phx75_midday(t):
    """Phoenix 75 Midday — VP(p=.008), RV(p=.012), RVSlope(p=.024), VIX(p=.047)."""
    s = 0
    # VP Ratio: 1.3-1.7 best, <1.0 worst (p=0.0078)
    vp = t.get("vp_ratio", 999)
    if vp is None: vp = 999
    if 1.3 <= vp < 1.7:  s += 3
    elif 1.0 <= vp < 1.3: s += 2
    elif vp > 1.7:       s += 0
    elif vp < 1.0:       s -= 3

    # RV Level: 12-18 best, >18 worst (p=0.0120)
    rv = t.get("rv", 0) or 0
    if 12 <= rv < 18:    s += 2
    elif 8 <= rv < 12:   s += 1
    elif rv < 8:         s += 0
    elif rv >= 18:       s -= 2

    # RV Slope: FALLING best, STABLE worst (p=0.0242)
    rvs = t.get("rv_slope", "")
    if rvs == "FALLING":  s += 2
    elif rvs == "RISING": s += 0
    elif rvs == "STABLE": s -= 1

    # VIX: <14 best, >20 worst (p=0.0468)
    vix = t.get("vix", 0) or 0
    if vix < 14:         s += 1
    elif vix >= 20:      s -= 1
    return s


def score_phx75_early_afternoon(t):
    """Phoenix 75 Early Afternoon — 5dRet(p=.046), VP(p=.050), VIX(p=.061),
    IWR(p=.067), DOW(p=.091). Weakest signal set — lighter weights."""
    s = 0
    # 5d Return: >1.5% best, near-flat worst (p=0.0458)
    r5d = t.get("prior_5d", 0) or 0
    if r5d > 1.5:        s += 2
    elif 0.5 <= r5d:     s += 1
    elif -0.5 <= r5d < 0.5: s -= 1
    elif r5d < -0.5:     s += 0

    # VP Ratio: 1.3-1.7 best, <1.0 worst (p=0.0503)
    vp = t.get("vp_ratio", 999)
    if vp is None: vp = 999
    if 1.3 <= vp < 1.7:  s += 2
    elif vp > 1.7:       s += 1
    elif 1.0 <= vp < 1.3: s += 0
    elif vp < 1.0:       s -= 2

    # VIX: 14-17 best, >20 worst (p=0.0608)
    vix = t.get("vix", 0) or 0
    if 14 <= vix < 17:   s += 1
    elif vix >= 20:      s -= 1

    # In Prior Week Range: Out better (p=0.0674)
    iwr = t.get("in_prior_week_range")
    if iwr is not None:
        if not iwr:      s += 1
        else:            s -= 1

    # Day of Week: Tue/Fri best, Thu worst (p=0.0909)
    dow = t.get("dow", "")
    if dow in ("Tuesday", "Friday"): s += 1
    elif dow == "Thursday":          s -= 1
    return s


def score_phx75_afternoon(t):
    """Phoenix 75 Afternoon — 5dRet(p=.013), RV(p=.026), Gap(p=.041), VP(p=.073)."""
    s = 0
    # 5d Return: >1.5% best, near-flat worst (p=0.0130)
    r5d = t.get("prior_5d", 0) or 0
    if r5d > 1.5:        s += 3
    elif 0.5 <= r5d:     s += 1
    elif -0.5 <= r5d < 0.5: s -= 2
    elif r5d < -0.5:     s += 0

    # RV Level: 12-18 best, >18 worst (p=0.0257)
    rv = t.get("rv", 0) or 0
    if 12 <= rv < 18:    s += 2
    elif 8 <= rv < 12:   s += 1
    elif rv < 8:         s += 0
    elif rv >= 18:       s -= 2

    # Gap Direction: DOWN best, FLAT worst (p=0.0414)
    gap = t.get("gap_pct")
    if gap is not None:
        if gap < -0.25:  s += 2
        elif gap > 0.25: s += 1
        else:            s -= 1

    # VP Ratio: 1.0-1.3 best, <1.0 worst (p=0.0728)
    vp = t.get("vp_ratio", 999)
    if vp is None: vp = 999
    if 1.0 <= vp < 1.3:  s += 1
    elif 1.3 <= vp < 1.7: s += 1
    elif vp < 1.0:       s -= 1
    return s


def score_fb60_final_bell(t):
    """Firebird 60 Final Bell — VIX(p=0), RV(p=0), 5dRet(p=.0001),
    VP(p=.037), PriorDir(p=.050)."""
    s = 0
    # VIX Level: <14 best, >20 worst (p=0.0000)
    vix = t.get("vix", 0) or 0
    if vix < 14:         s += 3
    elif 14 <= vix < 17: s += 1
    elif 17 <= vix < 20: s += 0
    elif vix >= 20:      s -= 3

    # RV Level: 8-12 best, >18 worst (p=0.0000)
    rv = t.get("rv", 0) or 0
    if 8 <= rv < 12:     s += 3
    elif rv < 8:         s += 1
    elif 12 <= rv < 18:  s += 0
    elif rv >= 18:       s -= 2

    # 5d Return: >1.5% best, <-0.5% worst (p=0.0001)
    r5d = t.get("prior_5d", 0) or 0
    if r5d > 1.5:        s += 2
    elif 0.5 <= r5d:     s += 1
    elif r5d < -0.5:     s -= 2

    # VP Ratio: >1.7 best, <1.0 worst (p=0.0370)
    vp = t.get("vp_ratio", 999)
    if vp is None: vp = 999
    if vp > 1.7:         s += 1
    elif 1.3 <= vp < 1.7: s += 1
    elif vp < 1.0:       s -= 1

    # Prior Day Dir: UP best, DOWN worst (p=0.0498)
    pd = t.get("prior_dir", "")
    if pd == "UP":       s += 1
    elif pd == "DOWN":   s -= 1
    return s


def score_fb60_last_hour(t):
    """Firebird 60 Last Hour — RV(p=.001), 5dRet(p=.001), VIX(p=.003),
    PriorDir(p=.011), TS(p=.030), V9D/V(p=.045)."""
    s = 0
    # RV Level: 8-12 best, >18 worst (p=0.0011)
    rv = t.get("rv", 0) or 0
    if 8 <= rv < 12:     s += 3
    elif rv < 8:         s += 2
    elif 12 <= rv < 18:  s += 0
    elif rv >= 18:       s -= 3

    # 5d Return: >1.5% best, <-0.5% worst (p=0.0012)
    r5d = t.get("prior_5d", 0) or 0
    if r5d > 1.5:        s += 2
    elif 0.5 <= r5d:     s += 1
    elif r5d < -0.5:     s -= 2

    # VIX: <14 best, >20 worst (p=0.0034)
    vix = t.get("vix", 0) or 0
    if vix < 14:         s += 2
    elif 14 <= vix < 17: s += 1
    elif vix >= 20:      s -= 2

    # Prior Day Dir: UP best, DOWN worst (p=0.0113)
    pd = t.get("prior_dir", "")
    if pd == "UP":       s += 2
    elif pd == "DOWN":   s -= 1

    # Term Structure: FLAT best, INVERTED worst (p=0.0295)
    ts = t.get("ts_label", "")
    if ts == "FLAT":       s += 1
    elif ts == "INVERTED": s -= 1

    # VIX9D/VIX: 0.85-0.95 best, >1.05 worst (p=0.0449)
    v9d_ratio = t.get("vix9d_vix_ratio")
    if v9d_ratio is not None:
        if 0.85 <= v9d_ratio < 0.95: s += 1
        elif v9d_ratio >= 1.05:      s -= 1
    return s


def score_fb60_midday(t):
    """Firebird 60 Midday — VP(p=.007), RV(p=.014), V9D/V(p=.019),
    VIX(p=.031), RVSlope(p=.032), 5dRet(p=.047)."""
    s = 0
    # VP Ratio: 1.3-1.7 best, <1.0 worst (p=0.0070)
    vp = t.get("vp_ratio", 999)
    if vp is None: vp = 999
    if 1.3 <= vp < 1.7:  s += 3
    elif 1.0 <= vp < 1.3: s += 2
    elif vp > 1.7:       s += 0
    elif vp < 1.0:       s -= 2

    # RV Level: 12-18 best, >18 worst (p=0.0144)
    rv = t.get("rv", 0) or 0
    if 12 <= rv < 18:    s += 2
    elif 8 <= rv < 12:   s += 2
    elif rv < 8:         s += 0
    elif rv >= 18:       s -= 2

    # VIX9D/VIX: 0.95-1.05 best, >1.05 worst (p=0.0194)
    v9d_ratio = t.get("vix9d_vix_ratio")
    if v9d_ratio is not None:
        if 0.95 <= v9d_ratio < 1.05: s += 1
        elif v9d_ratio >= 1.05:      s -= 2

    # VIX: <14 best, >20 worst (p=0.0311)
    vix = t.get("vix", 0) or 0
    if vix < 14:         s += 1
    elif vix >= 20:      s -= 1

    # RV Slope: FALLING best, STABLE worst (p=0.0316)
    rvs = t.get("rv_slope", "")
    if rvs == "FALLING":  s += 2
    elif rvs == "RISING": s += 0
    elif rvs == "STABLE": s -= 1

    # 5d Return: >1.5% best, near-flat worst (p=0.0473)
    r5d = t.get("prior_5d", 0) or 0
    if r5d > 1.5:        s += 1
    elif r5d < -0.5:     s += 0
    elif -0.5 <= r5d < 0.5: s -= 1
    return s


def score_ic35_condor(t):
    """Ironclad 35 Condor — V9D/V(p=.0006), DOW(p=.018), TS(p=.023),
    RV(p=.042). NOTE: V9D/V>1.05 is BEST (opposite of other strats)."""
    s = 0
    # VIX9D/VIX: >1.05 best, <0.85 worst (p=0.0006) — INVERTED vs other strats
    v9d_ratio = t.get("vix9d_vix_ratio")
    if v9d_ratio is not None:
        if v9d_ratio >= 1.05:       s += 3
        elif 0.95 <= v9d_ratio < 1.05: s += 1
        elif 0.85 <= v9d_ratio < 0.95: s += 0
        elif v9d_ratio < 0.85:      s -= 3

    # Day of Week: Wednesday best, Tuesday worst (p=0.0183)
    dow = t.get("dow", "")
    if dow == "Wednesday":  s += 2
    elif dow == "Thursday": s += 1
    elif dow == "Tuesday":  s -= 2

    # Term Structure: INVERTED best, CONTANGO worst (p=0.0229)
    ts = t.get("ts_label", "")
    if ts == "INVERTED":   s += 2
    elif ts == "FLAT":     s += 1
    elif ts == "CONTANGO": s -= 1

    # RV Level: <8 best, 8-12 worst (p=0.0419)
    rv = t.get("rv", 0) or 0
    if rv < 8:           s += 2
    elif 12 <= rv < 18:  s += 1
    elif rv >= 18:       s += 0
    elif 8 <= rv < 12:   s -= 1
    return s



# ── Score function registry ────────────────────────────────────────

SCORE_FUNCTIONS = {
    # Legacy strategies
    "v3":  score_v3,
    "n15": score_n15,
    "v6":  score_v6,
    "v7":  None,        # n=5, no reliable factors
    "v9":  score_v9,
    "v12": score_v12,
    "n17": score_n17,
    "n18": score_n18,
    # New strategies
    "Phoenix 75 Power Close":       score_phx75_power_close,
    "Phoenix 75 Last Hour":         score_phx75_last_hour,
    "Phoenix 75 Midday":            score_phx75_midday,
    "Phoenix 75 Early Afternoon":   score_phx75_early_afternoon,
    "Phoenix 75 Afternoon":         score_phx75_afternoon,
    "Firebird 60 Final Bell":   score_fb60_final_bell,
    "Firebird 60 Last Hour":    score_fb60_last_hour,
    "Firebird 60 Midday":       score_fb60_midday,
    "Ironclad 35 Condor":       score_ic35_condor,
}

# ── Per-strategy score thresholds ──────────────────────────────────
# Derived from quartile analysis of historical score distributions.
# (t25, t50, t75) — scores at or below t25 get 25% sizing, etc.
# Legacy thresholds calibrated from the 776-day backtest.
# New strategy thresholds will be calibrated from the backtest below.

SCORE_THRESHOLDS = {
    # Legacy
    "v3":  (-2, 0, 3),   # range [-5, 11]
    "n15": (-2, -1, 2),  # range [-6, 5]
    "v6":  (-3, 0, 2),   # range [-3, 6]
    "v9":  (-3, -1, 3),  # range [-6, 7]
    "v12": (-3, 1, 2),   # range [-4, 3]
    "n17": (-2, 0, 3),   # range [-8, 6]
    "n18": (0, 1, 2),    # range [-4, 4]
    # New — calibrated from backtest quartile analysis
    "Phoenix 75 Power Close":       (-3, 3, 6),   # range [-14, 15]
    "Phoenix 75 Last Hour":         (-2, 3, 6),   # range [-11, 11]
    "Phoenix 75 Midday":            (0, 3, 4),    # range [-7, 7]
    "Phoenix 75 Early Afternoon":   (-1, 1, 3),   # range [-5, 7]
    "Phoenix 75 Afternoon":         (-1, 1, 2),   # range [-6, 5]
    "Firebird 60 Final Bell":      (-2, 2, 5),   # range [-9, 10]
    "Firebird 60 Last Hour":       (-1, 3, 6),   # range [-9, 11]
    "Firebird 60 Midday":          (0, 3, 5),    # range [-8, 10]
    "Ironclad 35 Condor":          (0, 1, 3),    # range [-4, 6]
}


def score_to_multiplier(score, ver):
    """Map composite score to sizing multiplier (0.25, 0.50, 0.75, or 1.00)."""
    thresholds = SCORE_THRESHOLDS.get(ver)
    if thresholds is None:
        return 1.0   # no scoring for this strategy
    t25, t50, t75 = thresholds
    if score <= t25:  return 0.25
    if score <= t50:  return 0.50
    if score <= t75:  return 0.75
    return 1.00


def compute_sizing(ver, context):
    """
    Main entry point: given a strategy version and trade context dict,
    return (multiplier, score).

    context keys used (provide what's available, missing is OK):
      prior_dir, prior_1d, fire_count, rv, dow, rv_slope, ts_label,
      vp_ratio, gap_pct, in_prior_week_range, prior_day_range, prior_5d
    """
    fn = SCORE_FUNCTIONS.get(ver)
    if fn is None:
        return 1.0, 0
    score = fn(context)
    mult = score_to_multiplier(score, ver)
    return mult, score
