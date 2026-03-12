"""Generate PHOENIX 0DTE IBF Strategy Research Report as PDF."""
import json, os
from fpdf import FPDF
from datetime import datetime

_DIR = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(_DIR, 'strategy_stats.json')) as f:
    stats = json.load(f)

# ─── Strategy edge descriptions (plain English with trading terms) ───
edges = {
    "v3": {
        "title": "PHOENIX Concentrated Signal Model",
        "regime": "Any regime (signal-driven, not regime-gated)",
        "entry": "10:00 AM ET",
        "mechanics": "50% profit target | Hold to close | 1 tranche",
        "sizing": "Tiered: 1 sig=$25K, 2=$50K, 3=$75K, 4+=$100K",
        "edge": (
            "PHOENIX is the flagship strategy. Instead of matching a single market regime, "
            "it evaluates 5 independent confluence signals at 10:00 AM and only trades when "
            "at least one fires. Each signal tests a different combination of implied vs "
            "realized volatility, trend context, and vol dynamics.\n\n"
            "The core thesis: sell an iron butterfly when multiple independent indicators "
            "confirm that implied volatility is overpriced relative to what the market is "
            "actually doing. A Volatility Premium (VP) below 1.3 means the options market is "
            "pricing 30%+ more movement than realized vol justifies. Combined with a positive "
            "5-day return (uptrend context) and non-rising RV (vol not expanding), you get "
            "a high-probability setup for premium decay.\n\n"
            "The tiered sizing is the key innovation: 1 signal gets 25% capital (low confidence), "
            "but 3+ signals get 75-100% (redundant confirmation across implied vol, price action, "
            "and realized vol dynamics). This concentrates risk on the highest-conviction days "
            "while maintaining exposure on marginal ones."
        ),
        "signals": [
            "Group #1: VIX<=20 + VP<=1.0 + 5dRet>0 -- Deep vol discount in a calm, trending market. "
            "VIX under 20 with VP below 1.0 means implied vol is LESS than realized -- rare and powerful.",
            "Group #2: VP<=1.3 + PriorDayDown + 5dRet>0 -- Pullback in an uptrend with overpriced options. "
            "Yesterday sold off but the 5-day trend is still positive -- classic mean reversion setup.",
            "Group #3: VP<=1.2 + 5dRet>0 + RVchg>0 -- Strong vol discount with compressing realized vol. "
            "RV rose yesterday (spiked) but VP is still low, signaling the spike was noise, not trend.",
            "Group #4: VP<=1.5 + OutsideWeekRange + 5dRet>0 -- Breakout with vol premium. "
            "Price pushed outside the prior week range but IV is still manageable -- extended move likely to consolidate.",
            "Group #5: VP<=1.3 + RV not RISING + 5dRet>0 -- Broad vol discount with stable/falling realized vol. "
            "The market is calm underneath despite what VIX implies -- textbook premium selling conditions.",
        ],
    },
    "v4": {
        "title": "Mid-VIX Down Day, Flat Gap, In Range",
        "regime": "MID_DN_IN_GFL (VIX 15-20, prior day down, in prior week range, flat gap)",
        "entry": "10:00 AM ET",
        "mechanics": "40% profit target | Hold to close | 1 tranche",
        "sizing": "$100K risk budget",
        "edge": (
            "This strategy targets a very common regime: moderate implied vol (VIX 15-20) "
            "after a down day, with price still inside the prior week's range and no meaningful "
            "gap at the open. The thesis is that yesterday's selloff was contained (still in range) "
            "and today opens flat, suggesting the move is digested and the market is likely to "
            "consolidate. The 40% target is conservative, reflecting the high frequency but "
            "moderate edge of this setup. No additional filter is required -- the regime alone "
            "provides enough context.\n\n"
            "The risk: this fires on 44 days (most of any regime strategy), which means it catches "
            "some unfavorable days. The 65.9% win rate and 0.76 profit factor indicate this regime "
            "alone isn't enough -- it's a volume play that can accumulate losses during extended selloffs."
        ),
    },
    "v5": {
        "title": "Mid-VIX Up Day, Gap Up, Outside Range",
        "regime": "MID_UP_OT_GUP (VIX 15-20, prior day up, outside range, gap up)",
        "entry": "10:00 AM ET",
        "mechanics": "40% profit target | Hold to close | 1 tranche",
        "sizing": "$100K risk budget",
        "edge": (
            "After a strong up day that pushed price outside the prior week's range, today gaps "
            "up further. The filter requires 5-day return > 0 (confirming a genuine uptrend, "
            "not a dead cat bounce). The thesis: extended breakouts tend to consolidate intraday "
            "even if the trend continues. The butterfly profits from the pause, not the direction.\n\n"
            "The 72.2% win rate is solid, but with only 18 trades and a 1.17 PF, the edge is "
            "thin. The gap-up-outside-range condition is rare enough to keep trade count low, "
            "but when it fires into a trending day, the losses are large."
        ),
    },
    "v6": {
        "title": "Low-VIX Down Day, Flat Gap, In Range",
        "regime": "LOW_DN_IN_GFL (VIX <15, prior day down, in range, flat gap)",
        "entry": "10:00 AM ET",
        "mechanics": "50% profit target | Time stop 3:30 PM | 1 tranche",
        "sizing": "$100K risk budget",
        "edge": (
            "Low VIX (under 15) after a down day with price still in the prior week's range. "
            "The VP<=1.7 filter ensures implied vol isn't wildly overpricing the move. In a low-VIX "
            "environment, down days tend to be shallow pullbacks in a calm uptrend. The market "
            "opens flat (no panic gap), confirming the selloff was orderly.\n\n"
            "The 3:30 PM time stop is earlier than most strategies -- it exits before the final "
            "30 minutes of theta crush because low-VIX days can see late-day reversals when gamma "
            "is still meaningful. The 75% win rate and 1.91 PF make this one of the more reliable "
            "regime strategies."
        ),
    },
    "v7": {
        "title": "Low-VIX Flat Day, Gap Up, In Range",
        "regime": "LOW_FL_IN_GUP (VIX <15, prior day flat, in range, gap up)",
        "entry": "10:00 AM ET",
        "mechanics": "40% profit target | Hold to close | 1 tranche",
        "sizing": "$100K risk budget",
        "edge": (
            "Yesterday was a nothing-burger (flat close) in a low-VIX regime, and today gaps up. "
            "Price is still inside the prior week's range, so the gap-up is a continuation of the "
            "calm drift rather than a breakout. Iron butterflies thrive in this \"boring\" environment "
            "where nothing dramatic happens and premium decays smoothly.\n\n"
            "Only 5 trades in the backtest makes this statistically thin, but the 80% win rate and "
            "3.79 PF are noteworthy. The edge is intuitive: flat-into-gap-up in low vol = theta paradise."
        ),
    },
    "v8": {
        "title": "Elevated VIX Up Day, Gap Down, In Range",
        "regime": "ELEV_UP_IN_GDN (VIX 20-25, prior day up, in range, gap down)",
        "entry": "10:30 AM ET",
        "mechanics": "40% profit target | Time stop 3:30 PM | 1 tranche",
        "sizing": "$100K risk budget",
        "edge": (
            "Elevated VIX (20-25) means fat premium to sell. Yesterday was an up day (relief rally?) "
            "but today gaps down -- the market is oscillating. Price is still within the prior week's "
            "range, so this isn't a breakdown, it's chop.\n\n"
            "The 10:30 entry (30 min late) lets the opening volatility settle before placing the trade. "
            "The 3:30 PM time stop avoids the dangerous final hour when elevated VIX days can see "
            "violent close-to-close moves. With only 8 trades and a 1.06 PF, this is a marginal edge -- "
            "the premium is high but so is the realized movement."
        ),
    },
    "v9": {
        "title": "Mid-VIX Up Day, Flat Gap, Outside Range",
        "regime": "MID_UP_OT_GFL (VIX 15-20, prior day up, outside range, flat gap)",
        "entry": "10:00 AM ET",
        "mechanics": "70% profit target | Time stop 3:45 PM | 1 tranche",
        "sizing": "$100K risk budget",
        "edge": (
            "Price has already broken out of the prior week's range to the upside (yesterday was up), "
            "and today opens flat. The breakout is established but not accelerating -- the market "
            "is pausing to digest. The !RISING filter confirms that realized vol is not expanding, "
            "meaning the breakout is orderly, not panicky.\n\n"
            "The aggressive 70% profit target reflects high conviction: when price is already extended "
            "and vol is stable, the butterfly can capture a large portion of its credit as the "
            "breakout consolidates. 79.3% win rate and 2.36 PF make this the second-best performing "
            "strategy after V3, with enough trades (29) for statistical relevance."
        ),
    },
    "v10": {
        "title": "Mid-VIX Down Day, Flat Gap, Outside Range",
        "regime": "MID_DN_OT_GFL (VIX 15-20, prior day down, outside range, flat gap)",
        "entry": "11:00 AM ET",
        "mechanics": "70% profit target | Time stop 3:45 PM | 1 tranche",
        "sizing": "$100K risk budget",
        "edge": (
            "The mirror image of V9: price has broken DOWN out of the prior week's range, yesterday "
            "was a down day, and today opens flat. The selloff already happened and today the market "
            "is catching its breath. The 11:00 AM entry is the latest of all strategies -- it waits "
            "a full 90 minutes for the morning session to confirm the market isn't continuing to "
            "sell off before entering.\n\n"
            "The 70% target is aggressive because when an extended selloff pauses, the snap-back "
            "(or at minimum the consolidation) tends to be sharp and sustained. 66.7% win rate "
            "and $360K total P&L over 21 trades, though the $263K max drawdown shows the risk "
            "when the selloff resumes."
        ),
    },
    "v11": {
        "title": "Low-VIX Up Day, Flat Gap, Outside Range",
        "regime": "LOW_UP_OT_GFL (VIX <15, prior day up, outside range, flat gap)",
        "entry": "10:00 AM ET",
        "mechanics": "70% profit target | Hold to close | 1 tranche",
        "sizing": "$100K risk budget",
        "edge": (
            "Low VIX with price already above the prior week's range after an up day -- this is "
            "a calm breakout in a complacent market. The VP<=2.0 filter is lenient (allows VP up "
            "to 2x), acknowledging that in low-VIX environments the absolute premium is smaller "
            "and VP tends to run higher.\n\n"
            "The edge is that breakouts in low-vol regimes tend to stall intraday even as the "
            "daily trend continues. The butterfly captures intraday consolidation premium. "
            "29 trades, 62.1% win rate, but the 1.26 PF means the wins barely outpace losses."
        ),
    },
    "v12": {
        "title": "Low-VIX Up Day, Gap Up, Outside Range",
        "regime": "LOW_UP_OT_GUP (VIX <15, prior day up, outside range, gap up)",
        "entry": "10:00 AM ET",
        "mechanics": "40% profit target | Hold to close | 1 tranche",
        "sizing": "$100K risk budget",
        "edge": (
            "Strong momentum: up day, gap up, price above the prior week range, all in a low-VIX "
            "environment. The 5dRet>1% filter is the tightest in the ensemble -- it requires a full "
            "1% rally over 5 days, confirming genuine trend strength rather than noise.\n\n"
            "Paradoxically, selling premium on the strongest trend days works because intraday "
            "movement consolidates even as the trend extends over multiple days. 8 trades, 75% win "
            "rate, 1.98 PF -- small sample but clean execution."
        ),
    },
    "v13": {
        "title": "Low-VIX Down Day, Gap Up, In Range",
        "regime": "LOW_DN_IN_GUP (VIX <15, prior day down, in range, gap up)",
        "entry": "10:30 AM ET",
        "mechanics": "40% profit target | Hold to close | 1 tranche",
        "sizing": "$100K risk budget",
        "edge": (
            "Yesterday was a down day in a low-VIX regime, but today gaps up -- a classic reversal "
            "pattern. Price is still inside the prior week's range (no damage done), and the "
            "Rng<=0.3% filter requires the morning range to be very tight (under 0.3%), confirming "
            "that the gap-up is not volatile but controlled.\n\n"
            "The 10:30 entry lets the opening print settle. Only 7 trades with an 85.7% win rate "
            "suggests this is a high-selectivity, high-conviction setup -- when all conditions "
            "align, the butterfly almost always works."
        ),
    },
    "v14": {
        "title": "Mid-VIX Down Day, Gap Down, In Range",
        "regime": "MID_DN_IN_GDN (VIX 15-20, prior day down, in range, gap down)",
        "entry": "10:00 AM ET",
        "mechanics": "50% profit target | Hold to close | 1 tranche",
        "sizing": "$100K risk budget",
        "edge": (
            "Back-to-back selling pressure: yesterday was down and today gaps down further. But "
            "VIX is only in the 15-20 range (not elevated), price is still inside the prior week's "
            "range (no breakdown), and the ScoreVol<18 filter confirms that the volatility premium "
            "score is moderate (options aren't pricing in a crash).\n\n"
            "The edge: when the market sells off but IV doesn't spike aggressively, it signals "
            "an orderly correction rather than panic. The butterfly profits from the subsequent "
            "intraday consolidation. 8 trades, 87.5% win rate, 3.99 PF -- one of the highest-conviction "
            "setups in the ensemble, though the sample is small."
        ),
    },
}

