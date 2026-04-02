#!/usr/bin/env python3
"""
Polymarket Trade History Analysis Report Generator
Data source: /root/Downloads/Polymarket-History-2026-04-02.csv
"""

import csv
import os
from collections import defaultdict
from datetime import datetime, timezone, timedelta

# ReportLab imports
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable, PageBreak
)
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT

# ─────────────────────────────────────────────
# 1. LOAD & PARSE CSV
# ─────────────────────────────────────────────
CSV_PATH = "/root/Downloads/Polymarket-History-2026-04-02.csv"
OUT_PATH = "/root/.openclaw/workspace-novakash/novakash/docs/real-trade-analysis-2026-04-02.pdf"

os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)

rows = []
with open(CSV_PATH, newline='', encoding='utf-8-sig') as f:
    reader = csv.DictReader(f)
    for row in reader:
        rows.append({
            'marketName': row['marketName'].strip(),
            'action': row['action'].strip(),
            'usdcAmount': float(row['usdcAmount']) if row['usdcAmount'] else 0.0,
            'tokenAmount': float(row['tokenAmount']) if row['tokenAmount'] else 0.0,
            'tokenName': row['tokenName'].strip(),
            'timestamp': int(row['timestamp']),
            'hash': row['hash'].strip(),
        })

# Sort by timestamp ascending
rows.sort(key=lambda r: r['timestamp'])

# ─────────────────────────────────────────────
# 2. SEPARATE BY ACTION TYPE
# ─────────────────────────────────────────────
deposits = [r for r in rows if r['action'] == 'Deposit']
buys = [r for r in rows if r['action'] == 'Buy']
redeems = [r for r in rows if r['action'] == 'Redeem']

total_deposited = sum(r['usdcAmount'] for r in deposits)
total_spent = sum(r['usdcAmount'] for r in buys)
total_redeemed = sum(r['usdcAmount'] for r in redeems)
net_pnl = total_redeemed - total_spent
estimated_balance = total_deposited + net_pnl

# ─────────────────────────────────────────────
# 3. BUILD POSITIONS: match Buy → Redeem by marketName
# ─────────────────────────────────────────────
# Group buys by marketName
buys_by_market = defaultdict(list)
for b in buys:
    buys_by_market[b['marketName']].append(b)

redeems_by_market = defaultdict(list)
for r in redeems:
    redeems_by_market[r['marketName']].append(r)

positions = []
all_markets = set(list(buys_by_market.keys()) + list(redeems_by_market.keys()))

for market in all_markets:
    b_list = buys_by_market.get(market, [])
    r_list = redeems_by_market.get(market, [])
    
    total_cost = sum(b['usdcAmount'] for b in b_list)
    total_payout = sum(r['usdcAmount'] for r in r_list)
    token_names = list(set(b['tokenName'] for b in b_list if b['tokenName']))
    direction = token_names[0] if token_names else 'Unknown'
    
    # Get earliest buy timestamp for this market
    earliest_buy_ts = min(b['timestamp'] for b in b_list) if b_list else 0
    # Check if any redeem happened (resolved)
    has_redeem = len(r_list) > 0
    
    if has_redeem:
        won = total_payout > 0
        outcome = 'WIN' if won else 'LOSS'
    else:
        outcome = 'OPEN'
    
    profit = total_payout - total_cost
    
    # Extract time from market name e.g. "Bitcoin Up or Down - April 2, 9:40AM-9:45AM ET"
    # Parse the time window from market name
    import re
    time_match = re.search(r'(\d+:\d+(?:AM|PM))-(\d+:\d+(?:AM|PM)) ET', market)
    time_str = time_match.group(0) if time_match else 'Unknown'
    
    positions.append({
        'market': market,
        'time_str': time_str,
        'direction': direction,
        'cost': total_cost,
        'payout': total_payout,
        'outcome': outcome,
        'profit': profit,
        'timestamp': earliest_buy_ts,
        'has_redeem': has_redeem,
    })

# Sort positions by timestamp
positions.sort(key=lambda p: p['timestamp'])

# Win rate
resolved = [p for p in positions if p['outcome'] in ('WIN', 'LOSS')]
wins = [p for p in resolved if p['outcome'] == 'WIN']
losses = [p for p in resolved if p['outcome'] == 'LOSS']
open_positions = [p for p in positions if p['outcome'] == 'OPEN']
win_rate = len(wins) / len(resolved) * 100 if resolved else 0

# ─────────────────────────────────────────────
# 4. PERFORMANCE BY TIME PERIOD (ET)
# ─────────────────────────────────────────────
# ET = UTC-4 on April 2 (EDT)
ET_OFFSET = timedelta(hours=-4)

