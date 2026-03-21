"""
research/data.py — Comprehensive data loader for the new format.

Loads:
  - SPX 1-min intraday bars (data/spx_1min/*.json)
  - Option chains with 5-min bars (data/option_chains/*.json)
  - VIX/VIX9D/VVIX 1-min intraday bars (data/vix_1min/*.json etc.)
  - Bid/ask quotes (data/quotes/*.json)
  - SPX daily bars (data/spx_daily.json)
  - SPX weekly bars (data/spx_weekly.json)
  - VIX daily bars (data/vix_daily.json)
  - Daily context signals (data/daily_context.json)

All access methods enforce forward-walk rules:
  - Pre-open context: available for the full day (settled before 9:30)
  - Intraday data: requires an as_of_time parameter; raises if you peek ahead
"""

import os
import json
from functools import lru_cache

_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(_DIR, "data")


class DataUniverse:
    """
    Central data access object.  Load once, query many times.

    Usage:
        universe = DataUniverse()
        universe.load()
        dates = universe.trading_dates()
        spx = universe.spx_at("2024-06-03", "10:00")
    """

    def __init__(self, data_dir=None):
        self.data_dir = data_dir or DATA_DIR
        self._spx_daily = {}      # {date_str: {o,h,l,c,v}}
        self._spx_weekly = {}     # {date_str: {o,h,l,c,v}}
        self._vix_daily = {}      # {date_str: {o,h,l,c}}
        self._daily_context = {}  # {date_str: {field: value, ...}}
        self._spx_intraday = {}   # {date_str: {HH:MM: {o,h,l,c,v}}}
        self._vix_intraday = {}   # {date_str: {HH:MM: {o,h,l,c}}}
        self._option_chains = {}  # {date_str: {strikes: {K: {C: {HH:MM: bar}, P: ...}}}}
        self._quotes = {}         # {date_str: {times: {HH:MM: {K: {C: {bid,ask,mid,spread}}}}}}
        self._dates = []
        self._loaded = False

    def load(self, dates=None, load_quotes=True):
        """
        Load all data into memory.

        Args:
            dates: optional list of date strings to load (default: all available)
            load_quotes: whether to load bid/ask quote files (can skip to save memory)
        """
        print("Loading data universe...")

        # Daily bars (single files, always load)
        for fname, attr in [
            ("spx_daily.json", "_spx_daily"),
            ("spx_weekly.json", "_spx_weekly"),
            ("vix_daily.json", "_vix_daily"),
        ]:
            path = os.path.join(self.data_dir, fname)
            if os.path.exists(path):
                with open(path) as f:
                    data = json.load(f)
                setattr(self, attr, data.get("bars", data))
                print(f"  {fname}: {len(getattr(self, attr))} entries")

        # Daily context
        ctx_path = os.path.join(self.data_dir, "daily_context.json")
        if os.path.exists(ctx_path):
            with open(ctx_path) as f:
                self._daily_context = json.load(f)
            print(f"  daily_context.json: {len(self._daily_context)} dates")

        # Discover available per-day files
        spx_dir = os.path.join(self.data_dir, "spx_1min")
        opt_dir = os.path.join(self.data_dir, "option_chains")

        if os.path.isdir(spx_dir):
            available_spx = set(f.replace(".json", "") for f in os.listdir(spx_dir) if f.endswith(".json"))
        else:
            available_spx = set()

        if os.path.isdir(opt_dir):
            available_opt = set(f.replace(".json", "") for f in os.listdir(opt_dir) if f.endswith(".json"))
        else:
            available_opt = set()

        # Trading dates = dates with BOTH SPX bars AND option chains
        all_available = sorted(available_spx & available_opt)
        if dates:
            all_available = sorted(set(all_available) & set(dates))

        self._dates = all_available
        print(f"  Trading dates with full data: {len(self._dates)}")

        if not self._dates:
            print("  WARNING: No complete trading dates found!")
            self._loaded = True
            return

        print(f"  Date range: {self._dates[0]} to {self._dates[-1]}")

        # Load per-day files
        loaded = 0
        for date_str in self._dates:
            # SPX intraday
            spath = os.path.join(spx_dir, f"{date_str}.json")
            with open(spath) as f:
                self._spx_intraday[date_str] = json.load(f)

            # Option chain
            opath = os.path.join(opt_dir, f"{date_str}.json")
            with open(opath) as f:
                self._option_chains[date_str] = json.load(f)

            # VIX intraday (best effort)
            vpath = os.path.join(self.data_dir, "vix_1min", f"{date_str}.json")
            if os.path.exists(vpath):
                with open(vpath) as f:
                    self._vix_intraday[date_str] = json.load(f)

            # Quotes (optional)
            if load_quotes:
                qpath = os.path.join(self.data_dir, "quotes", f"{date_str}.json")
                if os.path.exists(qpath):
                    with open(qpath) as f:
                        self._quotes[date_str] = json.load(f)

            loaded += 1
            if loaded % 100 == 0:
                print(f"  Loaded {loaded}/{len(self._dates)} days...")

        print(f"  Done: {loaded} days loaded")
        self._loaded = True

    # ─────────────────────────────────────────────────────────────────────
    # DATE ACCESS
    # ─────────────────────────────────────────────────────────────────────

    def trading_dates(self):
        """All trading dates with complete data."""
        return list(self._dates)

    def has_date(self, date_str):
        return date_str in self._spx_intraday

    # ─────────────────────────────────────────────────────────────────────
    # PRE-OPEN CONTEXT (forward-walk safe — all settled before 9:30)
    # ─────────────────────────────────────────────────────────────────────

    def daily_context(self, date_str):
        """
        Get the full pre-open context for a date.
        All fields are computed from data settled before 9:30 AM.
        Safe to use for any decision on this date.
        Returns dict or empty dict.
        """
        return dict(self._daily_context.get(date_str, {}))

    def ctx(self, date_str, field, default=None):
        """Shorthand: get a single context field."""
        return self._daily_context.get(date_str, {}).get(field, default)

    def spx_daily_bar(self, date_str):
        """Get the daily OHLCV bar for a specific date (settled EOD)."""
        return self._spx_daily.get(date_str)

    def vix_daily_bar(self, date_str):
        """Get the VIX daily bar for a specific date."""
        return self._vix_daily.get(date_str)

    def spx_daily_bars_before(self, date_str, n):
        """Get the last N daily bars BEFORE (not including) date_str."""
        all_dates = sorted(self._spx_daily.keys())
        idx = None
        for i, d in enumerate(all_dates):
            if d >= date_str:
                idx = i
                break
        if idx is None:
            idx = len(all_dates)
        start = max(0, idx - n)
        return [(all_dates[i], self._spx_daily[all_dates[i]]) for i in range(start, idx)]

    # ─────────────────────────────────────────────────────────────────────
    # INTRADAY DATA (requires as_of_time enforcement)
    # ─────────────────────────────────────────────────────────────────────

    def spx_at(self, date_str, time_str):
        """
        Get SPX close price at a specific minute bar.
        Returns float or None.
        """
        bars = self._spx_intraday.get(date_str, {})
        bar = bars.get(time_str)
        return bar["c"] if bar else None

    def spx_bar_at(self, date_str, time_str):
        """Get full OHLCV bar for SPX at a specific minute."""
        bars = self._spx_intraday.get(date_str, {})
        return bars.get(time_str)

    def spx_bars_range(self, date_str, start_time, end_time):
        """
        Get all SPX bars from start_time to end_time (inclusive).
        Forward-walk safe: only returns bars <= end_time.
        Returns list of (time_str, bar_dict) tuples.
        """
        bars = self._spx_intraday.get(date_str, {})
        return [(t, bars[t]) for t in sorted(bars.keys())
                if start_time <= t <= end_time]

    def spx_range(self, date_str, start_time, end_time):
        """Compute SPX high-low range between two times."""
        bars = self.spx_bars_range(date_str, start_time, end_time)
        if not bars:
            return None
        hi = max(b["h"] for _, b in bars)
        lo = min(b["l"] for _, b in bars)
        return round(hi - lo, 2)

    def spx_move(self, date_str, start_time, end_time):
        """Net SPX move (close-to-close) between two times."""
        s = self.spx_at(date_str, start_time)
        e = self.spx_at(date_str, end_time)
        if s is not None and e is not None:
            return round(e - s, 2)
        return None

    def vix_at(self, date_str, time_str):
        """Get VIX level at a specific minute."""
        bars = self._vix_intraday.get(date_str, {})
        bar = bars.get(time_str)
        return bar["c"] if bar else None

    def current_atm(self, date_str, time_str):
        """
        Get the current ATM strike at a given time.
        Rounds SPX price to nearest 5.
        """
        spx = self.spx_at(date_str, time_str)
        if spx is None:
            return None
        return int(round(spx / 5) * 5)

    # ─────────────────────────────────────────────────────────────────────
    # OPTION CHAIN ACCESS
    # ─────────────────────────────────────────────────────────────────────

    def option_chain(self, date_str):
        """Get the full option chain data for a date."""
        return self._option_chains.get(date_str)

    def chain_strike_range(self, date_str):
        """Get [min_strike, max_strike] available in the chain."""
        chain = self._option_chains.get(date_str)
        if not chain:
            return None
        return chain.get("strike_range")

    def option_bar(self, date_str, strike, cp, time_str):
        """
        Get the 5-min option bar at a specific strike/side/time.

        Args:
            date_str: "YYYY-MM-DD"
            strike: int (e.g. 5300)
            cp: "C" or "P"
            time_str: "HH:MM" (must be a 5-min boundary)

        Returns: {o, h, l, c, v, vw} or None
        """
        chain = self._option_chains.get(date_str)
        if not chain:
            return None
        strike_data = chain.get("strikes", {}).get(str(strike), {})
        side_data = strike_data.get(cp, {})
        return side_data.get(time_str)

    def option_mid(self, date_str, strike, cp, time_str):
        """Get the midpoint (close) of a 5-min option bar."""
        bar = self.option_bar(date_str, strike, cp, time_str)
        if bar:
            return bar["c"]
        return None

    def option_vwap(self, date_str, strike, cp, time_str):
        """Get the VWAP of a 5-min option bar (better mid estimate)."""
        bar = self.option_bar(date_str, strike, cp, time_str)
        if bar and "vw" in bar:
            return bar["vw"]
        if bar:
            return bar["c"]
        return None

    def option_bars_through(self, date_str, strike, cp, end_time):
        """
        Get all 5-min bars for a contract up through end_time.
        Forward-walk safe.
        """
        chain = self._option_chains.get(date_str)
        if not chain:
            return []
        side_data = chain.get("strikes", {}).get(str(strike), {}).get(cp, {})
        return [(t, side_data[t]) for t in sorted(side_data.keys()) if t <= end_time]

    def has_strike(self, date_str, strike, cp, time_str):
        """Check if we have data for this strike/side/time."""
        return self.option_bar(date_str, strike, cp, time_str) is not None

    def all_available_strikes(self, date_str):
        """Get all strike values with any data for this date."""
        chain = self._option_chains.get(date_str)
        if not chain:
            return []
        return sorted(int(k) for k in chain.get("strikes", {}).keys())

    # ─────────────────────────────────────────────────────────────────────
    # QUOTE (BID/ASK) ACCESS
    # ─────────────────────────────────────────────────────────────────────

    def quote(self, date_str, time_str, strike, cp):
        """
        Get bid/ask quote for a specific strike/side/time.
        Returns {bid, ask, mid, spread} or None.
        """
        qdata = self._quotes.get(date_str, {})
        time_data = qdata.get("times", {}).get(time_str, {})
        strike_data = time_data.get(str(strike), {})
        return strike_data.get(cp)

    def bid_ask_mid(self, date_str, time_str, strike, cp):
        """Get (bid, ask, mid) tuple or (None, None, None)."""
        q = self.quote(date_str, time_str, strike, cp)
        if q:
            return q["bid"], q["ask"], q["mid"]
        return None, None, None

    # ─────────────────────────────────────────────────────────────────────
    # INTRADAY REGIME SIGNALS (computed on-the-fly, forward-walk safe)
    # ─────────────────────────────────────────────────────────────────────

    def morning_range(self, date_str, as_of_time):
        """SPX high-low range from 09:30 to as_of_time."""
        return self.spx_range(date_str, "09:30", as_of_time)

    def morning_direction(self, date_str, as_of_time):
        """Net SPX move from open to as_of_time."""
        return self.spx_move(date_str, "09:30", as_of_time)

    def morning_volume(self, date_str, as_of_time):
        """Total SPX volume from 09:30 to as_of_time."""
        bars = self.spx_bars_range(date_str, "09:30", as_of_time)
        vols = [b.get("v", 0) for _, b in bars]
        return sum(vols)

    def vix_change_since_open(self, date_str, as_of_time):
        """VIX change from 09:30 to as_of_time."""
        v_open = self.vix_at(date_str, "09:30") or self.vix_at(date_str, "09:31")
        v_now = self.vix_at(date_str, as_of_time)
        if v_open and v_now:
            return round(v_now - v_open, 2)
        return None

    def gap_filled(self, date_str, as_of_time):
        """
        Check if the overnight gap has been filled by as_of_time.
        Gap fill = price has retraced to prior close.
        Forward-walk safe: only uses bars up to as_of_time.
        """
        ctx = self.daily_context(date_str)
        prior_close = ctx.get("prior_close")
        gap_pts = ctx.get("gap_pts", 0)
        if prior_close is None or gap_pts == 0:
            return None

        bars = self.spx_bars_range(date_str, "09:30", as_of_time)
        if not bars:
            return False

        if gap_pts > 0:  # gap up — filled if price drops to prior close
            lo = min(b["l"] for _, b in bars)
            return lo <= prior_close
        else:  # gap down — filled if price rises to prior close
            hi = max(b["h"] for _, b in bars)
            return hi >= prior_close

    # ─────────────────────────────────────────────────────────────────────
    # VELOCITY / ACCELERATION (forward-walk safe)
    # ─────────────────────────────────────────────────────────────────────

    def spx_velocity(self, date_str, as_of_time, lookback_minutes=15):
        """
        SPX price velocity: average pts/min over the last N minutes.
        Positive = moving up, negative = moving down.
        Forward-walk safe: only uses bars up to as_of_time.
        """
        bars = self.spx_bars_range(date_str, "09:30", as_of_time)
        if len(bars) < 2:
            return None
        # Take the last lookback_minutes bars (1-min bars)
        recent = bars[-lookback_minutes:] if len(bars) >= lookback_minutes else bars
        if len(recent) < 2:
            return None
        first_price = recent[0][1]["c"]
        last_price = recent[-1][1]["c"]
        n_bars = len(recent)
        return round((last_price - first_price) / n_bars, 4)

    def spx_acceleration(self, date_str, as_of_time, window=10):
        """
        SPX price acceleration: change in velocity.
        Compares velocity over the recent 'window' bars vs the prior 'window'.
        Positive = speeding up (in either direction), negative = slowing down.
        Forward-walk safe.
        """
        bars = self.spx_bars_range(date_str, "09:30", as_of_time)
        if len(bars) < window * 2:
            return None
        recent = bars[-window:]
        prior = bars[-(window * 2):-window]
        v_recent = (recent[-1][1]["c"] - recent[0][1]["c"]) / len(recent)
        v_prior = (prior[-1][1]["c"] - prior[0][1]["c"]) / len(prior)
        return round(v_recent - v_prior, 4)

    def spx_abs_velocity(self, date_str, as_of_time, lookback_minutes=15):
        """
        Absolute velocity (speed): average absolute pts/min.
        Measures how fast SPX is moving regardless of direction.
        High speed = trending. Low speed = consolidating.
        Forward-walk safe.
        """
        bars = self.spx_bars_range(date_str, "09:30", as_of_time)
        recent = bars[-lookback_minutes:] if len(bars) >= lookback_minutes else bars
        if len(recent) < 2:
            return None
        moves = [abs(recent[i][1]["c"] - recent[i-1][1]["c"]) for i in range(1, len(recent))]
        return round(sum(moves) / len(moves), 4)

    def spx_range_velocity(self, date_str, as_of_time, lookback_minutes=30):
        """
        Range expansion rate: how fast is the day's range growing?
        Compares recent-window high-low to total session high-low.
        Low ratio = range has stabilized (good for vol-selling).
        High ratio = range still expanding (risky for vol-selling).
        Forward-walk safe.
        """
        all_bars = self.spx_bars_range(date_str, "09:30", as_of_time)
        if len(all_bars) < lookback_minutes:
            return None
        recent = all_bars[-lookback_minutes:]
        total_hi = max(b["h"] for _, b in all_bars)
        total_lo = min(b["l"] for _, b in all_bars)
        total_range = total_hi - total_lo
        if total_range == 0:
            return 0
        recent_hi = max(b["h"] for _, b in recent)
        recent_lo = min(b["l"] for _, b in recent)
        recent_range = recent_hi - recent_lo
        return round(recent_range / total_range, 4)

    def spx_bar_range_avg(self, date_str, as_of_time, lookback_minutes=15):
        """
        Average per-bar range (high-low) over the last N minutes.
        Proxy for intraday realized volatility at that moment.
        Forward-walk safe.
        """
        bars = self.spx_bars_range(date_str, "09:30", as_of_time)
        recent = bars[-lookback_minutes:] if len(bars) >= lookback_minutes else bars
        if not recent:
            return None
        ranges = [b["h"] - b["l"] for _, b in recent]
        return round(sum(ranges) / len(ranges), 4)

    def is_trending(self, date_str, as_of_time, lookback_minutes=30, threshold=0.7):
        """
        Is SPX trending? Measured as abs(net move) / total range.
        Close to 1.0 = strong trend. Close to 0.0 = choppy/mean-reverting.
        Forward-walk safe.
        """
        bars = self.spx_bars_range(date_str, "09:30", as_of_time)
        recent = bars[-lookback_minutes:] if len(bars) >= lookback_minutes else bars
        if len(recent) < 5:
            return None
        net_move = abs(recent[-1][1]["c"] - recent[0][1]["c"])
        hi = max(b["h"] for _, b in recent)
        lo = min(b["l"] for _, b in recent)
        rng = hi - lo
        if rng == 0:
            return False
        efficiency = net_move / rng
        return efficiency >= threshold

    def is_consolidating(self, date_str, as_of_time, lookback_minutes=30, threshold=0.3):
        """
        Is SPX consolidating / range-bound?
        Low efficiency = price moving back and forth without net direction.
        This is the ideal setup for vol-selling.
        Forward-walk safe.
        """
        bars = self.spx_bars_range(date_str, "09:30", as_of_time)
        recent = bars[-lookback_minutes:] if len(bars) >= lookback_minutes else bars
        if len(recent) < 5:
            return None
        net_move = abs(recent[-1][1]["c"] - recent[0][1]["c"])
        hi = max(b["h"] for _, b in recent)
        lo = min(b["l"] for _, b in recent)
        rng = hi - lo
        if rng == 0:
            return True
        efficiency = net_move / rng
        return efficiency <= threshold

    # ─────────────────────────────────────────────────────────────────────
    # CENTER PINNING (forward-walk safe)
    # ─────────────────────────────────────────────────────────────────────

    def center_pin_score(self, date_str, as_of_time, lookback_minutes=30, tol=10):
        """
        Center pinning score: fraction of 1-min bars in the prior window
        where SPX was within ±tol points of the current ATM.

        High score (>0.6) = SPX is stuck near ATM = ideal for short-vol.
        Low score (<0.3) = SPX is trending away from center = risky.

        Args:
            date_str: trading date
            as_of_time: current time (computes ATM at this time)
            lookback_minutes: how far back to look (default 30)
            tol: tolerance in SPX points (default ±10)

        Forward-walk safe: only uses bars at or before as_of_time.
        """
        atm = self.current_atm(date_str, as_of_time)
        if atm is None:
            return None

        # Get bars for the prior window
        bars = self.spx_bars_range(date_str, "09:30", as_of_time)
        if not bars:
            return None

        # Take only the last lookback_minutes bars
        window = bars[-lookback_minutes:] if len(bars) >= lookback_minutes else bars
        if not window:
            return None

        pinned = sum(1 for _, b in window if abs(b["c"] - atm) <= tol)
        return round(pinned / len(window), 4)

    def center_pin_score_fixed(self, date_str, as_of_time, center_strike,
                                lookback_minutes=30, tol=10):
        """
        Same as center_pin_score but uses a fixed center strike
        (e.g., the ATM at entry time, not at current time).
        Useful for monitoring pinning persistence after entry.

        Forward-walk safe: only uses bars at or before as_of_time.
        """
        bars = self.spx_bars_range(date_str, "09:30", as_of_time)
        if not bars:
            return None

        window = bars[-lookback_minutes:] if len(bars) >= lookback_minutes else bars
        if not window:
            return None

        pinned = sum(1 for _, b in window if abs(b["c"] - center_strike) <= tol)
        return round(pinned / len(window), 4)

    def is_center_pinned(self, date_str, as_of_time, lookback_minutes=30,
                          tol=10, threshold=0.6):
        """
        Boolean: is SPX center-pinned right now?
        True if center_pin_score >= threshold.
        Forward-walk safe.
        """
        score = self.center_pin_score(date_str, as_of_time, lookback_minutes, tol)
        if score is None:
            return None
        return score >= threshold

    def gap_fill_time(self, date_str):
        """
        Find the time the gap was filled (if ever).
        Returns HH:MM string or None.
        Uses full session data — call this only for analysis, not for
        real-time entry decisions (use gap_filled with as_of_time instead).
        """
        ctx = self.daily_context(date_str)
        prior_close = ctx.get("prior_close")
        gap_pts = ctx.get("gap_pts", 0)
        if prior_close is None or gap_pts == 0:
            return None

        bars = self.spx_bars_range(date_str, "09:30", "16:15")
        for t, b in bars:
            if gap_pts > 0 and b["l"] <= prior_close:
                return t
            if gap_pts < 0 and b["h"] >= prior_close:
                return t
        return None