# ─── Build PDF ───
class PDF(FPDF):
    def header(self):
        self.set_font('Helvetica', 'B', 9)
        self.set_text_color(120, 120, 120)
        self.cell(0, 6, 'PHOENIX 0DTE IBF Strategy Research Report', align='R', new_x='LMARGIN', new_y='NEXT')
        self.line(10, self.get_y(), 200, self.get_y())
        self.ln(3)

    def footer(self):
        self.set_y(-15)
        self.set_font('Helvetica', 'I', 8)
        self.set_text_color(150, 150, 150)
        self.cell(0, 10, f'Page {self.page_no()}/{{nb}}', align='C')

    def section_title(self, title):
        self.set_font('Helvetica', 'B', 14)
        self.set_text_color(20, 20, 20)
        self.cell(0, 10, title, new_x='LMARGIN', new_y='NEXT')
        self.ln(2)

    def sub_title(self, title):
        self.set_font('Helvetica', 'B', 11)
        self.set_text_color(40, 40, 40)
        self.cell(0, 8, title, new_x='LMARGIN', new_y='NEXT')
        self.ln(1)

    def body_text(self, text):
        self.set_font('Helvetica', '', 9)
        self.set_text_color(50, 50, 50)
        self.multi_cell(0, 4.5, text)
        self.ln(2)

    def stat_line(self, label, value, bold_val=False):
        self.set_font('Helvetica', '', 9)
        self.set_text_color(80, 80, 80)
        self.cell(50, 5, label)
        self.set_font('Helvetica', 'B' if bold_val else '', 9)
        self.set_text_color(20, 20, 20)
        self.cell(0, 5, str(value), new_x='LMARGIN', new_y='NEXT')

