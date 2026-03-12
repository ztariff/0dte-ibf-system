import pandas as pd
import numpy as np
import json

df = pd.read_csv('research_all_trades.csv')
with open('spx_gap_cache.json') as f:
    gaps = json.load(f)

strats = [
    {"ver":"v3","type":"phoenix","mech":"50%/close/1T","entry":"10:00"},
    {"ver":"v4","vix":["MID"],"prior_day":["DN"],"range":["IN"],"gap":["GUP"],"filter":"5dRet>0","mech":"50%/close/1T","entry":"10:00"},
    {"ver":"v5","vix":["LOW"],"prior_day":["UP"],"range":["IN"],"gap":["GUP"],"filter":"VP<=1.7","mech":"40%/close/1T","entry":"10:30"},
    {"ver":"v6","vix":["LOW"],"prior_day":["DN"],"range":["IN"],"gap":["GFL"],"filter":"5dRet>0","mech":"50%/close/1T","entry":"10:00"},
    {"ver":"v7","vix":["LOW"],"prior_day":["UP"],"range":["IN"],"gap":["GFL"],"filter":"VP<=1.7","mech":"40%/1545/1T","entry":"10:30"},
    {"ver":"v8","vix":["MID"],"prior_day":["DN"],"range":["IN"],"gap":["GFL"],"filter":"5dRet>0","mech":"50%/close/1T","entry":"10:00"},
    {"ver":"v9","vix":["MID"],"prior_day":["UP"],"range":["IN"],"gap":["GFL"],"filter":"!RISING","mech":"50%/close/1T","entry":"10:00"},
    {"ver":"v10","vix":["MID"],"prior_day":["UP"],"range":["IN"],"gap":["GUP"],"filter":"!RISING","mech":"70%/1530/1T","entry":"11:00"},
    {"ver":"v11","vix":["ELEV"],"prior_day":["DN"],"range":["IN"],"gap":["GUP"],"filter":"5dRet>0","mech":"70%/close/1T","entry":"10:00"},
    {"ver":"v12","vix":["ELEV"],"prior_day":["UP"],"range":["IN"],"gap":["GUP"],"filter":None,"mech":"70%/close/1T","entry":"10:00"},
    {"ver":"v13","vix":["ELEV"],"prior_day":["DN"],"range":["IN"],"gap":["GFL"],"filter":"5dRet>0","mech":"50%/close/1T","entry":"10:00"},
    {"ver":"v14","vix":["ELEV"],"prior_day":["DN"],"range":["OT"],"gap":["GUP"],"filter":"5dRet>0","mech":"70%/close/1T","entry":"10:00"},
]

def classify_row(row):
    vix = row["vix"]
    vix_bucket = "LOW" if vix <= 16 else "MID" if vix <= 22 else "ELEV" if vix <= 30 else "HIGH"
    prior_dir = row.get("prior_day_direction", "FLAT")
    pd_label = "UP" if prior_dir == "UP" else "DN" if prior_dir == "DOWN" else "FL"
    in_range = bool(row.get("in_prior_week_range", 0))
    rng = "IN" if in_range else "OT"
    d = str(row["date"])[:10]
    gap_pct = gaps.get(d, 0)
    if isinstance(gap_pct, dict):
        gap_pct = gap_pct.get("gap_pct", 0)
    gap_label = "GUP" if gap_pct > 0.3 else "GDN" if gap_pct < -0.3 else "GFL"
    return vix_bucket, pd_label, rng, gap_label

def phoenix_fire_count(row):
    vp = row.get("vp_ratio", 99)
    vix = row["vix"]
    ret5d = row.get("prior_5d_return", -99)
    rv_chg = row.get("rv_1d_change", -99)
    prior_dir = row.get("prior_day_direction", "FLAT")
    in_range = bool(row.get("in_prior_week_range", 0))
    rv_slope = row.get("rv_slope", "UNKNOWN")
    if pd.isna(vp): vp = 99
    if pd.isna(ret5d): ret5d = -99
    if pd.isna(rv_chg): rv_chg = -99
    count = 0
    details = []
    g1 = vix <= 20 and vp <= 1.0 and ret5d > 0
    if g1: count += 1
    details.append(g1)
    g2 = vp <= 1.3 and prior_dir == "DOWN" and ret5d > 0
    if g2: count += 1
    details.append(g2)
    g3 = vp <= 1.2 and ret5d > 0 and rv_chg > 0
    if g3: count += 1
    details.append(g3)
    g4 = vp <= 1.5 and not in_range and ret5d > 0
    if g4: count += 1
    details.append(g4)
    g5 = vp <= 1.3 and rv_slope != "RISING" and ret5d > 0
    if g5: count += 1
    details.append(g5)
    return count, details

