"""
SPX 0DTE Iron Butterfly Auto-Executor
======================================
Enters a 4-leg SPXW iron butterfly at a scheduled time and manages the
position to one of four exits:
  1. Profit target : IBF value decays to (1-profit_target_pct)*credit
  2. Time stop     : held to time_stop_et, then exit
  3. Loss stop     : position P&L < -(loss_stop_pct * max_risk)  [optional]
  4. Wing stop     : SPX price breaches a wing strike             [optional]

Usage
-----
Start one instance per strategy you want to execute. The cockpit tells you
which strategies are live and how many contracts -- pass those as params.

Parameters
----------
  entry_time_et     : "HH:MM" Eastern -- when to enter (optional; omit to enter now)
  contracts         : number of IBF spreads
  wing_width        : SPX points per wing (0 = auto from VIX formula)
  profit_target_pct : 0.50 = take profit when spread value decays 50%
  time_stop_et      : "HH:MM" Eastern -- exit if still open at this time
  loss_stop_pct     : 0.70 = stop if IBF value > credit + 0.70*max_risk (0=off)
  use_wing_stop     : true = stop if SPX breaches either wing strike

Symbol
------
Start with symbol = "SPXW". The underlying price (md.L1.last) is the SPX level.
"""

import math
import datetime
from ktg.interfaces import (
    Strategy,
    Event,
    OptionOrderType,
    OptionTimeInForce,
    DeliverToCompId,
    ExDestination,
    OptionKey,
)

# ── State machine ──────────────────────────────────────────────────────────────
_IDLE      = "IDLE"       # waiting for entry time
_ENTERING  = "ENTERING"   # entry orders submitted, waiting for fills
_ACTIVE    = "ACTIVE"     # position on, monitoring
_EXITING   = "EXITING"    # exit orders submitted
_DONE      = "DONE"       # flat, complete

# ── Timer IDs ─────────────────────────────────────────────────────────────────
_T_ENTRY        = "entry"
_T_TIME_STOP    = "time_stop"
_T_MONITOR      = "monitor"
_T_FILL_TIMEOUT = "fill_timeout"

# Monitor P&L every 30 seconds while active
_MONITOR_US     = 30 * 1_000_000
# Abort entry if all 4 legs not filled within 90 seconds
_FILL_TIMEOUT_US = 90 * 1_000_000
# Refuse to enter if credit per spread is below this (stale quote guard)
_MIN_CREDIT     = 0.50


def _hhmm_to_us(hhmm_str, service):
    """Convert 'HH:MM' Eastern to absolute microsecond timestamp for today."""
    try:
        hh, mm = hhmm_str.strip().split(":")
        hh, mm = int(hh), int(mm)
    except Exception:
        service.error(f"Bad time format '{hhmm_str}' -- expected HH:MM")
        return None
    now_us  = service.system_time
    now_dt  = datetime.datetime.utcfromtimestamp(now_us / 1_000_000)
    # Approximate EDT/EST: Apr-Oct = UTC-4, Nov-Mar = UTC-5
    et_off  = -4 if 3 < now_dt.month < 11 else -5
    et_midnight_utc = datetime.datetime(
        now_dt.year, now_dt.month, now_dt.day, 0, 0, 0
    ) - datetime.timedelta(hours=et_off)
    target_utc = et_midnight_utc + datetime.timedelta(hours=hh, minutes=mm)
    return int(target_utc.timestamp() * 1_000_000)


def _round_strike(price, inc=5):
    """Round SPX price to nearest strike increment."""
    return float(round(round(price / inc) * inc))


def _vix_wing(spx_price, vix):
    """Adaptive wing width formula -- must match backtest exactly."""
    daily_sigma = spx_price * (vix / 100.0) / math.sqrt(252)
    raw_wing    = daily_sigma * 0.75
    return int(max(40, round(raw_wing / 5) * 5))