pdf = PDF()
pdf.alias_nb_pages()
pdf.set_auto_page_break(auto=True, margin=20)

# ─── COVER PAGE ───
pdf.add_page()
pdf.ln(30)
pdf.set_font('Helvetica', 'B', 28)
pdf.set_text_color(20, 20, 20)
pdf.cell(0, 15, 'PHOENIX 0DTE IBF', align='C', new_x='LMARGIN', new_y='NEXT')
pdf.set_font('Helvetica', '', 16)
pdf.set_text_color(80, 80, 80)
pdf.cell(0, 10, 'Strategy Ensemble Research Report', align='C', new_x='LMARGIN', new_y='NEXT')
pdf.ln(5)
pdf.set_font('Helvetica', '', 11)
pdf.cell(0, 8, 'Strategies V3 through V14', align='C', new_x='LMARGIN', new_y='NEXT')
pdf.cell(0, 8, 'Backtest Period: November 2023 - February 2026', align='C', new_x='LMARGIN', new_y='NEXT')
pdf.cell(0, 8, '573 Trading Days | $100,000 Daily Risk Budget', align='C', new_x='LMARGIN', new_y='NEXT')
pdf.ln(10)
pdf.set_font('Helvetica', '', 9)
pdf.set_text_color(120, 120, 120)
pdf.cell(0, 6, f'Generated: {datetime.now().strftime("%B %d, %Y")}', align='C', new_x='LMARGIN', new_y='NEXT')
pdf.cell(0, 6, 'Instrument: SPX 0DTE Iron Butterfly (cash-settled)', align='C', new_x='LMARGIN', new_y='NEXT')
pdf.cell(0, 6, 'Sizing: Adaptive wing width (0.75x daily sigma, min 40pt)', align='C', new_x='LMARGIN', new_y='NEXT')
pdf.cell(0, 6, 'Slippage: $1.00 per contract deducted from all P&L', align='C', new_x='LMARGIN', new_y='NEXT')