def check_filter(filt, row):
    if filt is None: return True
    vp = row.get("vp_ratio", 99)
    ret5d = row.get("prior_5d_return", -99)
    rv_slope = row.get("rv_slope", "UNKNOWN")
    if pd.isna(vp): vp = 99
    if pd.isna(ret5d): ret5d = -99
    if filt == "5dRet>0": return ret5d > 0
    if filt == "VP<=1.7": return vp <= 1.7
    if filt == "!RISING": return rv_slope != "RISING"
    return True

def compute_pnl(row, strat, fire_count=0):
    ww = row["wing_width"]
    n_spreads = row["n_spreads_p1"]
    risk_deployed = row["risk_deployed_p1"]
    if n_spreads <= 0 or pd.isna(risk_deployed): return None
    entry_credit = ww - risk_deployed / n_spreads / 100
    if entry_credit <= 0: return None
    mech = strat["mech"]
    parts = mech.split("/")
    target_pct = int(parts[0].replace("%","")) / 100
    time_stop = parts[1]
    target_credit = entry_credit * target_pct
    exit_type = None
    pnl_per_spread = None
    for t in ["1030","1100","1130","1200","1230","1300","1330","1400","1430","1500","1530","1545"]:
        col = "pnl_at_" + t
        if col in row.index and pd.notna(row[col]):
            if row[col] >= target_credit:
                pnl_per_spread = target_credit
                exit_type = "TARGET"
                break
    if exit_type is None:
        if time_stop == "close" or time_stop == "1600":
            pnl_per_spread = row.get("pnl_at_close", 0)
            if pd.isna(pnl_per_spread): pnl_per_spread = 0
            exit_type = "CLOSE"
        elif time_stop == "1545":
            pnl_per_spread = row.get("pnl_at_1545", row.get("pnl_at_close", 0))
            if pd.isna(pnl_per_spread): pnl_per_spread = 0
            exit_type = "TIME_1545"
        elif time_stop == "1530":
            pnl_per_spread = row.get("pnl_at_1530", row.get("pnl_at_close", 0))
            if pd.isna(pnl_per_spread): pnl_per_spread = 0
            exit_type = "TIME_1530"
        else:
            pnl_per_spread = row.get("pnl_at_close", 0)
            if pd.isna(pnl_per_spread): pnl_per_spread = 0
            exit_type = "CLOSE"
    max_loss_pnl = row.get("min_pnl", 0)
    if pd.isna(max_loss_pnl): max_loss_pnl = 0
    if max_loss_pnl < -(ww - entry_credit) * 0.9:
        pnl_per_spread = max_loss_pnl
        exit_type = "WING_STOP"
    if strat["ver"] == "v3":
        tier_map = {0: 0, 1: 25000, 2: 50000, 3: 75000, 4: 100000, 5: 100000}
        risk_budget = tier_map.get(fire_count, 100000)
    else:
        risk_budget = 100000
    max_loss_per = (ww - entry_credit) * 100
    if max_loss_per <= 0: return None
    qty = int(risk_budget // max_loss_per)
    if qty <= 0: return None
    dollar_pnl = qty * (pnl_per_spread * 100 - 100)
    return {
        "dollar_pnl": dollar_pnl,
        "pnl_per_spread": pnl_per_spread,
        "entry_credit": entry_credit,
        "exit_type": exit_type,
        "qty": qty,
        "risk_budget": risk_budget,
        "ww": ww,
        "fire_count": fire_count,
        "is_win": pnl_per_spread > 0,
    }

all_stats = {}

for strat in strats:
    ver = strat["ver"]
    trades = []
    for idx, row in df.iterrows():
        vix_bucket, pd_label, rng, gap_label = classify_row(row)
        if ver == "v3":
            fc, details = phoenix_fire_count(row)
            if fc > 0:
                result = compute_pnl(row, strat, fc)
                if result:
                    result["date"] = str(row["date"])[:10]
                    result["regime"] = vix_bucket + "_" + pd_label + "_" + rng + "_" + gap_label
                    trades.append(result)
        else:
            vix_ok = vix_bucket in strat["vix"]
            pd_ok = pd_label in strat["prior_day"]
            rng_ok = rng in strat["range"]
            gap_ok = gap_label in strat["gap"]
            filt_ok = check_filter(strat.get("filter"), row)
            if vix_ok and pd_ok and rng_ok and gap_ok and filt_ok:
                result = compute_pnl(row, strat)
                if result:
                    result["date"] = str(row["date"])[:10]
                    result["regime"] = vix_bucket + "_" + pd_label + "_" + rng + "_" + gap_label
                    trades.append(result)
    if trades:
        pnls = [t["dollar_pnl"] for t in trades]
        wins = [t for t in trades if t["is_win"]]
        losses = [t for t in trades if not t["is_win"]]
        gross_wins = sum(t["dollar_pnl"] for t in wins)
        gross_losses = abs(sum(t["dollar_pnl"] for t in losses))
        exit_counts = {}
        exit_pnl = {}
        for t in trades:
            et = t["exit_type"]
            exit_counts[et] = exit_counts.get(et, 0) + 1
            exit_pnl[et] = exit_pnl.get(et, 0) + t["dollar_pnl"]
        monthly = {}
        for t in trades:
            ym = t["date"][:7]
            monthly[ym] = monthly.get(ym, 0) + t["dollar_pnl"]
        fc_dist = {}
        if ver == "v3":
            for t in trades:
                fc = t["fire_count"]
                if fc not in fc_dist:
                    fc_dist[fc] = {"count": 0, "pnl": 0, "wins": 0}
                fc_dist[fc]["count"] += 1
                fc_dist[fc]["pnl"] += t["dollar_pnl"]
                if t["is_win"]: fc_dist[fc]["wins"] += 1
        stats = {
            "ver": ver,
            "mech": strat["mech"],
            "entry_time": strat["entry"],
            "filter": strat.get("filter", "PHOENIX confluence"),
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(trades) * 100, 1),
            "total_pnl": round(sum(pnls)),
            "avg_pnl": round(sum(pnls) / len(trades)),
            "max_win": round(max(pnls)),
            "max_loss": round(min(pnls)),
            "median_pnl": round(float(np.median(pnls))),
            "gross_wins": round(gross_wins),
            "gross_losses": round(gross_losses),
            "profit_factor": round(gross_wins / gross_losses, 2) if gross_losses > 0 else 999,
            "avg_win": round(gross_wins / len(wins)) if wins else 0,
            "avg_loss": round(-gross_losses / len(losses)) if losses else 0,
            "exit_counts": exit_counts,
            "exit_pnl": {k: round(v) for k, v in exit_pnl.items()},
            "monthly_pnl": {k: round(v) for k, v in sorted(monthly.items())},
            "dates_traded": [t["date"] for t in trades],
            "avg_qty": round(np.mean([t["qty"] for t in trades]), 1),
            "avg_entry_credit": round(np.mean([t["entry_credit"] for t in trades]), 2),
            "avg_ww": round(np.mean([t["ww"] for t in trades]), 1),
        }
        if ver == "v3":
            stats["fire_count_dist"] = {str(k): v for k, v in sorted(fc_dist.items())}
        streak_w = 0
        streak_l = 0
        max_streak_w = 0
        max_streak_l = 0
        for t in trades:
            if t["is_win"]:
                streak_w += 1
                streak_l = 0
                max_streak_w = max(max_streak_w, streak_w)
            else:
                streak_l += 1
                streak_w = 0
                max_streak_l = max(max_streak_l, streak_l)
        stats["max_win_streak"] = max_streak_w
        stats["max_loss_streak"] = max_streak_l
        equity = 0
        peak = 0
        max_dd = 0
        for t in trades:
            equity += t["dollar_pnl"]
            peak = max(peak, equity)
            dd = peak - equity
            max_dd = max(max_dd, dd)
        stats["max_drawdown"] = round(max_dd)
        stats["final_equity"] = round(equity)
        all_stats[ver] = stats
    else:
        all_stats[ver] = {
            "ver": ver,
            "total_trades": 0,
            "total_pnl": 0,
            "note": "No trades matched"
        }

with open("strategy_stats.json", "w") as f:
    json.dump(all_stats, f, indent=2)

header = "{:<5} {:>7} {:>8} {:>12} {:>9} {:>6} {:>9}".format("Ver", "Trades", "WinRate", "Total P&L", "Avg P&L", "PF", "MaxDD")
print(header)
print("-" * 65)
for ver in ["v3","v4","v5","v6","v7","v8","v9","v10","v11","v12","v13","v14"]:
    s = all_stats.get(ver, {})
    if s.get("total_trades", 0) > 0:
        line = "{:<5} {:>7} {:>7.1f}% ${:>10,} ${:>7,} {:>5.2f} ${:>7,}".format(
            ver, s["total_trades"], s["win_rate"], s["total_pnl"], s["avg_pnl"], s["profit_factor"], s["max_drawdown"])
        print(line)
    else:
        print("{:<5}       0    ---           ---        ---     ---        ---".format(ver))

print()
print("Saved to strategy_stats.json")