def ts_to_et(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc) + ET_OFFSET

morning_positions = []  # 6:00-12:00 ET
afternoon_positions = []  # 12:00-16:30 ET

for p in resolved:
    dt = ts_to_et(p['timestamp'])
    hour = dt.hour + dt.minute / 60
    if 6 <= hour < 12:
        morning_positions.append(p)
    elif 12 <= hour <= 16.5:
        afternoon_positions.append(p)

def period_stats(pos_list):
    if not pos_list:
        return {'count': 0, 'wins': 0, 'losses': 0, 'win_rate': 0, 'total_cost': 0, 'total_payout': 0, 'net_profit': 0}
    w = [p for p in pos_list if p['outcome'] == 'WIN']
    l = [p for p in pos_list if p['outcome'] == 'LOSS']
    cost = sum(p['cost'] for p in pos_list)
    payout = sum(p['payout'] for p in pos_list)
    return {
        'count': len(pos_list),
        'wins': len(w),
        'losses': len(l),
        'win_rate': len(w) / len(pos_list) * 100 if pos_list else 0,
        'total_cost': cost,
        'total_payout': payout,
        'net_profit': payout - cost,
    }

morning_stats = period_stats(morning_positions)
afternoon_stats = period_stats(afternoon_positions)

# ─────────────────────────────────────────────
# 5. PERFORMANCE BY TOKEN PRICE
# ─────────────────────────────────────────────
# Token price = usdcAmount / tokenAmount per buy
# Group buys into price ranges

price_buckets = {
    '0–10¢': [],
    '10–30¢': [],
    '30–50¢': [],
    '50–70¢': [],
    '70–90¢': [],
    '90–100¢': [],
}

def price_bucket(price_cents):
    if price_cents < 10:
        return '0–10¢'
    elif price_cents < 30:
        return '10–30¢'
    elif price_cents < 50:
        return '30–50¢'
    elif price_cents < 70:
        return '50–70¢'
    elif price_cents < 90:
        return '70–90¢'
    else:
        return '90–100¢'

for b in buys:
    if b['tokenAmount'] > 0:
        price_cents = (b['usdcAmount'] / b['tokenAmount']) * 100
        bucket = price_bucket(price_cents)
        
        # Find outcome for this market
        market = b['marketName']
        pos = next((p for p in positions if p['market'] == market), None)
        outcome = pos['outcome'] if pos else 'OPEN'
        profit = pos['profit'] if pos else 0
        
        price_buckets[bucket].append({
            'usdc': b['usdcAmount'],
            'price_cents': price_cents,
            'outcome': outcome,
            'profit': profit,
            'market': market,
        })

price_bucket_stats = {}
for bucket, entries in price_buckets.items():
    if not entries:
        price_bucket_stats[bucket] = None
        continue
    resolved_e = [e for e in entries if e['outcome'] in ('WIN', 'LOSS')]
    wins_e = [e for e in resolved_e if e['outcome'] == 'WIN']
    total_usdc = sum(e['usdc'] for e in entries)
    total_payout_b = sum(e['usdc'] for e in wins_e)  # proxy; actual payout tracked in positions
    
    # Better: sum actual profits from positions in this bucket
    seen_markets = set()
    actual_profit = 0
    for e in entries:
        if e['market'] not in seen_markets:
            seen_markets.add(e['market'])
            pos = next((p for p in positions if p['market'] == e['market']), None)
            if pos and pos['outcome'] in ('WIN', 'LOSS'):
                actual_profit += pos['profit']
    
    price_bucket_stats[bucket] = {
        'count': len(entries),
        'resolved': len(resolved_e),
        'wins': len(wins_e),
        'win_rate': len(wins_e) / len(resolved_e) * 100 if resolved_e else 0,
        'total_usdc': total_usdc,
        'net_profit': actual_profit,
        'avg_price': sum(e['price_cents'] for e in entries) / len(entries),
    }

# ─────────────────────────────────────────────
# 6. BUILD PDF
# ─────────────────────────────────────────────

# Colour palette
DARK_BG = colors.HexColor('#1a1a2e')
ACCENT_BLUE = colors.HexColor('#0f3460')
ACCENT_TEAL = colors.HexColor('#16213e')
WIN_GREEN = colors.HexColor('#00b894')
LOSS_RED = colors.HexColor('#d63031')
NEUTRAL_GREY = colors.HexColor('#636e72')
LIGHT_GREY = colors.HexColor('#dfe6e9')
WHITE = colors.white
HEADER_BG = colors.HexColor('#0f3460')
ROW_ALT = colors.HexColor('#f8f9fa')