# ─── EXECUTIVE SUMMARY ───
pdf.add_page()
pdf.section_title('Executive Summary')
pdf.body_text(
    'This report covers 12 strategies (V3-V14) that trade 0DTE SPX iron butterflies under different '
    'market conditions. V3 (PHOENIX) uses a multi-signal confluence model. V4-V14 are regime-based '
    'strategies that each target a specific combination of VIX level, prior-day direction, weekly range '
    'position, and opening gap direction.'
)
pdf.body_text(
    'Each strategy enters a single iron butterfly at its designated time, with an adaptive wing width '
    'based on implied daily range (0.75x one-sigma move from VIX, rounded to nearest 5 points, minimum '
    '40 points). All P&L assumes $100,000 daily risk budget with $1/contract slippage. V3 uses tiered '
    'sizing based on signal count; V4-V14 always deploy the full $100K budget.'
)

# Summary table
pdf.sub_title('Performance Overview')
pdf.set_font('Courier', 'B', 8)
pdf.set_text_color(20, 20, 20)
header = f"{'Strat':<6} {'Trades':>6} {'Win%':>6} {'Total P&L':>12} {'Avg P&L':>10} {'PF':>6} {'Max DD':>10} {'Avg Win':>9} {'Avg Loss':>10}"
pdf.cell(0, 5, header, new_x='LMARGIN', new_y='NEXT')
pdf.set_font('Courier', '', 7)
pdf.cell(0, 4, '-' * 85, new_x='LMARGIN', new_y='NEXT')

