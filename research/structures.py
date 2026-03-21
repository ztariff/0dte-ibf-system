"""
research/structures.py — Option structure definitions and pricing.

Supports:
  - ATM Iron Butterfly (IBF): short straddle + symmetric long wings
  - OTM Iron Condor (IC): short strangle + long wings
  - Broken-Wing Butterfly (BWB): asymmetric wings
  - Vertical Spreads: single-side credit spreads (bear call, bull put)
  - Custom: arbitrary 4-leg or 2-leg combinations

All structures are defined by absolute strikes, not offsets.
Pricing uses real option bar data from the DataUniverse.
No Black-Scholes anywhere — every price is a real market print.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class Leg:
    """A single option leg."""
    strike: int
    cp: str          # "C" or "P"
    side: str        # "LONG" or "SHORT"
    qty: int = 1

    @property
    def sign(self):
        """+1 for long, -1 for short (from the position holder's perspective)."""
        return 1 if self.side == "LONG" else -1


@dataclass
class Structure:
    """
    A multi-leg option structure defined by its legs.
    All strikes are absolute (e.g., 5300, not ATM+20).
    """
    name: str
    legs: List[Leg]
    entry_time: str = ""   # HH:MM of intended entry
    date: str = ""

    @property
    def max_risk(self):
        """
        Approximate max risk per spread.
        For iron butterfly/condor: max wing width - credit.
        Must be computed after pricing entry.
        """
        # This is set by the pricing engine after entry
        return getattr(self, '_max_risk', None)

    @property
    def strikes(self):
        return [leg.strike for leg in self.legs]

    @property
    def min_strike(self):
        return min(self.strikes)

    @property
    def max_strike(self):
        return max(self.strikes)


# ─────────────────────────────────────────────────────────────────────────────
# STRUCTURE BUILDERS
# ─────────────────────────────────────────────────────────────────────────────

def iron_butterfly(atm: int, wing_width: int) -> Structure:
    """
    ATM Iron Butterfly.
    Short straddle at ATM + long wings at ATM ± wing_width.
    Max profit = credit received. Max loss = wing_width - credit.
    """
    return Structure(
        name=f"IBF_{atm}_{wing_width}w",
        legs=[
            Leg(atm, "C", "SHORT"),       # short call
            Leg(atm, "P", "SHORT"),       # short put
            Leg(atm + wing_width, "C", "LONG"),  # long call wing
            Leg(atm - wing_width, "P", "LONG"),  # long put wing
        ]
    )


def iron_condor(atm: int, short_offset: int, wing_width: int) -> Structure:
    """
    OTM Iron Condor.
    Short call at ATM+short_offset, short put at ATM-short_offset,
    Long call at ATM+short_offset+wing_width, long put at ATM-short_offset-wing_width.
    """
    sc = atm + short_offset
    sp = atm - short_offset
    lc = sc + wing_width
    lp = sp - wing_width
    return Structure(
        name=f"IC_{atm}_{short_offset}otm_{wing_width}w",
        legs=[
            Leg(sc, "C", "SHORT"),
            Leg(sp, "P", "SHORT"),
            Leg(lc, "C", "LONG"),
            Leg(lp, "P", "LONG"),
        ]
    )


def broken_wing_butterfly(atm: int, call_wing: int, put_wing: int) -> Structure:
    """
    Broken-Wing Butterfly.
    Short straddle at ATM, asymmetric long wings.
    Wider wing on one side provides directional bias.
    """
    return Structure(
        name=f"BWB_{atm}_{call_wing}c_{put_wing}p",
        legs=[
            Leg(atm, "C", "SHORT"),
            Leg(atm, "P", "SHORT"),
            Leg(atm + call_wing, "C", "LONG"),
            Leg(atm - put_wing, "P", "LONG"),
        ]
    )


def bear_call_spread(short_strike: int, long_strike: int) -> Structure:
    """Credit bear call spread (sell lower, buy higher)."""
    return Structure(
        name=f"BCS_{short_strike}_{long_strike}",
        legs=[
            Leg(short_strike, "C", "SHORT"),
            Leg(long_strike, "C", "LONG"),
        ]
    )


def bull_put_spread(short_strike: int, long_strike: int) -> Structure:
    """Credit bull put spread (sell higher, buy lower)."""
    return Structure(
        name=f"BPS_{short_strike}_{long_strike}",
        legs=[
            Leg(short_strike, "P", "SHORT"),
            Leg(long_strike, "P", "LONG"),
        ]
    )


def short_strangle(call_strike: int, put_strike: int) -> Structure:
    """Naked short strangle (no wings — undefined risk)."""
    return Structure(
        name=f"STRG_{put_strike}_{call_strike}",
        legs=[
            Leg(call_strike, "C", "SHORT"),
            Leg(put_strike, "P", "SHORT"),
        ]
    )


# ─────────────────────────────────────────────────────────────────────────────
# STRUCTURE PRICING
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PricedPosition:
    """A structure that has been priced at a specific time."""
    structure: Structure
    date: str
    entry_time: str
    entry_credit: float         # total credit received (positive = net credit)
    entry_prices: dict          # {strike_cp: price} for each leg at entry
    max_risk: float             # max loss per spread
    qty: int = 1                # number of spreads

    def mark_to_market(self, universe, time_str) -> Optional[float]:
        """
        Mark this position at a later time using real option prices.
        Returns P&L per spread, or None if any leg can't be priced.

        Forward-walk safe: only accesses data at time_str.
        """
        current_value = 0.0
        for leg in self.structure.legs:
            price = universe.option_mid(self.date, leg.strike, leg.cp, time_str)
            if price is None:
                # Try VWAP
                price = universe.option_vwap(self.date, leg.strike, leg.cp, time_str)
            if price is None:
                return None  # can't mark — DON'T silently skip
            current_value += leg.sign * price

        # P&L = entry credit + current value of position
        # Entry: we sold the structure for +credit
        # Now: buying it back costs -current_value (negative current_value = cost to close)
        # P&L = credit + current_value (if current_value is negative, position lost value = good)
        pnl = self.entry_credit + current_value
        return round(pnl, 4)

    def mark_or_intrinsic(self, universe, time_str, spx_price=None) -> float:
        """
        Mark to market, falling back to intrinsic value if option bars unavailable.
        This is useful for settlement (16:15) when option bars may not exist.

        Intrinsic: call = max(0, SPX - K), put = max(0, K - SPX)
        """
        mtm = self.mark_to_market(universe, time_str)
        if mtm is not None:
            return mtm

        if spx_price is None:
            spx_price = universe.spx_at(self.date, time_str)
        if spx_price is None:
            return None

        intrinsic_value = 0.0
        for leg in self.structure.legs:
            if leg.cp == "C":
                iv = max(0, spx_price - leg.strike)
            else:
                iv = max(0, leg.strike - spx_price)
            intrinsic_value += leg.sign * iv

        pnl = self.entry_credit + intrinsic_value
        return round(pnl, 4)

    def pnl_pct_of_credit(self, universe, time_str) -> Optional[float]:
        """P&L as percentage of entry credit."""
        pnl = self.mark_to_market(universe, time_str)
        if pnl is None or self.entry_credit == 0:
            return None
        return round(pnl / self.entry_credit * 100, 2)

    def pnl_pct_of_max_risk(self, universe, time_str) -> Optional[float]:
        """P&L as percentage of max risk (negative = losing)."""
        pnl = self.mark_to_market(universe, time_str)
        if pnl is None or self.max_risk == 0:
            return None
        return round(pnl / self.max_risk * 100, 2)


def price_entry(universe, date_str, time_str, structure: Structure,
                risk_budget: float = None, use_vwap: bool = False) -> Optional[PricedPosition]:
    """
    Price a structure entry using real option data.

    Args:
        universe: DataUniverse instance
        date_str: trading date
        time_str: entry time (HH:MM, must be 5-min boundary for options)
        structure: Structure to price
        risk_budget: if provided, compute qty = risk_budget / max_risk
        use_vwap: if True, prefer VWAP over close for mid estimate

    Returns:
        PricedPosition or None if any leg can't be priced.
        Never fabricates prices. If real data is missing, returns None.
    """
    entry_prices = {}
    net_credit = 0.0

    for leg in structure.legs:
        if use_vwap:
            price = universe.option_vwap(date_str, leg.strike, leg.cp, time_str)
        else:
            price = universe.option_mid(date_str, leg.strike, leg.cp, time_str)

        if price is None:
            return None  # can't price this leg — don't fabricate

        key = f"{leg.strike}_{leg.cp}"
        entry_prices[key] = price
        net_credit -= leg.sign * price  # short legs add credit, long legs cost

    if net_credit <= 0:
        return None  # no credit received — not a valid short-vol structure

    # Compute max risk
    # For 4-leg structures: max risk = max wing width - credit
    call_strikes = sorted(l.strike for l in structure.legs if l.cp == "C")
    put_strikes = sorted(l.strike for l in structure.legs if l.cp == "P")

    max_risk = 0
    if len(call_strikes) >= 2:
        call_width = call_strikes[-1] - call_strikes[0]
        max_risk = max(max_risk, call_width)
    if len(put_strikes) >= 2:
        put_width = put_strikes[-1] - put_strikes[0]
        max_risk = max(max_risk, put_width)

    max_risk = max_risk - net_credit if max_risk > 0 else net_credit * 10  # strangles: undefined

    # Compute qty
    qty = 1
    if risk_budget and max_risk > 0:
        qty = max(1, int(risk_budget / (max_risk * 100)))

    structure.date = date_str
    structure.entry_time = time_str

    return PricedPosition(
        structure=structure,
        date=date_str,
        entry_time=time_str,
        entry_credit=round(net_credit, 4),
        entry_prices=entry_prices,
        max_risk=round(max_risk, 4),
        qty=qty,
    )