doc = SimpleDocTemplate(
    OUT_PATH,
    pagesize=A4,
    topMargin=20*mm,
    bottomMargin=20*mm,
    leftMargin=18*mm,
    rightMargin=18*mm,
)

styles = getSampleStyleSheet()
W = A4[0] - 36*mm  # usable width

# Custom styles
title_style = ParagraphStyle('Title', parent=styles['Title'],
    fontSize=22, textColor=DARK_BG, spaceAfter=4, fontName='Helvetica-Bold',
    alignment=TA_LEFT)
subtitle_style = ParagraphStyle('Subtitle', parent=styles['Normal'],
    fontSize=11, textColor=NEUTRAL_GREY, spaceAfter=12, fontName='Helvetica')
section_style = ParagraphStyle('Section', parent=styles['Heading2'],
    fontSize=14, textColor=ACCENT_BLUE, spaceBefore=14, spaceAfter=6,
    fontName='Helvetica-Bold', borderPad=0)
body_style = ParagraphStyle('Body', parent=styles['Normal'],
    fontSize=9.5, textColor=colors.HexColor('#2d3436'), spaceAfter=4,
    fontName='Helvetica', leading=14)
small_style = ParagraphStyle('Small', parent=styles['Normal'],
    fontSize=8, textColor=NEUTRAL_GREY, fontName='Helvetica')
caption_style = ParagraphStyle('Caption', parent=styles['Normal'],
    fontSize=8.5, textColor=NEUTRAL_GREY, spaceAfter=8, fontName='Helvetica-Oblique',
    alignment=TA_CENTER)
finding_style = ParagraphStyle('Finding', parent=styles['Normal'],
    fontSize=9.5, textColor=colors.HexColor('#2d3436'), spaceAfter=3,
    fontName='Helvetica', leftIndent=10, leading=14)

def section_header(text):
    return [
        Spacer(1, 4*mm),
        Paragraph(text, section_style),
        HRFlowable(width=W, thickness=1.5, color=ACCENT_BLUE, spaceAfter=4),
    ]

def metric_table(data, col_widths=None):
    """Render a 2-column key/value metrics table."""
    if col_widths is None:
        col_widths = [W*0.45, W*0.55]
    t = Table(data, colWidths=col_widths)
    t.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), colors.white),
        ('FONTNAME', (0,0), (0,-1), 'Helvetica-Bold'),
        ('FONTNAME', (1,0), (1,-1), 'Helvetica'),
        ('FONTSIZE', (0,0), (-1,-1), 9.5),
        ('TEXTCOLOR', (0,0), (0,-1), colors.HexColor('#2d3436')),
        ('TEXTCOLOR', (1,0), (1,-1), colors.HexColor('#0f3460')),
        ('TOPPADDING', (0,0), (-1,-1), 3),
        ('BOTTOMPADDING', (0,0), (-1,-1), 3),
        ('LEFTPADDING', (0,0), (-1,-1), 6),
        ('ROWBACKGROUNDS', (0,0), (-1,-1), [colors.white, ROW_ALT]),
        ('GRID', (0,0), (-1,-1), 0.3, colors.HexColor('#b2bec3')),
        ('ROWBACKGROUNDS', (0,0), (-1,-1), [colors.white, ROW_ALT]),
    ]))
    return t

def pos_row_color(outcome):
    if outcome == 'WIN':
        return colors.HexColor('#e8f8f5')
    elif outcome == 'LOSS':
        return colors.HexColor('#fdf0ef')
    return colors.HexColor('#f0f0f0')

story = []

# ── COVER ──────────────────────────────────────
story.append(Spacer(1, 8*mm))
story.append(Paragraph("Polymarket Trade Analysis", title_style))
story.append(Paragraph("April 2, 2026 — Bitcoin Up/Down Strategy", subtitle_style))
story.append(Paragraph(
    "Source: Official Polymarket history export (ground truth). "
    "All figures in USDC. Timestamps converted to US/Eastern (EDT, UTC-4).",
    small_style))
story.append(Spacer(1, 4*mm))
story.append(HRFlowable(width=W, thickness=2, color=ACCENT_BLUE))
story.append(Spacer(1, 4*mm))

# ── SECTION 1: EXECUTIVE SUMMARY ─────────────────────────
story += section_header("1. Executive Summary")

# KPI boxes via table
pnl_color = WIN_GREEN if net_pnl >= 0 else LOSS_RED
pnl_str = f"{'+'if net_pnl>=0 else ''}{net_pnl:.2f} USDC"
wr_color = WIN_GREEN if win_rate >= 50 else LOSS_RED