total_trades = 0
total_pnl = 0
for ver in ['v3','v4','v5','v6','v7','v8','v9','v10','v11','v12','v13','v14']:
    s = stats.get(ver, {})
    n = s.get('total_trades', 0)
    total_trades += n
    total_pnl += s.get('total_pnl', 0)
    if n > 0:
        pnl_color = (0, 120, 0) if s['total_pnl'] >= 0 else (200, 0, 0)
        pdf.set_text_color(*pnl_color)
        line = f"{ver:<6} {n:>6} {s['win_rate']:>5.1f}% ${s['total_pnl']:>10,} ${s['avg_pnl']:>8,} {s['profit_factor']:>5.2f} ${s['max_drawdown']:>8,} ${s['avg_win']:>7,} ${s['avg_loss']:>8,}"
        pdf.cell(0, 4.5, line, new_x='LMARGIN', new_y='NEXT')
    else:
        pdf.set_text_color(150, 150, 150)
        pdf.cell(0, 4.5, f"{ver:<6}      0   ---", new_x='LMARGIN', new_y='NEXT')

pdf.set_text_color(20, 20, 20)
pdf.set_font('Courier', 'B', 8)
pdf.cell(0, 4, '-' * 85, new_x='LMARGIN', new_y='NEXT')
pdf.cell(0, 5, f"{'TOTAL':<6} {total_trades:>6} {'':>6} ${total_pnl:>10,}", new_x='LMARGIN', new_y='NEXT')
pdf.ln(5)

