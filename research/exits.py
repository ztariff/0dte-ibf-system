"""
research/exits.py — Exit mechanics for option positions.

All exit logic is forward-walk safe: decisions at time T use only
data available at or before T.  No peeking at future bars.

Supported exit types:
  - PROFIT_TARGET: close when P&L >= X% of credit
  - TIME_STOP: close at a fixed time regardless of P&L
  - WING_STOP: close when SPX breaches a wing strike (1-min resolution)
  - LOSS_STOP: close when P&L <= -X% of max risk
  - TRAILING_STOP: after reaching X% profit, close if profit drops Y% from peak
  - CLOSE: held to settlement (16:15 intrinsic)
  - CUSTOM: user-defined exit function
"""

from dataclasses import dataclass
from typing import Optional, List, Callable
from research.structures import PricedPosition


@dataclass
class ExitRule:
    """A single exit condition."""
    name: str
    check: Callable  # (universe, position, time_str, state) -> bool
    priority: int = 0  # lower = checked first


@dataclass
class TradeResult:
    """The complete outcome of a trade."""
    date: str
    entry_time: str
    exit_time: str
    exit_type: str          # TARGET, TIME, WING_STOP, LOSS_STOP, TRAIL, CLOSE
    entry_credit: float     # per spread
    pnl_per_spread: float   # at exit
    pnl_dollar: float       # pnl_per_spread * 100 * qty
    max_risk: float
    qty: int
    peak_pnl: float         # highest P&L observed during trade
    trough_pnl: float       # lowest P&L observed during trade
    pnl_timeline: dict      # {HH:MM: pnl_per_spread} at each checkpoint
    structure_name: str
    metadata: dict = None   # extra info (wing stop SPX, etc.)


# ─────────────────────────────────────────────────────────────────────────────
# EXIT RULE FACTORIES
# ─────────────────────────────────────────────────────────────────────────────

def profit_target(pct: float) -> ExitRule:
    """Exit when P&L >= pct * entry_credit."""
    def check(universe, pos, time_str, state):
        pnl = pos.mark_to_market(universe, time_str)
        if pnl is not None and pnl >= pos.entry_credit * pct:
            return True
        return False
    return ExitRule(name=f"TARGET_{int(pct*100)}%", check=check, priority=10)


def time_stop(stop_time: str) -> ExitRule:
    """Exit at or after stop_time."""
    def check(universe, pos, time_str, state):
        return time_str >= stop_time
    return ExitRule(name=f"TIME_{stop_time}", check=check, priority=20)


def wing_stop() -> ExitRule:
    """
    Exit when SPX breaches either wing strike.
    Uses 1-min SPX bars for high-resolution detection.
    """
    def check(universe, pos, time_str, state):
        # Find the wing strikes (outermost long legs)
        call_wings = [l.strike for l in pos.structure.legs if l.cp == "C" and l.side == "LONG"]
        put_wings = [l.strike for l in pos.structure.legs if l.cp == "P" and l.side == "LONG"]

        upper = max(call_wings) if call_wings else None
        lower = min(put_wings) if put_wings else None

        # Check 1-min bars from last check to current time
        last_check = state.get("last_wing_check", pos.entry_time)
        bars = universe.spx_bars_range(pos.date, last_check, time_str)
        state["last_wing_check"] = time_str

        for t, bar in bars:
            if t <= pos.entry_time:
                continue
            if upper and bar["h"] >= upper:
                state["wing_stop_spx"] = bar["h"]
                state["wing_stop_side"] = "CALL"
                return True
            if lower and bar["l"] <= lower:
                state["wing_stop_spx"] = bar["l"]
                state["wing_stop_side"] = "PUT"
                return True
        return False
    return ExitRule(name="WING_STOP", check=check, priority=5)


def loss_stop(pct_of_max_risk: float) -> ExitRule:
    """Exit when loss exceeds pct_of_max_risk of max risk."""
    def check(universe, pos, time_str, state):
        pnl = pos.mark_to_market(universe, time_str)
        if pnl is not None and pnl < -(pos.max_risk * pct_of_max_risk):
            return True
        return False
    return ExitRule(name=f"LOSS_STOP_{int(pct_of_max_risk*100)}%", check=check, priority=3)


def trailing_stop(activate_pct: float, trail_pct: float) -> ExitRule:
    """
    After P&L reaches activate_pct of credit, close if it drops
    trail_pct from the peak.

    Example: activate_pct=0.30, trail_pct=0.15 means:
      Once P&L >= 30% of credit, start trailing.
      If P&L drops 15% of credit from intraday high, exit.
    """
    def check(universe, pos, time_str, state):
        pnl = pos.mark_to_market(universe, time_str)
        if pnl is None:
            return False

        credit = pos.entry_credit
        # Track peak P&L
        peak = state.get("trail_peak", 0)
        if pnl > peak:
            state["trail_peak"] = pnl
            peak = pnl

        # Is the trail active?
        if peak >= credit * activate_pct:
            state["trail_active"] = True
            # Check if we've given back too much
            if pnl < peak - credit * trail_pct:
                return True
        return False
    return ExitRule(name=f"TRAIL_{int(activate_pct*100)}/{int(trail_pct*100)}",
                    check=check, priority=15)