kpi_data = [
    ["Total Deposited", f"{total_deposited:.2f} USDC"],
    ["Total Spent on Buys", f"{total_spent:.2f} USDC"],
    ["Total Redeemed (Payouts)", f"{total_redeemed:.2f} USDC"],
    ["Net P&L", pnl_str],
    ["Estimated Current Balance", f"{estimated_balance:.2f} USDC"],
    ["Resolved Positions", f"{len(resolved)}  ({len(wins)} wins / {len(losses)} losses)"],
    ["Win Rate", f"{win_rate:.1f}%"],
    ["Open / Unresolved Positions", str(len(open_positions))],
]

kpi_table = Table(kpi_data, colWidths=[W*0.5, W*0.5])
kpi_table.setStyle(TableStyle([
    ('FONTNAME', (0,0), (0,-1), 'Helvetica-Bold'),
    ('FONTNAME', (1,0), (1,-1), 'Helvetica'),
    ('FONTSIZE', (0,0), (-1,-1), 9.5),
    ('TOPPADDING', (0,0), (-1,-1), 4),
    ('BOTTOMPADDING', (0,0), (-1,-1), 4),
    ('LEFTPADDING', (0,0), (-1,-1), 8),
    ('ROWBACKGROUNDS', (0,0), (-1,-1), [colors.white, ROW_ALT]),
    ('GRID', (0,0), (-1,-1), 0.3, colors.HexColor('#b2bec3')),
    # Colour the P&L row
    ('TEXTCOLOR', (1,3), (1,3), pnl_color),
    ('FONTNAME', (1,3), (1,3), 'Helvetica-Bold'),
    ('TEXTCOLOR', (1,6), (1,6), wr_color),
    ('FONTNAME', (1,6), (1,6), 'Helvetica-Bold'),
]))
story.append(kpi_table)
story.append(Spacer(1, 3*mm))
story.append(Paragraph(
    "Net P&L = Total Redeemed − Total Spent. Balance = Deposits + Net P&L. "
    "Win rate counts only fully resolved positions (Redeem > 0 = WIN, Redeem = 0 = LOSS).",
    caption_style))

# ── SECTION 2: POSITION TIMELINE ─────────────────────────
story += section_header("2. Position Timeline")

# Header
tl_header = ['Market Window', 'Dir', 'Cost', 'Payout', 'Outcome', 'Profit']
tl_col_widths = [W*0.38, W*0.07, W*0.11, W*0.11, W*0.13, W*0.12]

tl_data = [tl_header]
row_colors_cmd = [
    ('BACKGROUND', (0,0), (-1,0), HEADER_BG),
    ('TEXTCOLOR', (0,0), (-1,0), WHITE),
    ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
    ('FONTSIZE', (0,0), (-1,0), 8.5),
]

for i, p in enumerate(positions):
    # Extract time window from market name
    import re
    time_match = re.search(r'(\d+:\d+(?:AM|PM)-\d+:\d+(?:AM|PM) ET)', p['market'])
    time_w = time_match.group(1) if time_match else p['time_str']
    
    profit_str = f"{'+'if p['profit']>=0 else ''}{p['profit']:.2f}"
    out_str = p['outcome']
    
    row = [
        Paragraph(time_w, ParagraphStyle('cell', fontSize=8, leading=10, fontName='Helvetica')),
        p['direction'][:2] if p['direction'] else '?',
        f"{p['cost']:.2f}",
        f"{p['payout']:.2f}",
        out_str,
        profit_str,
    ]
    tl_data.append(row)
    
    row_idx = i + 1
    if p['outcome'] == 'WIN':
        row_colors_cmd.append(('BACKGROUND', (0,row_idx), (-1,row_idx), colors.HexColor('#e8f8f5')))
        row_colors_cmd.append(('TEXTCOLOR', (4,row_idx), (4,row_idx), WIN_GREEN))
        row_colors_cmd.append(('TEXTCOLOR', (5,row_idx), (5,row_idx), WIN_GREEN))
    elif p['outcome'] == 'LOSS':
        row_colors_cmd.append(('BACKGROUND', (0,row_idx), (-1,row_idx), colors.HexColor('#fdf0ef')))
        row_colors_cmd.append(('TEXTCOLOR', (4,row_idx), (4,row_idx), LOSS_RED))
        row_colors_cmd.append(('TEXTCOLOR', (5,row_idx), (5,row_idx), LOSS_RED))
    else:  # OPEN
        row_colors_cmd.append(('TEXTCOLOR', (4,row_idx), (4,row_idx), NEUTRAL_GREY))