# Key findings
pdf.sub_title('Key Findings')
pdf.body_text(
    '1. V3 (PHOENIX) is the highest-volume profitable strategy: 112 trades, 70.5% win rate, '
    '$919K total P&L, 2.04 profit factor. Its tiered sizing concentrates capital on the best days.\n\n'
    '2. V9 (Mid-VIX, up day, outside range, flat gap) is the best regime strategy: 29 trades, '
    '79.3% win rate, $425K P&L, 2.36 PF. The !RISING filter and 70% target are well-calibrated.\n\n'
    '3. V10 (Mid-VIX, down day, outside range, flat gap) is the highest per-trade earner among '
    'viable strategies: $17,145 average per trade, driven by the 70% target on extended selloff pauses.\n\n'
    '4. V14 (Mid-VIX, down-gap-down, in range) has the highest win rate (87.5%) and PF (3.99) '
    'but only 8 trades -- high conviction, low frequency.\n\n'
    '5. V4 is the biggest drag: 44 trades at a -$250K loss. The MID_DN_IN_GFL regime fires too '
    'often without a quality filter, catching unfavorable trending days.\n\n'
    '6. The combined ensemble generates $2.33M over 573 trading days -- roughly $4,070 per day.'
)

# ─── INDIVIDUAL STRATEGY PAGES ───
for ver in ['v3','v4','v5','v6','v7','v8','v9','v10','v11','v12','v13','v14']:
    s = stats.get(ver, {})
    e = edges.get(ver, {})
    n = s.get('total_trades', 0)

    pdf.add_page()
    pdf.section_title(f'{ver.upper()}: {e.get("title", "")}')

    # Regime & mechanics box
    pdf.set_fill_color(245, 245, 245)
    pdf.set_font('Helvetica', '', 9)
    pdf.set_text_color(50, 50, 50)
    box_y = pdf.get_y()
    pdf.cell(0, 5, f'Regime: {e.get("regime", "")}', new_x='LMARGIN', new_y='NEXT', fill=True)
    pdf.cell(0, 5, f'Entry: {e.get("entry", "")}  |  Mechanics: {e.get("mechanics", "")}', new_x='LMARGIN', new_y='NEXT', fill=True)
    pdf.cell(0, 5, f'Filter: {s.get("filter", "None")}  |  Sizing: {e.get("sizing", "$100K")}', new_x='LMARGIN', new_y='NEXT', fill=True)
    pdf.ln(4)

    if n == 0:
        pdf.body_text('No trades matched this regime during the backtest period.')
        continue

    # Stats grid
    pdf.sub_title('Performance Statistics')
    col1 = [
        ('Total Trades', str(n)),
        ('Wins / Losses', f'{s["wins"]} / {s["losses"]}'),
        ('Win Rate', f'{s["win_rate"]}%'),
        ('Profit Factor', f'{s["profit_factor"]}'),
        ('Max Win Streak', f'{s["max_win_streak"]}'),
        ('Max Loss Streak', f'{s["max_loss_streak"]}'),
    ]
    col2 = [
        ('Total P&L', f'${s["total_pnl"]:,}'),
        ('Average P&L', f'${s["avg_pnl"]:,}'),
        ('Median P&L', f'${s["median_pnl"]:,}'),
        ('Max Drawdown', f'${s["max_drawdown"]:,}'),
        ('Avg Win', f'${s["avg_win"]:,}'),
        ('Avg Loss', f'${s["avg_loss"]:,}'),
    ]
    col3 = [
        ('Max Win', f'${s["max_win"]:,}'),
        ('Max Loss', f'${s["max_loss"]:,}'),
        ('Avg Wing Width', f'{s["avg_ww"]}pt'),
        ('Avg Entry Credit', f'${s["avg_entry_credit"]}'),
        ('Avg Contracts', f'{s["avg_qty"]}'),
        ('Gross Wins', f'${s["gross_wins"]:,}'),
    ]

    for i in range(len(col1)):
        pdf.set_font('Helvetica', '', 8)
        pdf.set_text_color(100, 100, 100)
        pdf.cell(30, 4, col1[i][0])
        pdf.set_font('Helvetica', 'B', 8)
        pdf.set_text_color(20, 20, 20)
        pdf.cell(30, 4, col1[i][1])

        pdf.set_font('Helvetica', '', 8)
        pdf.set_text_color(100, 100, 100)
        pdf.cell(30, 4, col2[i][0])
        pdf.set_font('Helvetica', 'B', 8)
        pnl_val = col2[i][1]
        if '$-' in pnl_val:
            pdf.set_text_color(200, 0, 0)
        elif '$' in pnl_val and not pnl_val.startswith('$0'):
            pdf.set_text_color(0, 120, 0)
        else:
            pdf.set_text_color(20, 20, 20)
        pdf.cell(30, 4, col2[i][1])

        pdf.set_font('Helvetica', '', 8)
        pdf.set_text_color(100, 100, 100)
        pdf.cell(25, 4, col3[i][0])
        pdf.set_font('Helvetica', 'B', 8)
        pdf.set_text_color(20, 20, 20)
        pdf.cell(0, 4, col3[i][1], new_x='LMARGIN', new_y='NEXT')

    pdf.ln(3)

    # Exit type breakdown
    if s.get('exit_counts'):
        pdf.sub_title('Exit Type Breakdown')
        pdf.set_font('Courier', 'B', 8)
        pdf.set_text_color(20, 20, 20)
        pdf.cell(0, 4.5, f"{'Exit Type':<15} {'Count':>6} {'P&L':>12}", new_x='LMARGIN', new_y='NEXT')
        pdf.set_font('Courier', '', 8)
        for et, cnt in sorted(s['exit_counts'].items()):
            ep = s['exit_pnl'].get(et, 0)
            color = (0, 120, 0) if ep >= 0 else (200, 0, 0)
            pdf.set_text_color(*color)
            pdf.cell(0, 4, f"{et:<15} {cnt:>6} ${ep:>10,}", new_x='LMARGIN', new_y='NEXT')
        pdf.ln(3)

    # V3 fire count distribution
    if ver == 'v3' and s.get('fire_count_dist'):
        pdf.sub_title('PHOENIX Signal Depth')
        pdf.set_font('Courier', 'B', 8)
        pdf.set_text_color(20, 20, 20)
        pdf.cell(0, 4.5, f"{'Signals':>8} {'Trades':>7} {'Wins':>6} {'Win%':>7} {'Total P&L':>12} {'Risk/Trade':>12}", new_x='LMARGIN', new_y='NEXT')
        pdf.set_font('Courier', '', 8)
        tier_map = {'1': '$25K', '2': '$50K', '3': '$75K', '4': '$100K', '5': '$100K'}
        for fc, data in sorted(s['fire_count_dist'].items()):
            wr = data['wins'] / data['count'] * 100 if data['count'] > 0 else 0
            color = (0, 120, 0) if data['pnl'] >= 0 else (200, 0, 0)
            pdf.set_text_color(*color)
            pdf.cell(0, 4, f"{fc+' signal':>8} {data['count']:>7} {data['wins']:>6} {wr:>5.1f}% ${data['pnl']:>10,} {tier_map.get(fc, '$100K'):>12}", new_x='LMARGIN', new_y='NEXT')
        pdf.ln(3)

    # Edge description
    pdf.sub_title('The Edge (Plain English)')
    pdf.body_text(e.get('edge', 'No description available.'))

    # V3 signal details
    if ver == 'v3' and e.get('signals'):
        pdf.sub_title('Signal Group Details')
        for sig_desc in e['signals']:
            pdf.set_font('Helvetica', '', 8)
            pdf.set_text_color(50, 50, 50)
            pdf.multi_cell(0, 4, f'  {sig_desc}')
            pdf.ln(1)