class spx_ibf_executor(Strategy):
    __script_name__ = "spx_ibf_executor"

    def on_init(self, info):
        info.strategy_name = "SPX IBF Executor"

    # ── on_start ───────────────────────────────────────────────────────────────

    def on_start(self, md, order, service, account):
        p = service.strategy_params

        # Parameters
        self.n_contracts    = int(p.get("contracts", 1))
        self.wing_param     = int(p.get("wing_width", 75))
        self.tgt_pct        = float(p.get("profit_target_pct", 0.50))
        self.time_stop_str  = p.get("time_stop_et", "15:30")
        self.loss_stop_pct  = float(p.get("loss_stop_pct", 0.70))
        self.use_wing_stop  = str(p.get("use_wing_stop", "true")).lower() == "true"
        self.entry_time_str = p.get("entry_time_et", "")

        # State
        self.state          = _IDLE
        self.resolved_expiry = 0
        self.atm            = 0.0
        self.wing           = 0
        self.entry_credit   = 0.0   # credit collected per spread at entry
        self.max_risk       = 0.0   # wing_width - credit (worst case per spread)
        self.tgt_val        = 0.0   # buyback value at which profit target is hit
        self.loss_val       = None  # buyback value at which loss stop triggers
        self.legs           = {}    # {"sc","sp","bc","bp"} -> OptionKey
        self.entry_fills    = set() # leg names that have confirmed fills
        self.exit_fills     = set()

        service.clear_event_triggers()
        service.add_event_trigger(
            [md.symbol],
            [Event.ACK, Event.FILL, Event.CANCEL, Event.REJECT]
        )

        # Subscribe to 0DTE options
        expirations = service.get_expirations()
        if not expirations:
            service.error("No expirations -- options module not initialized")
            service.terminate()
            return

        expiry = expirations[0]   # nearest = today for 0DTE
        spx    = md.L1.last or (md.L1.bid + md.L1.ask) / 2.0
        if spx <= 0:
            service.error("No SPX price available")
            service.terminate()
            return

        result = service.options_subscribe(spx, expiry, 20)
        if not result.get("success"):
            service.error(f"options_subscribe failed: {result.get('error_message')}")
            service.terminate()
            return

        self.resolved_expiry = result["resolved_expiry_yyyymmdd"]
        service.info(
            f"Subscribed: expiry={self.resolved_expiry} "
            f"strikes={result['num_strikes_subscribed']} SPX~{spx:.0f}"
        )

        # Schedule entry
        if self.entry_time_str:
            entry_us = _hhmm_to_us(self.entry_time_str, service)
            if entry_us is None:
                service.terminate()
                return
            now_us = service.system_time
            if entry_us <= now_us:
                service.warn(f"Entry time {self.entry_time_str} ET already passed -- entering in 5s")
                entry_us = now_us + 5_000_000
            service.add_time_trigger(entry_us, 0, timer_id=_T_ENTRY)
            service.info(f"Entry scheduled at {self.entry_time_str} ET")
        else:
            service.add_time_trigger(service.system_time + 5_000_000, 0, timer_id=_T_ENTRY)
            service.info("No entry_time_et -- entering in 5s")

        # Schedule time stop
        ts_us = _hhmm_to_us(self.time_stop_str, service)
        if ts_us:
            service.add_time_trigger(ts_us, 0, timer_id=_T_TIME_STOP)
            service.info(f"Time stop at {self.time_stop_str} ET")

    # ── Timers ─────────────────────────────────────────────────────────────────

    def on_timer(self, e, md, order, service, account):
        tid = getattr(e, "timer_id", None)

        if tid == _T_ENTRY:
            self._enter(md, order, service)

        elif tid == _T_TIME_STOP:
            if self.state in (_IDLE, _ENTERING):
                service.info("Time stop: no position, terminating")
                service.terminate()
            elif self.state == _ACTIVE:
                service.info(f"Time stop: exiting at {self.time_stop_str} ET")
                self._exit(md, order, service, "TIME_STOP")

        elif tid == _T_MONITOR:
            if self.state == _ACTIVE:
                self._check_exits(md, order, service)

        elif tid == _T_FILL_TIMEOUT:
            if self.state == _ENTERING:
                service.warn("Fill timeout: cancelling unfilled legs, aborting")
                for name, key in self.legs.items():
                    if name not in self.entry_fills:
                        order.cancel_option(contract=key)
                service.terminate()

    # ── Options quotes ─────────────────────────────────────────────────────────

    def on_options_batch_quote(self, event, md, order, service, account):
        if self.state == _ACTIVE:
            self._check_exits(md, order, service)

    # ── Order events ──────────────────────────────────────────────────────────

    def on_fill(self, event, md, order, service, account):
        if not getattr(event, "is_option", False):
            return
        key     = getattr(event, "option_key", None)
        if key is None:
            return
        key_str = str(key)

        if self.state == _ENTERING:
            # Match fill to leg name by comparing key strings
            for name, leg_key in self.legs.items():
                if str(leg_key) == key_str:
                    self.entry_fills.add(name)
                    service.info(
                        f"Entry fill {name}: {key_str} "
                        f"qty={event.shares} px=${event.price:.2f} "
                        f"({len(self.entry_fills)}/4)"
                    )
                    break
            if len(self.entry_fills) >= 4:
                self._on_entered(service)

        elif self.state == _EXITING:
            for name, leg_key in self.legs.items():
                if str(leg_key) == key_str:
                    self.exit_fills.add(name)
                    service.info(
                        f"Exit fill {name}: {key_str} "
                        f"qty={event.shares} px=${event.price:.2f} "
                        f"({len(self.exit_fills)}/4)"
                    )
                    break
            if len(self.exit_fills) >= 4:
                service.info("All 4 exit legs filled -- flat")
                service.send_alert("EXIT_FILLED", text=(
                    f"IBF closed. ATM={self.atm:.0f} wing={self.wing} "
                    f"credit_in=${self.entry_credit:.2f}"
                ))
                self.state = _DONE
                service.terminate()

    def on_ack(self, event, md, order, service, account):
        service.info(f"ACK {event.order_id}")

    def on_reject(self, event, md, order, service, account):
        service.error(f"REJECT {event.order_id}: {event.reason}")
        if self.state == _ENTERING:
            service.error("Entry reject -- aborting")
            for name, key in self.legs.items():
                if name not in self.entry_fills:
                    order.cancel_option(contract=key)
            service.terminate()

    def on_cancel(self, event, md, order, service, account):
        service.info(f"CANCEL {event.order_id}")

    # ── Entry ──────────────────────────────────────────────────────────────────

    def _enter(self, md, order, service):
        if self.state != _IDLE:
            return
        if not md.has_options():
            service.error("No options data at entry time")
            service.terminate()
            return

        chain = md.get_options_chain(self.resolved_expiry)
        if not chain:
            service.error("Empty options chain at entry")
            service.terminate()
            return

        spx  = md.L1.last or (md.L1.bid + md.L1.ask) / 2.0
        atm  = _round_strike(spx)

        if self.wing_param > 0:
            wing = self.wing_param
        else:
            vix  = self._approx_vix(chain)
            wing = _vix_wing(spx, vix)
            service.info(f"Auto wing width: vix~{vix:.1f} -> {wing}pts")

        call_wing = atm + wing
        put_wing  = atm - wing
        expiry    = self.resolved_expiry

        self.atm  = atm
        self.wing = wing
        self.legs = {
            "sc": OptionKey("SPXW", expiry, int(atm       * 10000), 'C'),
            "sp": OptionKey("SPXW", expiry, int(atm       * 10000), 'P'),
            "bc": OptionKey("SPXW", expiry, int(call_wing * 10000), 'C'),
            "bp": OptionKey("SPXW", expiry, int(put_wing  * 10000), 'P'),
        }

        service.info(
            f"Entry: SPX={spx:.2f} ATM={atm:.0f} +{wing}/-{wing} "
            f"call_wing={call_wing:.0f} put_wing={put_wing:.0f}"
        )

        # Validate quotes and compute credit
        quotes = {}
        for name, key in self.legs.items():
            v = md.get_option(key)
            if v is None or not v.is_valid() or v.quote.bid <= 0 or v.quote.ask <= 0:
                service.error(f"No valid two-sided quote for {name} ({key})")
                service.terminate()
                return
            quotes[name] = v.quote

        def mid(q):
            return (q.bid + q.ask) / 2.0

        credit = (mid(quotes["sc"]) + mid(quotes["sp"])
                  - mid(quotes["bc"]) - mid(quotes["bp"]))

        if credit < _MIN_CREDIT:
            service.error(
                f"Credit ${credit:.2f} below minimum ${_MIN_CREDIT:.2f} "
                f"(possible stale quotes) -- aborting"
            )
            service.terminate()
            return

        self.entry_credit = credit
        self.max_risk     = wing - credit
        self.tgt_val      = credit * (1.0 - self.tgt_pct)
        if self.loss_stop_pct > 0:
            # Exit if cost-to-close exceeds credit + loss_stop fraction of max_risk
            self.loss_val = credit + (self.loss_stop_pct * self.max_risk)
        else:
            self.loss_val = None

        service.info(
            f"Credit=${credit:.2f} max_risk=${self.max_risk:.2f} "
            f"tgt_buyback=${self.tgt_val:.2f} "
            f"loss_stop={'${:.2f}'.format(self.loss_val) if self.loss_val else 'off'}"
        )

        self.state = _ENTERING

        # Sell short legs at ask (aggressive fill, we want the credit)
        # Buy long wings at ask (pay up, protection matters)
        order.sell_option(
            contract=self.legs["sc"], intent="init",
            type=OptionOrderType.LIMIT, tif=OptionTimeInForce.DAY,
            comp_id=DeliverToCompId.SILEXX, ex_dest=ExDestination.CBOE,
            order_quantity=float(self.n_contracts),
            price=max(quotes["sc"].ask, 0.05)
        )
        order.sell_option(
            contract=self.legs["sp"], intent="init",
            type=OptionOrderType.LIMIT, tif=OptionTimeInForce.DAY,
            comp_id=DeliverToCompId.SILEXX, ex_dest=ExDestination.CBOE,
            order_quantity=float(self.n_contracts),
            price=max(quotes["sp"].ask, 0.05)
        )
        order.buy_option(
            contract=self.legs["bc"], intent="init",
            type=OptionOrderType.LIMIT, tif=OptionTimeInForce.DAY,
            comp_id=DeliverToCompId.SILEXX, ex_dest=ExDestination.CBOE,
            order_quantity=float(self.n_contracts),
            price=max(quotes["bc"].ask, 0.05)
        )
        order.buy_option(
            contract=self.legs["bp"], intent="init",
            type=OptionOrderType.LIMIT, tif=OptionTimeInForce.DAY,
            comp_id=DeliverToCompId.SILEXX, ex_dest=ExDestination.CBOE,
            order_quantity=float(self.n_contracts),
            price=max(quotes["bp"].ask, 0.05)
        )

        service.add_time_trigger(
            service.system_time + _FILL_TIMEOUT_US, 0, timer_id=_T_FILL_TIMEOUT
        )
        service.info("4 entry orders submitted")

    def _on_entered(self, service):
        self.state = _ACTIVE
        service.info(
            f"ACTIVE: {self.n_contracts}x IBF "
            f"ATM={self.atm:.0f} wing={self.wing}pts "
            f"credit=${self.entry_credit:.2f} "
            f"tgt=${self.tgt_val:.2f}"
        )
        service.send_alert("ENTRY_FILLED", text=(
            f"IBF ENTERED: {self.n_contracts} spreads "
            f"ATM={self.atm:.0f} +/-{self.wing}pts "
            f"credit=${self.entry_credit:.2f}"
        ))
        service.add_time_trigger(
            service.system_time + _MONITOR_US, _MONITOR_US,
            timer_id=_T_MONITOR
        )

    # ── Exit monitoring ────────────────────────────────────────────────────────

    def _check_exits(self, md, order, service):
        if self.state != _ACTIVE or not md.has_options():
            return

        # Current cost to close all 4 legs at mid
        close_cost = 0.0
        for name, key in self.legs.items():
            v = md.get_option(key)
            if v is None or not v.is_valid():
                continue
            m = (v.quote.bid + v.quote.ask) / 2.0
            # Short legs: closing costs +mid; long legs: closing returns mid (subtract)
            close_cost += m if name in ("sc", "sp") else -m

        spx = md.L1.last

        # Wing stop
        if self.use_wing_stop:
            if spx >= self.atm + self.wing or spx <= self.atm - self.wing:
                service.warn(
                    f"WING_STOP: SPX={spx:.2f} hit wing at "
                    f"{self.atm+self.wing:.0f}/{self.atm-self.wing:.0f}"
                )
                service.send_alert("STOP_TRIGGERED", text=f"WING_STOP SPX={spx:.2f}")
                self._exit(md, order, service, "WING_STOP")
                return

        # Profit target: close_cost has decayed to tgt_val or below
        if close_cost <= self.tgt_val:
            service.info(
                f"TARGET: close_cost=${close_cost:.2f} <= tgt=${self.tgt_val:.2f}"
            )
            self._exit(md, order, service, "TARGET")
            return

        # Loss stop: close_cost has risen to loss_val or above
        if self.loss_val is not None and close_cost >= self.loss_val:
            service.warn(
                f"LOSS_STOP: close_cost=${close_cost:.2f} >= threshold=${self.loss_val:.2f}"
            )
            service.send_alert("STOP_TRIGGERED", text=f"LOSS_STOP cost=${close_cost:.2f}")
            self._exit(md, order, service, "LOSS_STOP")
            return

        pnl = (self.entry_credit - close_cost) * self.n_contracts * 100
        service.info(
            f"Monitor: SPX={spx:.2f} close_cost=${close_cost:.2f} "
            f"unrealized_pnl=${pnl:.0f}"
        )

    def _exit(self, md, order, service, reason):
        if self.state in (_EXITING, _DONE):
            return
        self.state = _EXITING
        service.info(f"Exiting position: reason={reason}")

        for name, key in self.legs.items():
            v = md.get_option(key)
            if v is not None and v.is_valid():
                if name in ("sc", "sp"):
                    price = max(v.quote.ask, 0.01)   # buy back short legs at ask
                else:
                    price = max(v.quote.bid, 0.01)   # sell long wings at bid
            else:
                price = 0.01 if name in ("sc", "sp") else 999.0

            if name in ("sc", "sp"):
                order.buy_option(
                    contract=key, intent="exit",
                    type=OptionOrderType.LIMIT, tif=OptionTimeInForce.DAY,
                    comp_id=DeliverToCompId.SILEXX, ex_dest=ExDestination.CBOE,
                    order_quantity=float(self.n_contracts), price=price
                )
            else:
                order.sell_option(
                    contract=key, intent="exit",
                    type=OptionOrderType.LIMIT, tif=OptionTimeInForce.DAY,
                    comp_id=DeliverToCompId.SILEXX, ex_dest=ExDestination.CBOE,
                    order_quantity=float(self.n_contracts), price=price
                )
            service.info(f"Exit order: {name} {key} @ ${price:.2f}")

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _approx_vix(self, chain):
        """Estimate VIX from ATM implied vol when VIX symbol not available."""
        for c in chain:
            if c.is_valid() and c.greeks.implied_vol > 0:
                return c.greeks.implied_vol * 100.0
        return 18.0  # fallback