tl_table = Table(tl_data, colWidths=tl_col_widths, repeatRows=1)
base_style = [
    ('FONTSIZE', (0,1), (-1,-1), 8),
    ('FONTNAME', (0,1), (-1,-1), 'Helvetica'),
    ('TOPPADDING', (0,0), (-1,-1), 3),
    ('BOTTOMPADDING', (0,0), (-1,-1), 3),
    ('LEFTPADDING', (0,0), (-1,-1), 4),
    ('GRID', (0,0), (-1,-1), 0.3, colors.HexColor('#b2bec3')),
    ('ALIGN', (2,0), (-1,-1), 'RIGHT'),
    ('ALIGN', (4,0), (4,-1), 'CENTER'),
    ('FONTNAME', (4,0), (4,-1), 'Helvetica-Bold'),
]
tl_table.setStyle(TableStyle(row_colors_cmd + base_style))
story.append(tl_table)
story.append(Spacer(1, 2*mm))
story.append(Paragraph(
    "Dir = Direction traded (Up/Down). Cost and Payout in USDC. "
    "OPEN = position bought but no Redeem record yet.",
    caption_style))

story.append(PageBreak())

# ── SECTION 3: PERFORMANCE BY TIME PERIOD ────────────────
story += section_header("3. Performance by Time Period (ET)")

period_header = ['Period', 'Positions', 'Wins', 'Losses', 'Win Rate', 'Total Cost', 'Total Payout', 'Net Profit']
period_col_w = [W*0.15, W*0.09, W*0.07, W*0.09, W*0.09, W*0.13, W*0.14, W*0.12]

def period_row(label, s, color=None):
    wr_c = WIN_GREEN if s['win_rate'] >= 50 else LOSS_RED
    np_c = WIN_GREEN if s['net_profit'] >= 0 else LOSS_RED
    return [
        label,
        str(s['count']),
        str(s['wins']),
        str(s['losses']),
        f"{s['win_rate']:.1f}%",
        f"{s['total_cost']:.2f}",
        f"{s['total_payout']:.2f}",
        f"{'+'if s['net_profit']>=0 else ''}{s['net_profit']:.2f}",
    ]

p_data = [period_header]
p_data.append(period_row('Morning\n6:00–12:00', morning_stats))
p_data.append(period_row('Afternoon\n12:00–16:30', afternoon_stats))

all_stats = period_stats(resolved)
p_data.append(period_row('ALL DAY', all_stats))

p_table = Table(p_data, colWidths=period_col_w, repeatRows=1)
p_style_cmds = [
    ('BACKGROUND', (0,0), (-1,0), HEADER_BG),
    ('TEXTCOLOR', (0,0), (-1,0), WHITE),
    ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
    ('FONTSIZE', (0,0), (-1,-1), 8.5),
    ('FONTNAME', (0,1), (-1,-1), 'Helvetica'),
    ('TOPPADDING', (0,0), (-1,-1), 4),
    ('BOTTOMPADDING', (0,0), (-1,-1), 4),
    ('LEFTPADDING', (0,0), (-1,-1), 4),
    ('GRID', (0,0), (-1,-1), 0.3, colors.HexColor('#b2bec3')),
    ('ALIGN', (1,0), (-1,-1), 'CENTER'),
    # Morning row
    ('BACKGROUND', (0,1), (-1,1), colors.HexColor('#e8f8f5')),
    # Afternoon row
    ('BACKGROUND', (0,2), (-1,2), colors.HexColor('#fdf0ef')),
    # ALL row
    ('BACKGROUND', (0,3), (-1,3), colors.HexColor('#eaf2ff')),
    ('FONTNAME', (0,3), (-1,3), 'Helvetica-Bold'),
]

# Colour win rate cells
for row_idx, stats in [(1, morning_stats), (2, afternoon_stats), (3, all_stats)]:
    wr_color = WIN_GREEN if stats['win_rate'] >= 50 else LOSS_RED
    np_color = WIN_GREEN if stats['net_profit'] >= 0 else LOSS_RED
    p_style_cmds.append(('TEXTCOLOR', (4, row_idx), (4, row_idx), wr_color))
    p_style_cmds.append(('FONTNAME', (4, row_idx), (4, row_idx), 'Helvetica-Bold'))
    p_style_cmds.append(('TEXTCOLOR', (7, row_idx), (7, row_idx), np_color))
    p_style_cmds.append(('FONTNAME', (7, row_idx), (7, row_idx), 'Helvetica-Bold'))

p_table.setStyle(TableStyle(p_style_cmds))
story.append(p_table)
story.append(Spacer(1, 3*mm))