# ─── GLOSSARY ───
pdf.add_page()
pdf.section_title('Glossary of Terms')
terms = [
    ('Iron Butterfly (IBF)', 'A neutral options strategy: sell ATM put + ATM call, buy OTM put + OTM call at equal wing widths. Max profit = credit received. Max loss = wing width minus credit.'),
    ('0DTE', 'Zero Days to Expiration. Options expiring the same day they are traded. Maximum theta decay but also maximum gamma risk.'),
    ('Wing Width', 'Distance in SPX points from the ATM strike to the protective wing strikes. Wider wings = more credit but more risk.'),
    ('VIX', 'CBOE Volatility Index. Measures 30-day implied volatility of SPX options. Higher VIX = more expensive options = fatter butterfly credit.'),
    ('Realized Volatility (RV)', 'Actual market movement computed from 1-minute price changes, annualized. Measures what the market IS doing vs what options SAY it will do.'),
    ('Volatility Premium (VP)', 'Ratio of VIX to Realized Vol. VP > 1 means options are overpriced relative to actual movement. Lower VP = more edge for selling premium.'),
    ('Profit Factor (PF)', 'Gross winning dollars / gross losing dollars. PF > 1.5 is solid. PF > 2.0 is excellent. PF < 1.0 means net loser.'),
    ('Max Drawdown', 'Largest peak-to-trough decline in cumulative P&L. Measures worst-case pain tolerance required to trade the strategy.'),
    ('Regime', 'Classification of market conditions: VIX level + prior day direction + weekly range position + gap direction. Each V4-V14 strategy targets one specific regime.'),
    ('RV Slope', 'Direction of intraday realized vol: RISING (vol expanding), FALLING (vol compressing), STABLE (flat). !RISING filter avoids selling into expanding moves.'),
    ('5d Return', 'Trailing 5-trading-day SPX return. Positive = uptrend context. Used as a trend filter in most PHOENIX signals.'),
    ('Theta', 'Time decay of option premium. Butterflies are net theta-positive: they profit from the passage of time if the underlying stays near the ATM strike.'),
    ('Gamma', 'Rate of change of delta. High gamma near expiration means small price moves create large P&L swings. This is the primary risk in 0DTE butterflies.'),
]
for term, defn in terms:
    pdf.set_font('Helvetica', 'B', 9)
    pdf.set_text_color(20, 20, 20)
    pdf.cell(55, 5, term)
    pdf.set_font('Helvetica', '', 8)
    pdf.set_text_color(60, 60, 60)
    # Use multi_cell for definition but we need to position it
    x = pdf.get_x()
    y = pdf.get_y()
    pdf.set_xy(65, y)
    pdf.multi_cell(125, 4, defn)
    pdf.ln(2)

# ─── SAVE ───
output_path = os.path.join(_DIR, 'PHOENIX_Strategy_Report.pdf')
pdf.output(output_path)
print(f'Report saved to: {output_path}')
print(f'Pages: {pdf.page_no()}')