def time_decay_target(early_pct: float, late_pct: float, transition_time: str) -> ExitRule:
    """
    Dynamic target: early_pct before transition_time, late_pct after.
    Tighten targets in the afternoon when theta accelerates.
    """
    def check(universe, pos, time_str, state):
        pnl = pos.mark_to_market(universe, time_str)
        if pnl is None:
            return False
        target = early_pct if time_str < transition_time else late_pct
        return pnl >= pos.entry_credit * target
    return ExitRule(name=f"TDTARGET_{int(early_pct*100)}/{int(late_pct*100)}@{transition_time}",
                    check=check, priority=10)


# ─────────────────────────────────────────────────────────────────────────────
# TRADE SIMULATOR
# ─────────────────────────────────────────────────────────────────────────────

# Standard 5-min time grid for option bar checkpoints
TIME_GRID_5MIN = []
for h in range(9, 17):
    for m in range(0, 60, 5):
        t = f"{h:02d}:{m:02d}"
        if "09:30" <= t <= "16:15":
            TIME_GRID_5MIN.append(t)


def simulate_trade(universe, position: PricedPosition,
                   exit_rules: List[ExitRule],
                   slippage_per_spread: float = 1.0) -> Optional[TradeResult]:
    """
    Walk a position forward through time, checking exit rules at each
    5-min option bar. Returns a TradeResult or None if the trade couldn't
    be tracked (missing data).

    Args:
        universe: DataUniverse
        position: PricedPosition (already priced at entry)
        exit_rules: list of ExitRule to check (in priority order)
        slippage_per_spread: dollar slippage per spread (default $1)

    Forward-walk guarantee:
        At each time step T, only data at or before T is accessed.
        Exit rules are checked in priority order at each step.
    """
    date = position.date
    entry = position.entry_time

    # Sort exit rules by priority
    rules = sorted(exit_rules, key=lambda r: r.priority)

    # State dict shared across exit rules for intra-trade tracking
    state = {}

    # Track P&L timeline
    timeline = {}
    peak_pnl = 0.0
    trough_pnl = 0.0

    # Get the 5-min checkpoints after entry
    checkpoints = [t for t in TIME_GRID_5MIN if t > entry]
    if not checkpoints:
        return None

    exit_time = None
    exit_type = None

    for t in checkpoints:
        # Mark to market at this checkpoint
        pnl = position.mark_to_market(universe, t)

        if pnl is not None:
            timeline[t] = round(pnl, 4)
            peak_pnl = max(peak_pnl, pnl)
            trough_pnl = min(trough_pnl, pnl)

        # Check exit rules
        for rule in rules:
            if rule.check(universe, position, t, state):
                exit_time = t
                exit_type = rule.name
                break

        if exit_time:
            break

    # If no exit triggered, hold to settlement
    if not exit_time:
        exit_time = "16:15"
        exit_type = "CLOSE"

    # Get final P&L
    final_pnl = position.mark_to_market(universe, exit_time)
    if final_pnl is None:
        # Try intrinsic at settlement
        final_pnl = position.mark_or_intrinsic(universe, exit_time)
    if final_pnl is None:
        # Last resort: use last known P&L from timeline
        if timeline:
            last_t = max(timeline.keys())
            final_pnl = timeline[last_t]
        else:
            return None  # can't determine P&L at all

    # Apply slippage
    pnl_after_slippage = final_pnl - (slippage_per_spread / 100)
    dollar_pnl = pnl_after_slippage * 100 * position.qty

    return TradeResult(
        date=date,
        entry_time=entry,
        exit_time=exit_time,
        exit_type=exit_type,
        entry_credit=position.entry_credit,
        pnl_per_spread=round(pnl_after_slippage, 4),
        pnl_dollar=round(dollar_pnl, 2),
        max_risk=position.max_risk,
        qty=position.qty,
        peak_pnl=round(peak_pnl, 4),
        trough_pnl=round(trough_pnl, 4),
        pnl_timeline=timeline,
        structure_name=position.structure.name,
        metadata=dict(state),
    )


# ─────────────────────────────────────────────────────────────────────────────
# CONVENIENCE: common exit rule combos
# ─────────────────────────────────────────────────────────────────────────────

def standard_exits(target_pct=0.50, stop_time="15:30", use_wing_stop=True):
    """The standard exit rule set: target + time stop + optional wing stop."""
    rules = [profit_target(target_pct), time_stop(stop_time)]
    if use_wing_stop:
        rules.append(wing_stop())
    return rules


def aggressive_exits(target_pct=0.30, stop_time="15:30"):
    """Aggressive: tight target + wing stop + loss stop at 70%."""
    return [
        profit_target(target_pct),
        wing_stop(),
        loss_stop(0.70),
        time_stop(stop_time),
    ]


def trailing_exits(activate_pct=0.30, trail_pct=0.15, stop_time="15:30"):
    """Trailing stop after initial profit, with wing stop safety net."""
    return [
        wing_stop(),
        trailing_stop(activate_pct, trail_pct),
        time_stop(stop_time),
    ]