# Cumulative P&L by position (running total)
story.append(Paragraph("Running P&L through the day:", body_style))
running_pl = 0
running_rows = [['#', 'Market Window', 'Outcome', 'Trade P&L', 'Running P&L']]
for i, p in enumerate(resolved):
    running_pl += p['profit']
    import re
    time_match = re.search(r'(\d+:\d+(?:AM|PM)-\d+:\d+(?:AM|PM) ET)', p['market'])
    time_w = time_match.group(1) if time_match else p['market'][-30:]
    running_rows.append([
        str(i+1),
        time_w,
        p['outcome'],
        f"{'+'if p['profit']>=0 else ''}{p['profit']:.2f}",
        f"{'+'if running_pl>=0 else ''}{running_pl:.2f}",
    ])

run_table = Table(running_rows, colWidths=[W*0.05, W*0.38, W*0.12, W*0.20, W*0.20], repeatRows=1)
run_cmds = [
    ('BACKGROUND', (0,0), (-1,0), HEADER_BG),
    ('TEXTCOLOR', (0,0), (-1,0), WHITE),
    ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
    ('FONTSIZE', (0,0), (-1,-1), 7.5),
    ('FONTNAME', (0,1), (-1,-1), 'Helvetica'),
    ('TOPPADDING', (0,0), (-1,-1), 2.5),
    ('BOTTOMPADDING', (0,0), (-1,-1), 2.5),
    ('LEFTPADDING', (0,0), (-1,-1), 4),
    ('GRID', (0,0), (-1,-1), 0.3, colors.HexColor('#b2bec3')),
    ('ALIGN', (2,0), (-1,-1), 'CENTER'),
]
for i, p in enumerate(resolved):
    row_idx = i+1
    o_color = WIN_GREEN if p['outcome'] == 'WIN' else LOSS_RED
    run_cmds.append(('TEXTCOLOR', (2, row_idx), (2, row_idx), o_color))
    run_cmds.append(('FONTNAME', (2, row_idx), (2, row_idx), 'Helvetica-Bold'))
    pl_color = WIN_GREEN if p['profit'] >= 0 else LOSS_RED
    run_cmds.append(('TEXTCOLOR', (3, row_idx), (3, row_idx), pl_color))
    run_pl_v = sum(pos['profit'] for pos in resolved[:i+1])
    rpl_color = WIN_GREEN if run_pl_v >= 0 else LOSS_RED
    run_cmds.append(('TEXTCOLOR', (4, row_idx), (4, row_idx), rpl_color))
    if i % 2 == 1:
        run_cmds.append(('BACKGROUND', (0, row_idx), (-1, row_idx), ROW_ALT))

run_table.setStyle(TableStyle(run_cmds))
story.append(run_table)

story.append(PageBreak())

# ── SECTION 4: PERFORMANCE BY TOKEN PRICE ────────────────
story += section_header("4. Performance by Token Price")

story.append(Paragraph(
    "Token price = USDC paid ÷ tokens received per trade. "
    "Lower prices = more speculative bets; higher prices = confident directional plays.",
    body_style))
story.append(Spacer(1, 2*mm))

tp_header = ['Price Range', 'Trades', 'Resolved', 'Wins', 'Win Rate', 'USDC Spent', 'Net Profit', 'Avg Price']
tp_col_w = [W*0.12, W*0.07, W*0.09, W*0.07, W*0.10, W*0.13, W*0.14, W*0.12]

tp_data = [tp_header]
for bucket, s in price_bucket_stats.items():
    if s is None:
        tp_data.append([bucket, '0', '-', '-', '-', '-', '-', '-'])
    else:
        np_str = f"{'+'if s['net_profit']>=0 else ''}{s['net_profit']:.2f}"
        wr_str = f"{s['win_rate']:.1f}%" if s['resolved'] > 0 else 'N/A'
        tp_data.append([
            bucket,
            str(s['count']),
            str(s['resolved']),
            str(s['wins']),
            wr_str,
            f"{s['total_usdc']:.2f}",
            np_str,
            f"{s['avg_price']:.1f}¢",
        ])

tp_table = Table(tp_data, colWidths=tp_col_w, repeatRows=1)
tp_cmds = [
    ('BACKGROUND', (0,0), (-1,0), HEADER_BG),
    ('TEXTCOLOR', (0,0), (-1,0), WHITE),
    ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
    ('FONTSIZE', (0,0), (-1,-1), 8.5),
    ('FONTNAME', (0,1), (-1,-1), 'Helvetica'),
    ('TOPPADDING', (0,0), (-1,-1), 4),
    ('BOTTOMPADDING', (0,0), (-1,-1), 4),
    ('LEFTPADDING', (0,0), (-1,-1), 5),
    ('GRID', (0,0), (-1,-1), 0.3, colors.HexColor('#b2bec3')),
    ('ALIGN', (1,0), (-1,-1), 'CENTER'),
    ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, ROW_ALT]),
]
for i, (bucket, s) in enumerate(price_bucket_stats.items()):
    row_idx = i + 1
    if s is not None and s['resolved'] > 0:
        wr_color = WIN_GREEN if s['win_rate'] >= 50 else LOSS_RED
        tp_cmds.append(('TEXTCOLOR', (4, row_idx), (4, row_idx), wr_color))
        tp_cmds.append(('FONTNAME', (4, row_idx), (4, row_idx), 'Helvetica-Bold'))
    if s is not None:
        np_color = WIN_GREEN if s['net_profit'] >= 0 else LOSS_RED
        tp_cmds.append(('TEXTCOLOR', (6, row_idx), (6, row_idx), np_color))
        tp_cmds.append(('FONTNAME', (6, row_idx), (6, row_idx), 'Helvetica-Bold'))

tp_table.setStyle(TableStyle(tp_cmds))
story.append(tp_table)
story.append(Spacer(1, 3*mm))
story.append(Paragraph(
    "Net Profit per bucket de-duplicates multi-buy positions (counts each market once). "
    "USDC Spent sums all individual buys in that price range.",
    caption_style))

# ── SECTION 5: KEY FINDINGS ───────────────────────────────
story += section_header("5. Key Findings")

# Find best and worst positions
best_pos = max(resolved, key=lambda p: p['profit']) if resolved else None
worst_pos = min(resolved, key=lambda p: p['profit']) if resolved else None

# Morning vs afternoon detail
morning_profit = morning_stats['net_profit']
afternoon_profit = afternoon_stats['net_profit']

# Token price distribution
most_used_bucket = max(
    [(b, s) for b, s in price_bucket_stats.items() if s is not None],
    key=lambda x: x[1]['count']
)
best_win_rate_bucket = max(
    [(b, s) for b, s in price_bucket_stats.items() if s is not None and s['resolved'] > 0],
    key=lambda x: x[1]['win_rate'],
    default=(None, None)
)
cheapest_bucket_data = price_bucket_stats.get('0–10¢')
expensive_buys = sum(s['count'] for b, s in price_bucket_stats.items() 
                     if s is not None and b in ('70–90¢', '90–100¢'))
cheap_buys = sum(s['count'] for b, s in price_bucket_stats.items() 
                 if s is not None and b in ('0–10¢', '10–30¢'))

findings = []

# Morning
if morning_stats['count'] > 0:
    m_wr = morning_stats['win_rate']
    findings.append(f"✅  Morning (6:00–12:00 ET): {morning_stats['count']} positions resolved. "
                   f"Win rate {m_wr:.1f}%. Net P&L: {'+'if morning_profit>=0 else ''}{morning_profit:.2f} USDC. "
                   f"{'Strong positive performance.' if morning_profit > 0 else 'Net negative even in morning.'}")
else:
    findings.append("Morning: No resolved positions found.")

# Afternoon
if afternoon_stats['count'] > 0:
    a_wr = afternoon_stats['win_rate']
    findings.append(f"⚠️  Afternoon (12:00–16:30 ET): {afternoon_stats['count']} positions resolved. "
                   f"Win rate {a_wr:.1f}%. Net P&L: {'+'if afternoon_profit>=0 else ''}{afternoon_profit:.2f} USDC. "
                   f"{'Performance deteriorated in afternoon.' if afternoon_profit < morning_profit else 'Afternoon maintained or improved on morning.'}")

# Best/worst
if best_pos:
    import re
    tw = re.search(r'(\d+:\d+(?:AM|PM)-\d+:\d+(?:AM|PM) ET)', best_pos['market'])
    tw = tw.group(1) if tw else best_pos['market']
    findings.append(f"🏆  Best trade: {tw} — Profit: +{best_pos['profit']:.2f} USDC "
                   f"(cost {best_pos['cost']:.2f}, payout {best_pos['payout']:.2f})")
if worst_pos:
    tw = re.search(r'(\d+:\d+(?:AM|PM)-\d+(?:AM|PM) ET)', worst_pos['market'])
    if not tw:
        tw = re.search(r'(\d+:\d+(?:AM|PM)-\d+:\d+(?:AM|PM) ET)', worst_pos['market'])
    tw = tw.group(1) if tw else worst_pos['market']
    findings.append(f"💸  Worst trade: {tw} — Loss: {worst_pos['profit']:.2f} USDC "
                   f"(cost {worst_pos['cost']:.2f}, payout {worst_pos['payout']:.2f})")

# Token price
findings.append(f"📊  Token price distribution: {cheap_buys} buys at <30¢ "
               f"vs {expensive_buys} buys at >70¢. "
               f"Most trades in bucket: {most_used_bucket[0]} ({most_used_bucket[1]['count']} trades).")
if best_win_rate_bucket[0]:
    findings.append(f"💡  Best win-rate bucket: {best_win_rate_bucket[0]} "
                   f"({best_win_rate_bucket[1]['win_rate']:.1f}% win rate, "
                   f"{best_win_rate_bucket[1]['wins']}/{best_win_rate_bucket[1]['resolved']} resolved).")

# Open positions note
if open_positions:
    open_cost = sum(p['cost'] for p in open_positions)
    findings.append(f"🔓  {len(open_positions)} open position(s) still unresolved, "
                   f"cost {open_cost:.2f} USDC (not yet included in P&L).")

for f in findings:
    story.append(Paragraph(f"• {f}", finding_style))
    story.append(Spacer(1, 1.5*mm))

# ── SECTION 6: RECOMMENDATIONS ───────────────────────────
story += section_header("6. Recommendations")

# Determine optimal price range
if best_win_rate_bucket[0]:
    opt_range = best_win_rate_bucket[0]
    opt_wr = best_win_rate_bucket[1]['win_rate']
else:
    opt_range = "30–50¢"
    opt_wr = 0.0

recs = [
    f"📌  Optimal token price range: Focus on {opt_range} tokens where win rate was highest ({opt_wr:.1f}%). "
    f"Avoid sub-10¢ tokens — these are near-zero probability bets with asymmetric downside.",

    f"🔒  Add a max token price cap: Consider capping buys at 70¢ per token. "
    f"Buying tokens above 70¢ means you're paying premium for high-confidence plays "
    f"that still resolved as losses — poor risk/reward.",
]

if morning_profit > 0 and afternoon_profit < 0:
    recs.append(
        f"⏰  Implement a session stop-loss: Morning was {'profitable' if morning_profit > 0 else 'breakeven'} "
        f"({morning_profit:+.2f} USDC). Afternoon erased those gains ({afternoon_profit:+.2f} USDC). "
        f"A hard stop after -10 USDC in a session would have preserved morning profits."
    )
elif morning_profit <= 0:
    recs.append(
        f"⏰  Strategy struggled across all periods. Consider tightening entry criteria: "
        f"only trade when model confidence is above a threshold, not just any signal."
    )

recs += [
    "📉  Size management: The largest single losses came from positions where $20–30+ USDC was staked. "
    "Cap individual position size at $15 USDC to flatten the loss curve.",

    "🔄  Direction bias audit: Most trades were 'Down' bets. Verify the signal generator "
    "isn't structurally biased — test Up/Down signal distribution over more sessions.",

    "📊  Track token price at entry: Add avg_entry_price to the DB schema. "
    "This report shows price matters — the engine should factor it into position sizing.",
]

for r in recs:
    story.append(Paragraph(f"• {r}", finding_style))
    story.append(Spacer(1, 2*mm))

# ── FOOTER NOTE ──────────────────────────────────────────
story.append(Spacer(1, 6*mm))
story.append(HRFlowable(width=W, thickness=0.5, color=NEUTRAL_GREY))
story.append(Spacer(1, 2*mm))
story.append(Paragraph(
    f"Generated by Novakash Analysis Engine • Data: Polymarket CSV export • "
    f"Report date: April 2, 2026 • All times in US/Eastern (EDT, UTC-4) • "
    f"Source hash (first row): {rows[0]['hash'][:20]}...",
    ParagraphStyle('Footer', parent=styles['Normal'], fontSize=7,
                   textColor=NEUTRAL_GREY, alignment=TA_CENTER)
))

# ── BUILD ────────────────────────────────────────────────
doc.build(story)
print(f"✅ PDF saved to: {OUT_PATH}")
print(f"\nSummary:")
print(f"  Deposited:  ${total_deposited:.2f}")
print(f"  Spent:      ${total_spent:.2f}")
print(f"  Redeemed:   ${total_redeemed:.2f}")
print(f"  Net P&L:    {net_pnl:+.2f}")
print(f"  Balance:    ${estimated_balance:.2f}")
print(f"  Win Rate:   {win_rate:.1f}% ({len(wins)}/{len(resolved)})")
print(f"  Positions:  {len(positions)} total, {len(resolved)} resolved, {len(open_positions)} open")
