"""
Signal Mathematics PDF Generator — Novakash v3.1
Generates a comprehensive PDF explaining the trading system's signal mathematics.
"""

import os
import sys
import io
import json
import math
import warnings
warnings.filterwarnings('ignore')

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyArrowPatch
import matplotlib.patheffects as pe
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.gridspec import GridSpec

import psycopg2

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm, cm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.colors import (
    Color, HexColor, black, white, grey
)
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle,
    PageBreak, HRFlowable, KeepTogether
)
from reportlab.platypus.flowables import Flowable
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT, TA_JUSTIFY
from reportlab.pdfgen import canvas as pdf_canvas

# ── Colour Palette ─────────────────────────────────────────────────────────────
BG_DARK      = HexColor('#0D1117')
BG_CARD      = HexColor('#161B22')
BG_CARD2     = HexColor('#1C2230')
ACCENT_BLUE  = HexColor('#58A6FF')
ACCENT_GREEN = HexColor('#3FB950')
ACCENT_RED   = HexColor('#F85149')
ACCENT_AMBER = HexColor('#D29922')
ACCENT_PURPLE= HexColor('#BC8CFF')
TEXT_PRIMARY = HexColor('#E6EDF3')
TEXT_MUTED   = HexColor('#8B949E')
BORDER_COLOR = HexColor('#30363D')
GOLD         = HexColor('#F0B429')

# Matplotlib equivalents
MPL_BG       = '#0D1117'
MPL_CARD     = '#161B22'
MPL_BLUE     = '#58A6FF'
MPL_GREEN    = '#3FB950'
MPL_RED      = '#F85149'
MPL_AMBER    = '#D29922'
MPL_PURPLE   = '#BC8CFF'
MPL_TEXT     = '#E6EDF3'
MPL_MUTED    = '#8B949E'
MPL_BORDER   = '#30363D'

OUTPUT_PATH = "/root/.openclaw/workspace-novakash/novakash/docs/signal-mathematics-v3.1.pdf"

# ── Matplotlib Style ──────────────────────────────────────────────────────────
def setup_mpl_style():
    plt.rcParams.update({
        'figure.facecolor': MPL_BG,
        'axes.facecolor': MPL_CARD,
        'axes.edgecolor': MPL_BORDER,
        'axes.labelcolor': MPL_TEXT,
        'axes.titlecolor': MPL_TEXT,
        'text.color': MPL_TEXT,
        'xtick.color': MPL_MUTED,
        'ytick.color': MPL_MUTED,
        'grid.color': MPL_BORDER,
        'grid.linewidth': 0.5,
        'grid.alpha': 0.6,
        'font.family': 'sans-serif',
        'font.size': 10,
        'axes.titlesize': 12,
        'axes.labelsize': 10,
        'legend.facecolor': MPL_CARD,
        'legend.edgecolor': MPL_BORDER,
        'legend.labelcolor': MPL_TEXT,
    })

setup_mpl_style()


def fig_to_image(fig, width_mm=170, height_mm=None):
    """Convert matplotlib figure to ReportLab Image flowable."""
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=150, bbox_inches='tight',
                facecolor=MPL_BG, edgecolor='none')
    buf.seek(0)
    plt.close(fig)

    from reportlab.lib.units import mm
    w = width_mm * mm
    if height_mm:
        h = height_mm * mm
        return Image(buf, width=w, height=h)
    else:
        # Auto aspect
        img = Image(buf, width=w)
        return img


# ── Delta Weight Function (from window_evaluator.py) ──────────────────────────
def delta_weight(abs_delta):
    """Exact replica of the delta_weight logic from window_evaluator.py"""
    if abs_delta > 0.15:
        return 3.0
    elif abs_delta > 0.10:
        return 2.0 + (abs_delta - 0.10) / 0.05 * 1.0
    elif abs_delta > 0.05:
        return 1.5 + (abs_delta - 0.05) / 0.05 * 0.5
    elif abs_delta > 0.02:
        return 0.5 + (abs_delta - 0.02) / 0.03 * 1.0
    elif abs_delta > 0.005:
        return abs_delta / 0.02 * 0.5
    else:
        return 0.0


# ── VPIN Weight Function ───────────────────────────────────────────────────────
def vpin_weight(vpin):
    """From window_evaluator.py: min((vpin - 0.30) * 10, 3.0)"""
    if vpin <= 0.30:
        return 0.0
    return min((vpin - 0.30) * 10, 3.0)


# ── Fetch DB Data ──────────────────────────────────────────────────────────────
def fetch_trade_data():
    try:
        conn = psycopg2.connect(
            'postgresql://postgres:wKbsHjsWoWaUKkzSqgCUIijtnOKHIcQj@hopper.proxy.rlwy.net:35772/railway',
            connect_timeout=10
        )
        cur = conn.cursor()
        cur.execute("""
            SELECT metadata->>'delta_pct' as delta, metadata->>'vpin' as vpin,
                   metadata->>'confidence' as conf, outcome, pnl_usd, entry_price
            FROM trades WHERE mode = 'live' AND outcome IS NOT NULL;
        """)
        rows = cur.fetchall()
        conn.close()

        trades = []
        for row in rows:
            try:
                delta = float(row[0]) if row[0] else None
                vpin  = float(row[1]) if row[1] else None
                conf  = float(row[2]) if row[2] else None
                outcome = row[3]
                pnl   = float(row[4]) if row[4] else 0.0
                ep    = float(row[5]) if row[5] else None
                if delta is not None and vpin is not None:
                    trades.append({
                        'delta': abs(delta),
                        'vpin': vpin,
                        'conf': conf or 0.5,
                        'outcome': outcome,
                        'pnl': pnl,
                        'entry_price': ep,
                    })
            except Exception:
                pass
        return trades
    except Exception as e:
        print(f"DB connection failed: {e}")
        return []


# ── ReportLab Styles ───────────────────────────────────────────────────────────
def make_styles():
    base = getSampleStyleSheet()

    styles = {}

    styles['cover_title'] = ParagraphStyle(
        'cover_title',
        fontName='Helvetica-Bold',
        fontSize=28,
        textColor=TEXT_PRIMARY,
        leading=36,
        alignment=TA_CENTER,
        spaceAfter=6,
    )
    styles['cover_subtitle'] = ParagraphStyle(
        'cover_subtitle',
        fontName='Helvetica',
        fontSize=14,
        textColor=ACCENT_BLUE,
        leading=20,
        alignment=TA_CENTER,
        spaceAfter=4,
    )
    styles['cover_version'] = ParagraphStyle(
        'cover_version',
        fontName='Helvetica',
        fontSize=11,
        textColor=TEXT_MUTED,
        alignment=TA_CENTER,
    )
    styles['h1'] = ParagraphStyle(
        'h1',
        fontName='Helvetica-Bold',
        fontSize=20,
        textColor=TEXT_PRIMARY,
        leading=26,
        spaceBefore=8,
        spaceAfter=8,
    )
    styles['h2'] = ParagraphStyle(
        'h2',
        fontName='Helvetica-Bold',
        fontSize=14,
        textColor=ACCENT_BLUE,
        leading=20,
        spaceBefore=12,
        spaceAfter=6,
    )
    styles['h3'] = ParagraphStyle(
        'h3',
        fontName='Helvetica-Bold',
        fontSize=11,
        textColor=ACCENT_AMBER,
        leading=16,
        spaceBefore=8,
        spaceAfter=4,
    )
    styles['body'] = ParagraphStyle(
        'body',
        fontName='Helvetica',
        fontSize=10,
        textColor=TEXT_PRIMARY,
        leading=16,
        spaceAfter=6,
        alignment=TA_JUSTIFY,
    )
    styles['body_muted'] = ParagraphStyle(
        'body_muted',
        fontName='Helvetica',
        fontSize=9,
        textColor=TEXT_MUTED,
        leading=14,
        spaceAfter=4,
    )
    styles['code'] = ParagraphStyle(
        'code',
        fontName='Courier',
        fontSize=9,
        textColor=ACCENT_GREEN,
        leading=14,
        leftIndent=12,
        spaceAfter=4,
    )
    styles['equation'] = ParagraphStyle(
        'equation',
        fontName='Helvetica-Bold',
        fontSize=11,
        textColor=ACCENT_AMBER,
        leading=18,
        alignment=TA_CENTER,
        spaceBefore=8,
        spaceAfter=8,
    )
    styles['caption'] = ParagraphStyle(
        'caption',
        fontName='Helvetica-Oblique',
        fontSize=9,
        textColor=TEXT_MUTED,
        leading=13,
        alignment=TA_CENTER,
        spaceAfter=8,
    )
    styles['label'] = ParagraphStyle(
        'label',
        fontName='Helvetica-Bold',
        fontSize=9,
        textColor=TEXT_MUTED,
        leading=12,
    )
    styles['table_header'] = ParagraphStyle(
        'table_header',
        fontName='Helvetica-Bold',
        fontSize=9,
        textColor=TEXT_PRIMARY,
        leading=12,
        alignment=TA_CENTER,
    )
    styles['table_cell'] = ParagraphStyle(
        'table_cell',
        fontName='Helvetica',
        fontSize=9,
        textColor=TEXT_PRIMARY,
        leading=13,
        alignment=TA_CENTER,
    )
    styles['table_cell_left'] = ParagraphStyle(
        'table_cell_left',
        fontName='Helvetica',
        fontSize=9,
        textColor=TEXT_PRIMARY,
        leading=13,
        alignment=TA_LEFT,
    )
    styles['highlight'] = ParagraphStyle(
        'highlight',
        fontName='Helvetica-Bold',
        fontSize=10,
        textColor=ACCENT_GREEN,
        leading=16,
        alignment=TA_CENTER,
    )
    styles['section_num'] = ParagraphStyle(
        'section_num',
        fontName='Helvetica-Bold',
        fontSize=11,
        textColor=ACCENT_PURPLE,
        leading=16,
    )
    return styles


# ── Page Background ────────────────────────────────────────────────────────────
def add_background(canvas, doc):
    """Draw dark background on every page."""
    canvas.saveState()
    canvas.setFillColor(BG_DARK)
    canvas.rect(0, 0, A4[0], A4[1], fill=1, stroke=0)

    # Subtle top gradient bar
    canvas.setFillColor(ACCENT_BLUE)
    canvas.setFillAlpha(0.08)
    canvas.rect(0, A4[1] - 4*mm, A4[0], 4*mm, fill=1, stroke=0)

    # Footer line
    canvas.setFillAlpha(1.0)
    canvas.setStrokeColor(BORDER_COLOR)
    canvas.setLineWidth(0.5)
    canvas.line(20*mm, 14*mm, A4[0] - 20*mm, 14*mm)

    # Footer text
    canvas.setFont('Helvetica', 8)
    canvas.setFillColor(TEXT_MUTED)
    canvas.drawString(20*mm, 10*mm, 'Novakash Trading System — Signal Mathematics v3.1')
    canvas.drawRightString(A4[0] - 20*mm, 10*mm, f'Page {doc.page}')

    canvas.restoreState()


# ── Cover Page Figure ──────────────────────────────────────────────────────────
def make_cover_figure():
    """Hero figure for cover: animated signal flow concept."""
    fig, ax = plt.subplots(figsize=(10, 3.5))
    ax.set_facecolor(MPL_BG)
    fig.patch.set_facecolor(MPL_BG)
    ax.set_xlim(0, 10)
    ax.set_ylim(-1.5, 1.5)
    ax.axis('off')

    # Draw VPIN signal wave
    x = np.linspace(0, 10, 500)
    # Simulate a rising VPIN signal with spike
    vpin_signal = 0.5 + 0.15 * np.sin(x * 1.5) + np.where(x > 6, 0.3 * np.exp(-(x-7)**2 / 0.5), 0)
    # Normalise to -1..1 range for display
    disp = (vpin_signal - 0.5) * 3
    ax.plot(x, disp, color=MPL_BLUE, linewidth=2.0, alpha=0.9, label='VPIN Signal')

    # Price trajectory
    price = 0.2 * np.sin(x * 0.8 + 0.5) + np.where(x > 6.5, 0.5 * (x - 6.5) * 0.4, 0)
    ax.plot(x, price, color=MPL_GREEN, linewidth=1.5, alpha=0.8, linestyle='--', label='BTC Price Δ')

    # Threshold line
    ax.axhline(y=0.6, color=MPL_AMBER, linewidth=0.8, linestyle=':', alpha=0.6)
    ax.text(0.3, 0.65, 'Entry Threshold', color=MPL_AMBER, fontsize=8, alpha=0.8)

    # Fire marker
    fire_x = 7.0
    ax.scatter([fire_x], [0.9], color=MPL_GREEN, s=120, zorder=5, marker='*')
    ax.annotate('FIRE', xy=(fire_x, 0.9), xytext=(fire_x + 0.6, 1.1),
                color=MPL_GREEN, fontsize=9, fontweight='bold',
                arrowprops=dict(arrowstyle='->', color=MPL_GREEN, lw=1.2))

    ax.legend(loc='upper left', fontsize=8, framealpha=0.3)
    ax.set_title('Signal Confluence → Entry Decision', color=MPL_TEXT, fontsize=11, pad=8)

    plt.tight_layout(pad=0.5)
    return fig


# ── Section 2: Delta Weight Chart ─────────────────────────────────────────────
def make_delta_weight_chart():
    fig, ax = plt.subplots(figsize=(8, 3.8))

    x = np.linspace(0, 0.20, 500)
    y = [delta_weight(v) for v in x]

    # Gradient fill
    ax.fill_between(x * 100, y, alpha=0.25, color=MPL_BLUE)
    ax.plot(x * 100, y, color=MPL_BLUE, linewidth=2.5)

    # Annotate breakpoints
    breakpoints = [
        (0.005, 'Min'),
        (0.02, 'Min gate\n(0.5w)', 'below'),
        (0.05, '0.02% gate', 'below'),
        (0.10, '1.5w'),
        (0.15, '2.0w'),
    ]
    bp_x = [0.005, 0.02, 0.05, 0.10, 0.15]
    bp_y = [delta_weight(v) for v in bp_x]
    ax.scatter([v*100 for v in bp_x], bp_y, color=MPL_AMBER, s=60, zorder=5, alpha=0.9)

    ax.axvline(x=0.02*100, color=MPL_AMBER, linewidth=1, linestyle='--', alpha=0.5)
    ax.text(0.02*100 + 0.1, 0.2, '0.02% gate', color=MPL_AMBER, fontsize=8, alpha=0.8)
    ax.axvline(x=0.05*100, color=MPL_MUTED, linewidth=0.8, linestyle=':', alpha=0.4)
    ax.axvline(x=0.10*100, color=MPL_MUTED, linewidth=0.8, linestyle=':', alpha=0.4)
    ax.axvline(x=0.15*100, color=MPL_MUTED, linewidth=0.8, linestyle=':', alpha=0.4)

    # Morning win zone shading
    ax.axvspan(0.03*100, 0.09*100, alpha=0.10, color=MPL_GREEN, label='Morning win zone')
    ax.text(0.05*100, 2.7, '73% win zone', color=MPL_GREEN, fontsize=8, alpha=0.8, ha='center')

    ax.set_xlabel('|Δ Price| (%)', color=MPL_TEXT, fontsize=10)
    ax.set_ylabel('Delta Weight', color=MPL_TEXT, fontsize=10)
    ax.set_title('Delta Weight vs Price Change', color=MPL_TEXT, fontsize=12, fontweight='bold')
    ax.set_xlim(0, 0.20*100)
    ax.set_ylim(0, 3.2)
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8)

    # Y-axis reference lines
    for y_ref in [0.5, 1.0, 1.5, 2.0, 3.0]:
        ax.axhline(y=y_ref, color=MPL_BORDER, linewidth=0.5, alpha=0.5)

    plt.tight_layout(pad=0.8)
    return fig


# ── Section 2: VPIN Weight Chart ───────────────────────────────────────────────
def make_vpin_weight_chart():
    fig, ax = plt.subplots(figsize=(8, 3.8))

    x = np.linspace(0.0, 1.0, 500)
    y = [vpin_weight(v) for v in x]

    ax.fill_between(x, y, alpha=0.25, color=MPL_PURPLE)
    ax.plot(x, y, color=MPL_PURPLE, linewidth=2.5)

    ax.axvline(x=0.30, color=MPL_AMBER, linewidth=1.2, linestyle='--', alpha=0.7)
    ax.text(0.31, 0.15, 'Activates at 0.30', color=MPL_AMBER, fontsize=8)

    ax.axvline(x=0.50, color=MPL_RED, linewidth=1.5, linestyle='--', alpha=0.8)
    ax.text(0.51, 0.5, 'Trade gate\n(≥0.50)', color=MPL_RED, fontsize=8)

    ax.axvline(x=0.80, color=MPL_GREEN, linewidth=1, linestyle=':', alpha=0.5)
    ax.text(0.81, 2.2, 'Max weight\n(≥0.60)', color=MPL_GREEN, fontsize=8)

    # Morning VPIN range
    ax.axvspan(0.65, 0.93, alpha=0.12, color=MPL_GREEN, label='Morning win range (0.65–0.93)')
    ax.text(0.79, 3.05, 'Morning sweet spot', color=MPL_GREEN, fontsize=8, ha='center')

    ax.set_xlabel('VPIN Value', color=MPL_TEXT, fontsize=10)
    ax.set_ylabel('VPIN Weight', color=MPL_TEXT, fontsize=10)
    ax.set_title('VPIN Weight vs VPIN Value', color=MPL_TEXT, fontsize=12, fontweight='bold')
    ax.set_xlim(0, 1.0)
    ax.set_ylim(0, 3.4)
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8)

    plt.tight_layout(pad=0.8)
    return fig


# ── Section 2: CoinGlass Components Chart ────────────────────────────────────
def make_coinglass_charts():
    fig, axes = plt.subplots(2, 2, figsize=(10, 6))
    fig.suptitle('CoinGlass Components (Currently Inactive — All Zero)',
                 color=MPL_MUTED, fontsize=11, style='italic')

    # 1. Liquidation surge
    ax = axes[0, 0]
    liq_usd = np.linspace(0, 5_000_000, 500)
    liq_w = np.where(liq_usd > 500_000,
                     np.minimum(liq_usd / 2_000_000, 2.0),
                     0)
    ax.fill_between(liq_usd / 1e6, liq_w, alpha=0.2, color=MPL_RED)
    ax.plot(liq_usd / 1e6, liq_w, color=MPL_RED, linewidth=2)
    ax.axvline(x=0.5, color=MPL_AMBER, linewidth=1, linestyle='--', alpha=0.6)
    ax.text(0.55, 0.1, '$500K gate', color=MPL_AMBER, fontsize=7)
    ax.set_title('Liquidation Surge Weight', color=MPL_TEXT, fontsize=9)
    ax.set_xlabel('Total Liqs 1m ($M)', fontsize=8)
    ax.set_ylabel('|Weight| (directional)', fontsize=8)
    ax.set_ylim(0, 2.2)
    ax.grid(True, alpha=0.3)

    # 2. Long/Short ratio
    ax = axes[0, 1]
    long_pct = np.linspace(20, 80, 500)
    ls_w = np.where(long_pct > 65,
                    -np.minimum((long_pct - 50) / 30, 1.5),
                    np.where(long_pct < 35,
                             np.minimum((50 - long_pct) / 30, 1.5),
                             0))
    ax.fill_between(long_pct, ls_w, alpha=0.2,
                    color=np.where(ls_w > 0, MPL_GREEN, MPL_RED)[0])
    ax.plot(long_pct, ls_w, color=MPL_BLUE, linewidth=2)
    ax.axhline(0, color=MPL_MUTED, linewidth=0.8, linestyle=':')
    ax.axvline(35, color=MPL_GREEN, linewidth=0.8, linestyle='--', alpha=0.5)
    ax.axvline(65, color=MPL_RED, linewidth=0.8, linestyle='--', alpha=0.5)
    ax.text(22, 1.2, 'Contrarian UP', color=MPL_GREEN, fontsize=7)
    ax.text(65, -1.3, 'Contrarian DOWN', color=MPL_RED, fontsize=7)
    ax.set_title('Long/Short Imbalance Weight', color=MPL_TEXT, fontsize=9)
    ax.set_xlabel('Long % of Open Interest', fontsize=8)
    ax.set_ylabel('Weight (±)', fontsize=8)
    ax.set_ylim(-1.8, 1.8)
    ax.grid(True, alpha=0.3)

    # 3. Funding rate
    ax = axes[1, 0]
    funding = np.linspace(-0.003, 0.003, 500)
    fund_w = np.where(np.abs(funding) > 0.0003,
                      np.where(funding > 0,
                               -np.minimum(funding / 0.001, 1.0),
                               np.minimum(np.abs(funding) / 0.001, 1.0)),
                      0)
    ax.fill_between(funding * 100, fund_w, alpha=0.2, color=MPL_AMBER)
    ax.plot(funding * 100, fund_w, color=MPL_AMBER, linewidth=2)
    ax.axhline(0, color=MPL_MUTED, linewidth=0.8)
    ax.axvline(-0.03, color=MPL_AMBER, linewidth=0.8, linestyle='--', alpha=0.5)
    ax.axvline(0.03, color=MPL_AMBER, linewidth=0.8, linestyle='--', alpha=0.5)
    ax.set_title('Funding Rate Weight', color=MPL_TEXT, fontsize=9)
    ax.set_xlabel('Funding Rate (%)', fontsize=8)
    ax.set_ylabel('Weight (±)', fontsize=8)
    ax.set_ylim(-1.2, 1.2)
    ax.grid(True, alpha=0.3)

    # 4. OI delta
    ax = axes[1, 1]
    oi_vals = np.linspace(-0.03, 0.03, 500)
    # When delta_pct > 0 (UP): +1.0 if OI rising, -0.5 if falling
    oi_w_up = np.where(np.abs(oi_vals) > 0.005,
                       np.where(oi_vals > 0, 1.0, -0.5), 0)
    oi_w_dn = np.where(np.abs(oi_vals) > 0.005,
                       np.where(oi_vals > 0, -0.5, 0.5), 0)
    ax.plot(oi_vals * 100, oi_w_up, color=MPL_GREEN, linewidth=2, label='Dir=UP')
    ax.plot(oi_vals * 100, oi_w_dn, color=MPL_RED, linewidth=2, linestyle='--', label='Dir=DOWN')
    ax.axhline(0, color=MPL_MUTED, linewidth=0.8)
    ax.set_title('OI Delta Weight', color=MPL_TEXT, fontsize=9)
    ax.set_xlabel('OI Δ% (1m)', fontsize=8)
    ax.set_ylabel('Weight', fontsize=8)
    ax.legend(fontsize=7)
    ax.grid(True, alpha=0.3)

    plt.tight_layout(pad=1.0)
    return fig


# ── Section 3: Confidence & Tier ─────────────────────────────────────────────
def make_confidence_chart():
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10, 4))

    # Left: score → confidence mapping for different active_max
    score = np.linspace(0, 12, 500)
    for active_max, color, label in [
        (5.0, MPL_BLUE, 'active_max=5 (delta only)'),
        (8.0, MPL_PURPLE, 'active_max=8 (delta+VPIN)'),
        (12.0, MPL_GREEN, 'active_max=12 (all active)'),
    ]:
        conf = np.minimum(score / active_max, 0.95)
        ax1.plot(score, conf, color=color, linewidth=2, label=label)

    # Tier thresholds
    for threshold, name, color in [
        (0.85, 'DECISIVE (0.85)', MPL_RED),
        (0.65, 'HIGH (0.65)', MPL_AMBER),
        (0.50, 'MODERATE (0.50)', MPL_GREEN),
    ]:
        ax1.axhline(y=threshold, color=color, linewidth=1.2, linestyle='--', alpha=0.8)
        ax1.text(0.3, threshold + 0.01, name, color=color, fontsize=7.5)

    ax1.set_xlabel('Raw Signal Score', color=MPL_TEXT, fontsize=10)
    ax1.set_ylabel('Confidence', color=MPL_TEXT, fontsize=10)
    ax1.set_title('Score → Confidence Mapping', color=MPL_TEXT, fontsize=11, fontweight='bold')
    ax1.set_xlim(0, 12)
    ax1.set_ylim(0, 1.0)
    ax1.legend(fontsize=7.5)
    ax1.grid(True, alpha=0.3)

    # Right: tier windows (time-based)
    tier_data = [
        ('DECISIVE', 300, 180, 0.85, MPL_RED),
        ('HIGH', 180, 60, 0.65, MPL_AMBER),
        ('MODERATE', 60, 10, 0.50, MPL_GREEN),
        ('NO FIRE', 10, 0, 0.0, MPL_MUTED),
    ]
    y_pos = range(len(tier_data))
    for i, (name, t_start, t_end, conf, color) in enumerate(tier_data):
        width = t_start - t_end
        ax2.barh(i, width, left=t_end, color=color, alpha=0.4, height=0.6)
        ax2.barh(i, width, left=t_end, color='none', edgecolor=color, height=0.6, linewidth=1.2)
        if name != 'NO FIRE':
            ax2.text(t_end + width/2, i, f'{name}\nconf≥{conf:.0%}',
                     ha='center', va='center', color=color, fontsize=8, fontweight='bold')
        else:
            ax2.text(t_end + width/2, i, 'No fire\n(T<10s)',
                     ha='center', va='center', color=MPL_MUTED, fontsize=8)

    ax2.set_xlabel('Seconds Remaining in Window', color=MPL_TEXT, fontsize=10)
    ax2.set_yticks([])
    ax2.set_title('Tier Entry Windows (Time Remaining)', color=MPL_TEXT, fontsize=11, fontweight='bold')
    ax2.set_xlim(0, 310)
    ax2.invert_xaxis()
    ax2.grid(True, alpha=0.3, axis='x')

    plt.tight_layout(pad=0.8)
    return fig


# ── Section 4: Calibration Scatter ───────────────────────────────────────────
def make_calibration_chart(trades):
    fig, ax = plt.subplots(figsize=(10, 6))

    if not trades:
        # Synthetic fallback data
        np.random.seed(42)
        n_wins = 18
        n_losses = 7
        wins = [{
            'delta': np.random.uniform(0.03, 0.09),
            'vpin': np.random.uniform(0.65, 0.93),
            'pnl': np.random.uniform(3, 18),
            'outcome': 'win'
        } for _ in range(n_wins)]
        losses = [{
            'delta': np.random.uniform(0.02, 0.12),
            'vpin': np.random.uniform(0.50, 0.75),
            'pnl': np.random.uniform(-8, -1),
            'outcome': 'loss'
        } for _ in range(n_losses)]
        trades = wins + losses
        data_source = 'Synthetic (DB unavailable)'
    else:
        data_source = f'Live DB — {len(trades)} trades'

    wins = [t for t in trades if t['outcome'] in ('win', 'WIN', 'correct', 'yes')]
    losses = [t for t in trades if t['outcome'] in ('loss', 'LOSS', 'incorrect', 'no')]

    # Plot losses first (behind)
    if losses:
        loss_delta = [t['delta'] * 100 for t in losses]
        loss_vpin  = [t['vpin'] for t in losses]
        loss_size  = [max(20, min(200, abs(t['pnl']) * 15)) for t in losses]
        ax.scatter(loss_delta, loss_vpin, c=MPL_RED, s=loss_size, alpha=0.7,
                   marker='v', label=f'LOSS ({len(losses)})', zorder=3,
                   edgecolors='#F8514980', linewidths=0.8)

    if wins:
        win_delta  = [t['delta'] * 100 for t in wins]
        win_vpin   = [t['vpin'] for t in wins]
        win_size   = [max(40, min(300, abs(t['pnl']) * 20)) for t in wins]
        ax.scatter(win_delta, win_vpin, c=MPL_GREEN, s=win_size, alpha=0.8,
                   marker='^', label=f'WIN ({len(wins)})', zorder=4,
                   edgecolors='#3FB95080', linewidths=0.8)

    # Confidence contour overlay
    delta_vals = np.linspace(0, 0.15, 200)
    vpin_vals  = np.linspace(0.30, 1.0, 200)
    D, V = np.meshgrid(delta_vals, vpin_vals)

    def compute_conf(d, v):
        dw = delta_weight(d)
        vw = vpin_weight(v) if v > 0.50 else 0.0
        score = dw + vw
        active_max = 3.0
        if vw != 0:
            active_max += 3.0
        active_max = max(active_max, 5.0)
        return min(abs(score) / active_max, 0.95)

    CONF = np.vectorize(compute_conf)(D, V)
    contours = ax.contour(D * 100, V, CONF, levels=[0.50, 0.65, 0.75, 0.85],
                          colors=[MPL_GREEN, MPL_AMBER, '#FF8C00', MPL_RED],
                          linewidths=[1.0, 1.2, 1.4, 1.6], alpha=0.6)
    ax.clabel(contours, fmt={0.50: '50%', 0.65: '65%', 0.75: '75%', 0.85: '85%'},
              fontsize=8, colors=MPL_TEXT)

    # Sweet spot box
    rect = plt.Rectangle((0.03*100, 0.65), (0.09-0.03)*100, (0.93-0.65),
                          linewidth=2, edgecolor=MPL_GREEN,
                          facecolor='none', linestyle='--', alpha=0.8, zorder=5)
    ax.add_patch(rect)
    ax.text(0.06*100, 0.945, '⭐ Morning Sweet Spot', color=MPL_GREEN,
            fontsize=9, fontweight='bold', ha='center')

    # Gate lines
    ax.axvline(x=0.02*100, color=MPL_AMBER, linewidth=1.2, linestyle=':', alpha=0.7)
    ax.text(0.021*100, 0.32, '|Δ|≥0.02% gate', color=MPL_AMBER, fontsize=7.5, rotation=90, va='bottom')
    ax.axhline(y=0.50, color=MPL_RED, linewidth=1.2, linestyle=':', alpha=0.7)
    ax.text(0.001, 0.51, 'VPIN≥0.50 gate', color=MPL_RED, fontsize=7.5)

    ax.set_xlabel('|Price Delta| (%)', color=MPL_TEXT, fontsize=11)
    ax.set_ylabel('VPIN', color=MPL_TEXT, fontsize=11)
    ax.set_title(f'Signal Calibration: Delta vs VPIN — {data_source}', color=MPL_TEXT, fontsize=12, fontweight='bold')
    ax.set_xlim(0, 0.15*100)
    ax.set_ylim(0.30, 1.0)
    ax.legend(fontsize=9, loc='upper right')
    ax.grid(True, alpha=0.25)

    plt.tight_layout(pad=0.8)
    return fig


# ── Section 5: Stake Sizing ───────────────────────────────────────────────────
def make_stake_chart():
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10, 4.5))

    # Left: multiplier vs token price
    prices = np.linspace(0.20, 0.75, 500)
    tp_clamped = np.clip(prices, 0.30, 0.65)
    multiplier_raw = (1.0 - tp_clamped) / 0.50
    multiplier = np.clip(multiplier_raw, 0.5, 1.5)

    ax1.fill_between(prices, multiplier, 1.0, where=multiplier > 1.0,
                     alpha=0.2, color=MPL_GREEN, label='Bigger bet (cheap token)')
    ax1.fill_between(prices, multiplier, 1.0, where=multiplier < 1.0,
                     alpha=0.2, color=MPL_RED, label='Smaller bet (expensive token)')
    ax1.plot(prices, multiplier, color=MPL_BLUE, linewidth=2.5)
    ax1.axhline(1.0, color=MPL_MUTED, linewidth=0.8, linestyle=':')
    ax1.axvline(0.50, color=MPL_AMBER, linewidth=1, linestyle='--', alpha=0.7)
    ax1.text(0.51, 0.52, '50¢ = 1.0x', color=MPL_AMBER, fontsize=8)

    # Valid range shading
    ax1.axvspan(0.30, 0.65, alpha=0.08, color=MPL_BLUE)
    ax1.text(0.475, 1.45, 'Valid range\n(30–65¢)', color=MPL_BLUE, fontsize=8, ha='center')

    ax1.set_xlabel('Token Price (¢ / $)', color=MPL_TEXT, fontsize=10)
    ax1.set_ylabel('Stake Multiplier', color=MPL_TEXT, fontsize=10)
    ax1.set_title('Price Multiplier vs Token Price', color=MPL_TEXT, fontsize=11, fontweight='bold')
    ax1.set_ylim(0.3, 1.6)
    ax1.legend(fontsize=8)
    ax1.grid(True, alpha=0.3)

    # Right: potential win/loss at different prices (with $16 base)
    base = 16.0
    tp_range = np.linspace(0.30, 0.65, 200)
    mult = np.clip((1.0 - tp_range) / 0.50, 0.5, 1.5)
    stake = base * mult
    win_amt = stake * (1.0 / tp_range - 1.0)   # profit if win (payout - stake)
    loss_amt = -stake  # full loss

    ax2.plot(tp_range, win_amt, color=MPL_GREEN, linewidth=2.5, label='Profit if WIN')
    ax2.plot(tp_range, loss_amt, color=MPL_RED, linewidth=2.5, label='Loss if LOSS')
    ax2.fill_between(tp_range, win_amt, 0, alpha=0.15, color=MPL_GREEN)
    ax2.fill_between(tp_range, loss_amt, 0, alpha=0.15, color=MPL_RED)

    # Net EV line at 73% win rate
    ev = 0.73 * win_amt + 0.27 * loss_amt
    ax2.plot(tp_range, ev, color=MPL_AMBER, linewidth=1.5, linestyle='--', label='EV @ 73% WR')

    ax2.axhline(0, color=MPL_MUTED, linewidth=0.8, linestyle=':')
    ax2.set_xlabel('Token Price ($)', color=MPL_TEXT, fontsize=10)
    ax2.set_ylabel('P&L ($)', color=MPL_TEXT, fontsize=10)
    ax2.set_title('P&L at $16 Base Stake', color=MPL_TEXT, fontsize=11, fontweight='bold')
    ax2.legend(fontsize=8)
    ax2.grid(True, alpha=0.3)

    # Annotations
    ax2.text(0.40, win_amt[int(len(win_amt)*0.3)] + 1, f'Max win: ${win_amt[0]:.1f}',
             color=MPL_GREEN, fontsize=8)

    plt.tight_layout(pad=0.8)
    return fig


# ── Section 6: Risk Flow Diagram ──────────────────────────────────────────────
def make_risk_flow_diagram():
    fig, ax = plt.subplots(figsize=(10, 6))
    ax.set_facecolor(MPL_BG)
    fig.patch.set_facecolor(MPL_BG)
    ax.set_xlim(0, 10)
    ax.set_ylim(-0.5, 8.5)
    ax.axis('off')

    gates = [
        (5, 8.0, '🔍 Signal Detected', MPL_BLUE,     'Score > 0, VPIN active'),
        (5, 6.8, '1. VPIN ≥ 0.50',    MPL_PURPLE,   'No informed flow → skip'),
        (5, 5.6, '2. |Δ| ≥ 0.02%',   MPL_AMBER,    'Below min edge → skip'),
        (5, 4.4, '3. Confidence ≥ tier', MPL_BLUE,  'Check tier (DECISIVE/HIGH/MODERATE)'),
        (5, 3.2, '4. Risk Manager',   MPL_RED,      'Exposure, drawdown, kill switch'),
        (5, 2.0, '5. Token Price',    MPL_GREEN,    '30–65¢ (5m) or 30–70¢ (15m)'),
        (5, 0.8, '✅ Place Order',    MPL_GREEN,    'CLOB order → verify fill'),
    ]

    for i, (x, y, label, color, detail) in enumerate(gates):
        # Box
        rect_w = 5.0
        rect_h = 0.7
        rx = x - rect_w / 2
        ry = y - rect_h / 2
        fancy = mpatches.FancyBboxPatch(
            (rx, ry), rect_w, rect_h,
            boxstyle="round,pad=0.05",
            facecolor=color + '30',
            edgecolor=color,
            linewidth=1.5
        )
        ax.add_patch(fancy)
        ax.text(x, y + 0.05, label, ha='center', va='center',
                color=color, fontsize=9.5, fontweight='bold')

        # Detail text on right
        ax.text(8.0, y, detail, ha='left', va='center',
                color=MPL_MUTED, fontsize=8, style='italic')

        # Arrow down (except last)
        if i < len(gates) - 1:
            ax.annotate('', xy=(x, gates[i+1][1] + 0.38),
                        xytext=(x, y - 0.38),
                        arrowprops=dict(arrowstyle='->', color=MPL_MUTED,
                                        lw=1.2, mutation_scale=12))

        # Reject arrows (LEFT side, except first and last)
        if 0 < i < len(gates) - 1:
            ax.annotate('', xy=(1.5, y),
                        xytext=(rx, y),
                        arrowprops=dict(arrowstyle='->', color=MPL_RED,
                                        lw=0.8, mutation_scale=8))
            ax.text(1.4, y, 'REJECT', ha='right', va='center',
                    color=MPL_RED, fontsize=7.5)

    ax.set_title('Risk Gate Flow — Every Order Must Pass All Gates',
                 color=MPL_TEXT, fontsize=12, fontweight='bold', pad=10)

    plt.tight_layout(pad=0.5)
    return fig


# ── Section: VPIN Bucket Diagram ──────────────────────────────────────────────
def make_vpin_bucket_diagram():
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10, 4))

    # Left: single bucket filling
    ax1.set_xlim(0, 10)
    ax1.set_ylim(-0.5, 4)
    ax1.axis('off')
    ax1.set_title('VPIN Bucket Mechanics', color=MPL_TEXT, fontsize=11, fontweight='bold')

    bucket_w = 7
    bucket_h = 3
    bx = 1.5
    by = 0.2

    # Empty bucket outline
    rect = mpatches.FancyBboxPatch((bx, by), bucket_w, bucket_h,
                                    boxstyle="round,pad=0.1",
                                    facecolor=MPL_CARD, edgecolor=MPL_BORDER, linewidth=2)
    ax1.add_patch(rect)

    # Fill: buy (green) and sell (red)
    buy_frac = 0.72
    sell_frac = 0.28
    buy_w = bucket_w * buy_frac
    sell_w = bucket_w * sell_frac

    buy_rect = mpatches.FancyBboxPatch((bx, by), buy_w, bucket_h * 0.9,
                                        boxstyle="round,pad=0.05",
                                        facecolor=MPL_GREEN + '60', edgecolor='none')
    ax1.add_patch(buy_rect)
    sell_rect = mpatches.FancyBboxPatch((bx + buy_w, by), sell_w, bucket_h * 0.9,
                                         boxstyle="round,pad=0.05",
                                         facecolor=MPL_RED + '60', edgecolor='none')
    ax1.add_patch(sell_rect)

    ax1.text(bx + buy_w/2, by + bucket_h*0.45, f'BUY\n72%', ha='center', va='center',
             color=MPL_GREEN, fontsize=12, fontweight='bold')
    ax1.text(bx + buy_w + sell_w/2, by + bucket_h*0.45, f'SELL\n28%', ha='center', va='center',
             color=MPL_RED, fontsize=11, fontweight='bold')

    ax1.text(5, 3.6, 'Imbalance = |72% − 28%| = 0.44', ha='center',
             color=MPL_AMBER, fontsize=9, style='italic')

    ax1.annotate('Bucket full\n→ compute imbalance', xy=(8.5, 0.8), xytext=(8.8, 2.5),
                color=MPL_BLUE, fontsize=8,
                arrowprops=dict(arrowstyle='->', color=MPL_BLUE, lw=1))

    # Right: rolling VPIN
    ax2.set_title('Rolling VPIN (Mean of Last N Buckets)', color=MPL_TEXT, fontsize=11, fontweight='bold')

    # Simulate bucket imbalances
    np.random.seed(7)
    n = 50
    # Calm period → spike
    calm = np.random.uniform(0.1, 0.3, 35)
    spike = np.random.uniform(0.55, 0.90, 15)
    imbalances = np.concatenate([calm, spike])
    rolling_vpin = np.array([imbalances[max(0,i-20):i+1].mean() for i in range(n)])

    ax2.fill_between(range(n), rolling_vpin, alpha=0.3, color=MPL_PURPLE)
    ax2.plot(range(n), rolling_vpin, color=MPL_PURPLE, linewidth=2)

    ax2.axhline(0.50, color=MPL_RED, linewidth=1.2, linestyle='--', alpha=0.8)
    ax2.text(1, 0.515, 'Trade gate (0.50)', color=MPL_RED, fontsize=8)
    ax2.axhline(0.70, color=MPL_AMBER, linewidth=1, linestyle=':', alpha=0.7)
    ax2.text(1, 0.715, 'Cascade threshold (0.70)', color=MPL_AMBER, fontsize=8)

    ax2.axvspan(35, n-1, alpha=0.1, color=MPL_PURPLE)
    ax2.text(42, 0.85, 'Informed\ntrading detected', color=MPL_PURPLE, fontsize=8, ha='center')

    ax2.set_xlabel('Bucket Number', fontsize=9)
    ax2.set_ylabel('VPIN', fontsize=9)
    ax2.set_ylim(0, 1.0)
    ax2.grid(True, alpha=0.3)

    plt.tight_layout(pad=0.8)
    return fig


# ── Build the PDF ──────────────────────────────────────────────────────────────
def build_pdf(trades):
    S = make_styles()
    doc = SimpleDocTemplate(
        OUTPUT_PATH,
        pagesize=A4,
        leftMargin=20*mm,
        rightMargin=20*mm,
        topMargin=22*mm,
        bottomMargin=22*mm,
        title='Novakash Signal Mathematics v3.1',
        author='Novakash Trading System',
        subject='Signal Mathematics & Strategy Documentation',
    )

    story = []

    def hr(thickness=0.5, color=BORDER_COLOR, space=4):
        story.append(HRFlowable(width='100%', thickness=thickness,
                                color=color, spaceAfter=space, spaceBefore=space))

    def section_header(num, title):
        story.append(Spacer(1, 6*mm))
        story.append(Paragraph(f'<font color="#BC8CFF">§{num}</font>', S['section_num']))
        story.append(Paragraph(title, S['h1']))
        hr(1.5, ACCENT_BLUE, 6)

    # ══════════════════════════════════════════════════════════════════════════
    # COVER PAGE
    # ══════════════════════════════════════════════════════════════════════════
    story.append(Spacer(1, 18*mm))

    # Cover hero figure
    cover_fig = make_cover_figure()
    story.append(fig_to_image(cover_fig, width_mm=170, height_mm=55))
    story.append(Spacer(1, 8*mm))

    story.append(Paragraph('NOVAKASH', S['cover_subtitle']))
    story.append(Paragraph('Signal Mathematics', S['cover_title']))
    story.append(Paragraph('Quantitative Edge in Polymarket Binary Windows', S['cover_subtitle']))
    story.append(Spacer(1, 4*mm))
    hr(2, ACCENT_BLUE, 4)
    story.append(Spacer(1, 4*mm))
    story.append(Paragraph('Version 3.1 — Live System Documentation', S['cover_version']))
    story.append(Paragraph('Strategy: Five-Minute VPIN | Market: BTC/USD Perpetuals', S['cover_version']))
    story.append(Spacer(1, 12*mm))

    # Stats row
    stats_data = [
        [
            Paragraph('<b>73%</b>', ParagraphStyle('stat_n', fontName='Helvetica-Bold', fontSize=24, textColor=ACCENT_GREEN, alignment=TA_CENTER)),
            Paragraph('<b>6</b>', ParagraphStyle('stat_n', fontName='Helvetica-Bold', fontSize=24, textColor=ACCENT_BLUE, alignment=TA_CENTER)),
            Paragraph('<b>2</b>', ParagraphStyle('stat_n', fontName='Helvetica-Bold', fontSize=24, textColor=ACCENT_PURPLE, alignment=TA_CENTER)),
        ],
        [
            Paragraph('Morning Win Rate', S['label']),
            Paragraph('Signal Components', S['label']),
            Paragraph('Core Signals', S['label']),
        ],
    ]
    stats_table = Table(stats_data, colWidths=[55*mm, 55*mm, 55*mm])
    stats_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), BG_CARD),
        ('ROWBACKGROUNDS', (0,0), (-1,-1), [BG_CARD, BG_CARD]),
        ('BOX', (0,0), (-1,-1), 1, BORDER_COLOR),
        ('INNERGRID', (0,0), (-1,-1), 0.5, BORDER_COLOR),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('TOPPADDING', (0,0), (-1,-1), 8),
        ('BOTTOMPADDING', (0,0), (-1,-1), 8),
        ('ROUNDEDCORNERS', [4]),
    ]))
    story.append(stats_table)
    story.append(Spacer(1, 8*mm))
    story.append(Paragraph(
        '<font color="#8B949E">This document describes the mathematical foundation of the Novakash '
        'trading system — how VPIN, price delta, and market microstructure signals are combined '
        'to produce actionable edge in Polymarket\'s 5-minute binary windows.</font>',
        S['body_muted']
    ))

    story.append(PageBreak())

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION 1: THE VISION
    # ══════════════════════════════════════════════════════════════════════════
    section_header(1, 'The Vision')

    story.append(Paragraph('The Informed Flow Thesis', S['h2']))
    story.append(Paragraph(
        'Traditional market participants react to price changes. Informed traders — those with '
        'non-public information or superior models — move <i>ahead</i> of the crowd. Their activity '
        'creates a detectable signature in the order flow: they consistently trade on the same side, '
        'creating an imbalance between buy and sell volume.',
        S['body']
    ))
    story.append(Paragraph(
        '<b>VPIN (Volume-Synchronized Probability of Informed Trading)</b> captures this imbalance. '
        'When smart money is moving, VPIN spikes. By the time the broader market reprices, the '
        'informed traders have already taken their position. That gap — the lag between smart money '
        'and market repricing — is our edge.',
        S['body']
    ))

    # Thesis box
    thesis_data = [[
        Paragraph(
            '💡 <b>Core Thesis:</b> VPIN detects informed trader flow 60+ seconds before the market '
            'reprices. Combined with Polymarket\'s binary 5-minute windows, we can bet the direction '
            'of repricing at 50¢ odds — before the market knows which way it\'s going.',
            ParagraphStyle('thesis', fontName='Helvetica', fontSize=10, textColor=TEXT_PRIMARY,
                           leading=16, leftIndent=4, rightIndent=4)
        )
    ]]
    thesis_table = Table(thesis_data, colWidths=[170*mm])
    thesis_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), BG_CARD2),
        ('BOX', (0,0), (-1,-1), 1.5, ACCENT_BLUE),
        ('TOPPADDING', (0,0), (-1,-1), 10),
        ('BOTTOMPADDING', (0,0), (-1,-1), 10),
        ('LEFTPADDING', (0,0), (-1,-1), 12),
        ('RIGHTPADDING', (0,0), (-1,-1), 12),
    ]))
    story.append(thesis_table)
    story.append(Spacer(1, 6*mm))

    story.append(Paragraph('The Cascade Theory', S['h2']))
    story.append(Paragraph(
        'Large liquidation events in perpetual futures create <b>predictable price cascades</b>. '
        'When leveraged positions are force-closed, they create market orders that push price '
        'further, triggering more liquidations in a self-reinforcing loop.',
        S['body']
    ))
    story.append(Paragraph(
        'The "teetering ball" model: once a cascade starts rolling, momentum carries it until '
        'positions are exhausted. The CascadeDetector FSM identifies three phases:',
        S['body']
    ))

    cascade_steps = [
        ['Phase', 'Signal Conditions', 'What it Means'],
        ['CASCADE_DETECTED', 'VPIN ≥ 0.70 + |OI Δ| ≥ 2% + Liqs ≥ $5M', 'Cascade is starting — big positions being closed'],
        ['EXHAUSTING', 'Liq volume declining (<85% of prev) OR VPIN falling', 'Cascade losing energy — the ball is slowing'],
        ['BET_SIGNAL', 'VPIN < 0.55 AND Liqs < $2.5M', 'Exhaustion complete — mean reversion bet'],
        ['COOLDOWN', '900 seconds', 'Wait for next cycle'],
    ]
    cascade_table = Table(cascade_steps, colWidths=[45*mm, 80*mm, 45*mm])
    cascade_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), BG_CARD2),
        ('BACKGROUND', (0,1), (-1,-1), BG_CARD),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [BG_CARD, BG_CARD2]),
        ('BOX', (0,0), (-1,-1), 0.5, BORDER_COLOR),
        ('INNERGRID', (0,0), (-1,-1), 0.5, BORDER_COLOR),
        ('TEXTCOLOR', (0,0), (-1,0), TEXT_PRIMARY),
        ('TEXTCOLOR', (0,1), (-1,-1), TEXT_PRIMARY),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,-1), 8.5),
        ('TOPPADDING', (0,0), (-1,-1), 6),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
        ('LEFTPADDING', (0,0), (-1,-1), 8),
        ('ALIGN', (0,0), (-1,0), 'CENTER'),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('FONTNAME', (0,1), (0,-1), 'Courier-Bold'),
        ('TEXTCOLOR', (0,1), (0,1), ACCENT_RED),
        ('TEXTCOLOR', (0,2), (0,2), ACCENT_AMBER),
        ('TEXTCOLOR', (0,3), (0,3), ACCENT_GREEN),
        ('TEXTCOLOR', (0,4), (0,4), TEXT_MUTED),
    ]))
    story.append(cascade_table)
    story.append(Spacer(1, 5*mm))

    story.append(Paragraph(
        'The Polymarket Edge: Binary markets resolve at $1.00 or $0.00. '
        'When VPIN and delta signal a direction, the correct-direction token should be '
        'trading near 50¢ — before the market has priced in the move. We capture this '
        'mispricing by entering before consensus forms.',
        S['body']
    ))

    story.append(PageBreak())

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION 2: THE COMPOSITE SIGNAL
    # ══════════════════════════════════════════════════════════════════════════
    section_header(2, 'The Composite Signal Equation')

    story.append(Paragraph('Master Equation', S['h2']))
    story.append(Paragraph(
        'Score = Δ<sub>weight</sub> + VPIN<sub>weight</sub> + Liq<sub>weight</sub> '
        '+ L/S<sub>weight</sub> + Fund<sub>weight</sub> + OI<sub>weight</sub>',
        S['equation']
    ))
    story.append(Paragraph(
        'The score is signed: positive = UP, negative = DOWN. Each component contributes '
        'an independent weight based on its current value. Components not connected '
        '(CoinGlass) contribute zero and are excluded from the normalisation denominator.',
        S['body_muted']
    ))

    # Component summary table
    comp_data = [
        [Paragraph('Component', S['table_header']),
         Paragraph('Source', S['table_header']),
         Paragraph('Max Weight', S['table_header']),
         Paragraph('Status', S['table_header']),
         Paragraph('Direction', S['table_header'])],
        [Paragraph('Δ Price (delta)', S['table_cell_left']),
         Paragraph('Binance aggTrade', S['table_cell']),
         Paragraph('3.0', S['table_cell']),
         Paragraph('✅ Active', ParagraphStyle('green_cell', fontName='Helvetica-Bold', fontSize=9, textColor=ACCENT_GREEN, alignment=TA_CENTER, leading=13)),
         Paragraph('From price direction', S['table_cell'])],
        [Paragraph('VPIN (informed flow)', S['table_cell_left']),
         Paragraph('VPINCalculator', S['table_cell']),
         Paragraph('3.0', S['table_cell']),
         Paragraph('✅ Active', ParagraphStyle('green_cell', fontName='Helvetica-Bold', fontSize=9, textColor=ACCENT_GREEN, alignment=TA_CENTER, leading=13)),
         Paragraph('Amplifies delta dir.', S['table_cell'])],
        [Paragraph('Liquidation surge', S['table_cell_left']),
         Paragraph('CoinGlass 1m', S['table_cell']),
         Paragraph('±2.0', S['table_cell']),
         Paragraph('⚠️ Inactive', ParagraphStyle('amber_cell', fontName='Helvetica-Bold', fontSize=9, textColor=ACCENT_AMBER, alignment=TA_CENTER, leading=13)),
         Paragraph('Longs liq → DOWN, Shorts liq → UP', S['table_cell'])],
        [Paragraph('Long/Short ratio', S['table_cell_left']),
         Paragraph('CoinGlass 1m', S['table_cell']),
         Paragraph('±1.5', S['table_cell']),
         Paragraph('⚠️ Inactive', ParagraphStyle('amber_cell', fontName='Helvetica-Bold', fontSize=9, textColor=ACCENT_AMBER, alignment=TA_CENTER, leading=13)),
         Paragraph('Contrarian signal', S['table_cell'])],
        [Paragraph('Funding rate bias', S['table_cell_left']),
         Paragraph('CoinGlass', S['table_cell']),
         Paragraph('±1.0', S['table_cell']),
         Paragraph('⚠️ Inactive', ParagraphStyle('amber_cell', fontName='Helvetica-Bold', fontSize=9, textColor=ACCENT_AMBER, alignment=TA_CENTER, leading=13)),
         Paragraph('Positive fund → DOWN pressure', S['table_cell'])],
        [Paragraph('OI delta', S['table_cell_left']),
         Paragraph('CoinGlass 1m', S['table_cell']),
         Paragraph('±1.0', S['table_cell']),
         Paragraph('⚠️ Inactive', ParagraphStyle('amber_cell', fontName='Helvetica-Bold', fontSize=9, textColor=ACCENT_AMBER, alignment=TA_CENTER, leading=13)),
         Paragraph('Rising OI confirms direction', S['table_cell'])],
    ]
    comp_table = Table(comp_data, colWidths=[38*mm, 32*mm, 22*mm, 24*mm, 54*mm])
    comp_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), BG_CARD2),
        ('BACKGROUND', (0,1), (-1,2), BG_CARD),
        ('BACKGROUND', (0,3), (-1,-1), BG_CARD2),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [BG_CARD, BG_CARD2]),
        ('BOX', (0,0), (-1,-1), 0.5, BORDER_COLOR),
        ('INNERGRID', (0,0), (-1,-1), 0.5, BORDER_COLOR),
        ('TOPPADDING', (0,0), (-1,-1), 6),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
        ('LEFTPADDING', (0,0), (-1,-1), 6),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
    ]))
    story.append(comp_table)
    story.append(Spacer(1, 5*mm))

    # ── Component 1: Delta ────────────────────────────────────────────────────
    story.append(Paragraph('Component 1: Delta Weight — The Direction Signal', S['h2']))
    story.append(Paragraph(
        'The window delta measures how far BTC has moved from the window open. '
        'It is the primary signal — without sufficient delta, there is no directional conviction.',
        S['body']
    ))
    story.append(Paragraph('<b>Formula (from window_evaluator.py):</b>', S['h3']))

    code_lines = [
        'abs_delta = |current_price − open_price| / open_price × 100',
        '',
        'if abs_delta > 0.15%:   delta_weight = 3.0            # Max — big move',
        'elif abs_delta > 0.10%: delta_weight = 2.0 + (Δ − 0.10) / 0.05 × 1.0',
        'elif abs_delta > 0.05%: delta_weight = 1.5 + (Δ − 0.05) / 0.05 × 0.5',
        'elif abs_delta > 0.02%: delta_weight = 0.5 + (Δ − 0.02) / 0.03 × 1.0',
        'elif abs_delta > 0.005%: delta_weight = Δ / 0.02 × 0.5',
        'else:                   delta_weight = 0.0             # No signal',
    ]
    for line in code_lines:
        story.append(Paragraph(line, S['code']))

    delta_fig = make_delta_weight_chart()
    story.append(fig_to_image(delta_fig, width_mm=165, height_mm=68))
    story.append(Paragraph(
        'Figure 1: Delta weight curve. Morning wins clustered in the green zone (0.03–0.09%). '
        'The 0.02% gate is the minimum edge threshold — below this, no trade fires.',
        S['caption']
    ))

    # ── Component 2: VPIN ─────────────────────────────────────────────────────
    story.append(Paragraph('Component 2: VPIN Weight — The Informed Flow Detector', S['h2']))
    story.append(Paragraph(
        '<b>VPIN is the core signal.</b> It measures the probability that the current order '
        'flow is driven by informed traders rather than noise. A VPIN of 0.70 means 70% '
        'of recent volume is "one-sided" — someone knows something.',
        S['body']
    ))

    # VPIN formula box
    vpin_formula_data = [[
        Paragraph(
            '<b>VPIN Calculation:</b><br/><br/>'
            '1. Accumulate trades into fixed-USD buckets (each bucket = N USD notional)<br/>'
            '2. For each trade: classify as BUY (is_buyer_maker=False) or SELL (is_buyer_maker=True)<br/>'
            '3. When bucket fills: imbalance = |buy_vol − sell_vol| / total_vol<br/>'
            '4. VPIN = mean(imbalance) over last L buckets<br/><br/>'
            '<font color="#58A6FF">VPIN ∈ [0, 1]  |  0 = perfectly balanced  |  1 = fully one-sided</font>',
            ParagraphStyle('vpin_box', fontName='Helvetica', fontSize=9, textColor=TEXT_PRIMARY,
                           leading=15, leftIndent=4)
        )
    ]]
    vpin_formula_table = Table(vpin_formula_data, colWidths=[170*mm])
    vpin_formula_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), BG_CARD2),
        ('BOX', (0,0), (-1,-1), 1.5, ACCENT_PURPLE),
        ('TOPPADDING', (0,0), (-1,-1), 10),
        ('BOTTOMPADDING', (0,0), (-1,-1), 10),
        ('LEFTPADDING', (0,0), (-1,-1), 12),
    ]))
    story.append(vpin_formula_table)
    story.append(Spacer(1, 4*mm))

    story.append(Paragraph('<b>Weight Formula:</b>', S['h3']))
    story.append(Paragraph('vpin_weight = min((VPIN − 0.30) × 10, 3.0)   [only when VPIN > 0.50]', S['code']))
    story.append(Paragraph(
        'VPIN only amplifies the delta signal — it has no direction by itself. '
        'High VPIN with positive delta means smart money is buying. '
        'High VPIN with negative delta means smart money is selling.',
        S['body']
    ))

    vpin_bucket_fig = make_vpin_bucket_diagram()
    story.append(fig_to_image(vpin_bucket_fig, width_mm=165, height_mm=68))
    story.append(Paragraph(
        'Figure 2: Left — single bucket showing 72% buy / 28% sell → imbalance 0.44. '
        'Right — rolling VPIN over 50 buckets. Note spike when informed trading begins.',
        S['caption']
    ))

    vpin_w_fig = make_vpin_weight_chart()
    story.append(fig_to_image(vpin_w_fig, width_mm=165, height_mm=68))
    story.append(Paragraph(
        'Figure 3: VPIN weight curve. The morning win range (0.65–0.93) sits in the '
        'high-weight zone, contributing 3.0−3.0 weight to the composite score.',
        S['caption']
    ))

    story.append(PageBreak())

    # ── Components 3-6: CoinGlass ─────────────────────────────────────────────
    story.append(Paragraph('Components 3–6: CoinGlass Market Microstructure', S['h2']))
    story.append(Paragraph(
        'These four components use CoinGlass data to incorporate broader market structure signals. '
        '<b>They are currently inactive</b> (returning zero) but are architecturally integrated '
        'and will activate when the CoinGlass feed is connected.',
        S['body']
    ))

    inactive_note = [[
        Paragraph(
            '⚠️  <b>Status:</b> CoinGlass feed not connected. All four components return 0.0. '
            'The dynamic normalisation (<code>active_max</code>) accounts for this — inactive '
            'components are excluded from the denominator, preventing confidence inflation.',
            ParagraphStyle('inactive', fontName='Helvetica', fontSize=9.5, textColor=TEXT_PRIMARY,
                           leading=15, leftIndent=4)
        )
    ]]
    inactive_table = Table(inactive_note, colWidths=[170*mm])
    inactive_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), BG_CARD2),
        ('BOX', (0,0), (-1,-1), 1.5, ACCENT_AMBER),
        ('TOPPADDING', (0,0), (-1,-1), 8),
        ('BOTTOMPADDING', (0,0), (-1,-1), 8),
        ('LEFTPADDING', (0,0), (-1,-1), 10),
    ]))
    story.append(inactive_table)
    story.append(Spacer(1, 4*mm))

    cg_fig = make_coinglass_charts()
    story.append(fig_to_image(cg_fig, width_mm=170, height_mm=95))
    story.append(Paragraph(
        'Figure 4: CoinGlass weight curves. Top-left: liquidation surge (directional from long/short ratio). '
        'Top-right: long/short imbalance (contrarian). Bottom-left: funding rate bias. '
        'Bottom-right: OI delta (confirms conviction).',
        S['caption']
    ))

    story.append(PageBreak())

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION 3: CONFIDENCE & TIER SYSTEM
    # ══════════════════════════════════════════════════════════════════════════
    section_header(3, 'Confidence & Tier System')

    story.append(Paragraph('Normalising the Raw Score', S['h2']))
    story.append(Paragraph(
        'Raw score is dimensionless and unbounded. We normalise it into a confidence '
        'value [0, 0.95] using only the <i>active</i> components:',
        S['body']
    ))
    story.append(Paragraph(
        'confidence = min( |score| / active_max, 0.95 )',
        S['equation']
    ))
    story.append(Paragraph(
        'where <b>active_max</b> = sum of max weights for components that contributed non-zero values. '
        'With only delta and VPIN active (current state): active_max = 3.0 + 3.0 = 6.0 '
        '(floored at 5.0).',
        S['body']
    ))

    conf_fig = make_confidence_chart()
    story.append(fig_to_image(conf_fig, width_mm=170, height_mm=68))
    story.append(Paragraph(
        'Figure 5: Left — score→confidence mapping for different active component combinations. '
        'Right — time-based tier windows. Earlier entries require higher confidence.',
        S['caption']
    ))

    story.append(Spacer(1, 4*mm))
    story.append(Paragraph('Entry Tiers', S['h2']))
    story.append(Paragraph(
        'The system fires as soon as confidence meets the threshold for the current time tier. '
        'Earlier entry = cheaper tokens. Later entry = more signal but expensive tokens.',
        S['body']
    ))

    tier_table_data = [
        [Paragraph('Tier', S['table_header']),
         Paragraph('Time Remaining', S['table_header']),
         Paragraph('Min Confidence', S['table_header']),
         Paragraph('Token Price (typical)', S['table_header']),
         Paragraph('When it fires', S['table_header'])],
        [Paragraph('DECISIVE', ParagraphStyle('decisive', fontName='Helvetica-Bold', fontSize=9, textColor=ACCENT_RED, alignment=TA_CENTER, leading=13)),
         Paragraph('≥ 180s', S['table_cell']),
         Paragraph('85%', S['table_cell']),
         Paragraph('50–58¢', S['table_cell']),
         Paragraph('Strong early signal — delta >0.10% + high VPIN', S['table_cell'])],
        [Paragraph('HIGH', ParagraphStyle('high', fontName='Helvetica-Bold', fontSize=9, textColor=ACCENT_AMBER, alignment=TA_CENTER, leading=13)),
         Paragraph('60–180s', S['table_cell']),
         Paragraph('65%', S['table_cell']),
         Paragraph('55–65¢', S['table_cell']),
         Paragraph('Good signal mid-window — delta 0.05–0.10%', S['table_cell'])],
        [Paragraph('MODERATE', ParagraphStyle('moderate', fontName='Helvetica-Bold', fontSize=9, textColor=ACCENT_GREEN, alignment=TA_CENTER, leading=13)),
         Paragraph('10–60s', S['table_cell']),
         Paragraph('50%', S['table_cell']),
         Paragraph('58–68¢', S['table_cell']),
         Paragraph('73% win zone — morning sweet spot, delta 0.03–0.09%', S['table_cell'])],
        [Paragraph('SPIKE', ParagraphStyle('spike', fontName='Helvetica-Bold', fontSize=9, textColor=ACCENT_PURPLE, alignment=TA_CENTER, leading=13)),
         Paragraph('Any', S['table_cell']),
         Paragraph('+30% jump', S['table_cell']),
         Paragraph('Varies', S['table_cell']),
         Paragraph('Confidence jumps ≥0.30 in one eval + delta ≥0.05% + VPIN ≥0.50', S['table_cell'])],
        [Paragraph('NO FIRE', ParagraphStyle('nofire', fontName='Helvetica-Bold', fontSize=9, textColor=TEXT_MUTED, alignment=TA_CENTER, leading=13)),
         Paragraph('< 10s', S['table_cell']),
         Paragraph('N/A', S['table_cell']),
         Paragraph('N/A', S['table_cell']),
         Paragraph('No deadline fire — weak signals at last second are avoided', S['table_cell'])],
    ]
    tier_table = Table(tier_table_data, colWidths=[22*mm, 22*mm, 22*mm, 28*mm, 76*mm])
    tier_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), BG_CARD2),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [BG_CARD, BG_CARD2]),
        ('BOX', (0,0), (-1,-1), 0.5, BORDER_COLOR),
        ('INNERGRID', (0,0), (-1,-1), 0.5, BORDER_COLOR),
        ('TOPPADDING', (0,0), (-1,-1), 7),
        ('BOTTOMPADDING', (0,0), (-1,-1), 7),
        ('LEFTPADDING', (0,0), (-1,-1), 6),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
    ]))
    story.append(tier_table)

    story.append(Spacer(1, 4*mm))
    story.append(Paragraph(
        '<b>Key insight:</b> The morning session\'s 73% win rate came almost entirely from '
        'MODERATE tier entries (T-30s to T-60s). Not the DECISIVE tier — not the big obvious '
        'moves. The edge is in catching <i>moderate</i> signals where the token is still '
        'cheap enough to have good risk/reward.',
        S['body']
    ))

    story.append(PageBreak())

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION 4: CALIBRATION CHART
    # ══════════════════════════════════════════════════════════════════════════
    section_header(4, 'Signal Calibration')

    story.append(Paragraph('Morning Win Distribution', S['h2']))
    story.append(Paragraph(
        'Every live trade plotted by its signal values at entry. The "sweet spot" '
        'shows where wins clustered in the morning session — moderate delta, high VPIN, '
        'within the confidence contour lines.',
        S['body']
    ))

    cal_fig = make_calibration_chart(trades)
    story.append(fig_to_image(cal_fig, width_mm=170, height_mm=105))
    story.append(Paragraph(
        'Figure 6: Scatter of live trades. Green triangles = wins, Red inverted triangles = losses. '
        'Size proportional to |P&L|. Contour lines show confidence levels. '
        'The dashed green box marks the 73% morning win zone.',
        S['caption']
    ))

    story.append(Spacer(1, 4*mm))

    if trades:
        wins_n = len([t for t in trades if t['outcome'] in ('win', 'WIN', 'correct', 'yes')])
        losses_n = len([t for t in trades if t['outcome'] in ('loss', 'LOSS', 'incorrect', 'no')])
        total_n = len(trades)
        win_rate = wins_n / total_n if total_n > 0 else 0
        avg_win_vpin = np.mean([t['vpin'] for t in trades if t['outcome'] in ('win', 'WIN', 'correct', 'yes')]) if wins_n else 0
        avg_win_delta = np.mean([t['delta']*100 for t in trades if t['outcome'] in ('win', 'WIN', 'correct', 'yes')]) if wins_n else 0

        cal_stats = [
            [Paragraph('Metric', S['table_header']), Paragraph('Value', S['table_header'])],
            [Paragraph('Total live trades', S['table_cell_left']), Paragraph(str(total_n), S['table_cell'])],
            [Paragraph('Win rate', S['table_cell_left']), Paragraph(f'{win_rate:.1%}', ParagraphStyle('wr', fontName='Helvetica-Bold', fontSize=9, textColor=ACCENT_GREEN if win_rate > 0.5 else ACCENT_RED, alignment=TA_CENTER, leading=13))],
            [Paragraph('Avg VPIN on wins', S['table_cell_left']), Paragraph(f'{avg_win_vpin:.3f}', S['table_cell'])],
            [Paragraph('Avg |Δ| on wins', S['table_cell_left']), Paragraph(f'{avg_win_delta:.4f}%', S['table_cell'])],
        ]
        cal_table = Table(cal_stats, colWidths=[85*mm, 85*mm])
        cal_table.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), BG_CARD2),
            ('ROWBACKGROUNDS', (0,1), (-1,-1), [BG_CARD, BG_CARD2]),
            ('BOX', (0,0), (-1,-1), 0.5, BORDER_COLOR),
            ('INNERGRID', (0,0), (-1,-1), 0.5, BORDER_COLOR),
            ('TOPPADDING', (0,0), (-1,-1), 6),
            ('BOTTOMPADDING', (0,0), (-1,-1), 6),
            ('LEFTPADDING', (0,0), (-1,-1), 8),
        ]))
        story.append(cal_table)
    else:
        story.append(Paragraph(
            '<font color="#D29922">Note: Database unavailable — chart uses synthetic representative data '
            'matching the documented morning win characteristics (delta 0.03–0.09%, VPIN 0.65–0.93).</font>',
            S['body_muted']
        ))

    story.append(PageBreak())

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION 5: STAKE SIZING
    # ══════════════════════════════════════════════════════════════════════════
    section_header(5, 'Stake Sizing')

    story.append(Paragraph('Price-Scaled Position Sizing', S['h2']))
    story.append(Paragraph(
        'A fixed dollar stake at different token prices creates very different risk/reward profiles. '
        'A 30¢ token that wins pays 233% return. A 65¢ token that wins pays 54% return. '
        'We scale stakes to equalise the expected dollar return across price points.',
        S['body']
    ))

    story.append(Paragraph('<b>Formula (from five_min_vpin.py):</b>', S['h3']))
    stake_code = [
        'base_stake = bankroll × bet_fraction        # e.g. $160 × 10% = $16',
        '',
        'tp = clamp(token_price, 0.30, 0.65)         # Valid price range',
        'price_multiplier = (1 − tp) / 0.50          # 40¢ → 1.2x, 50¢ → 1.0x, 60¢ → 0.8x',
        'price_multiplier = clamp(multiplier, 0.5, 1.5)   # Floor 0.5x, cap 1.5x',
        '',
        'final_stake = base_stake × price_multiplier',
    ]
    for line in stake_code:
        story.append(Paragraph(line, S['code']))

    stake_fig = make_stake_chart()
    story.append(fig_to_image(stake_fig, width_mm=170, height_mm=74))
    story.append(Paragraph(
        'Figure 7: Left — multiplier curve (cheaper tokens get bigger bets). '
        'Right — P&L at $16 base stake across the token price range. '
        'The amber dashed line shows expected value at 73% win rate.',
        S['caption']
    ))

    story.append(Spacer(1, 4*mm))

    # Stake examples table
    ex_data = [
        [Paragraph('Token Price', S['table_header']),
         Paragraph('Multiplier', S['table_header']),
         Paragraph('Stake ($16 base)', S['table_header']),
         Paragraph('Win Payout', S['table_header']),
         Paragraph('Net Profit', S['table_header']),
         Paragraph('Max Loss', S['table_header'])],
    ]
    for tp, label in [(0.30, '30¢'), (0.40, '40¢'), (0.50, '50¢'), (0.55, '55¢'), (0.60, '60¢'), (0.65, '65¢')]:
        tp_clamp = max(0.30, min(0.65, tp))
        mult = max(0.5, min(1.5, (1 - tp_clamp) / 0.50))
        stake = 16.0 * mult
        win_payout = stake / tp  # total return
        net_profit = win_payout - stake
        ex_data.append([
            Paragraph(label, S['table_cell']),
            Paragraph(f'{mult:.2f}×', S['table_cell']),
            Paragraph(f'${stake:.2f}', S['table_cell']),
            Paragraph(f'${win_payout:.2f}', S['table_cell']),
            Paragraph(f'+${net_profit:.2f}', ParagraphStyle('green_cell', fontName='Helvetica-Bold', fontSize=9, textColor=ACCENT_GREEN, alignment=TA_CENTER, leading=13)),
            Paragraph(f'-${stake:.2f}', ParagraphStyle('red_cell', fontName='Helvetica-Bold', fontSize=9, textColor=ACCENT_RED, alignment=TA_CENTER, leading=13)),
        ])
    ex_table = Table(ex_data, colWidths=[28*mm, 25*mm, 32*mm, 30*mm, 28*mm, 27*mm])
    ex_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), BG_CARD2),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [BG_CARD, BG_CARD2]),
        ('BOX', (0,0), (-1,-1), 0.5, BORDER_COLOR),
        ('INNERGRID', (0,0), (-1,-1), 0.5, BORDER_COLOR),
        ('TOPPADDING', (0,0), (-1,-1), 6),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
        ('LEFTPADDING', (0,0), (-1,-1), 6),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        # Highlight the 50¢ row
        ('BACKGROUND', (0,3), (-1,3), BG_CARD2),
        ('FONTNAME', (0,3), (-1,3), 'Helvetica-Bold'),
    ]))
    story.append(ex_table)

    story.append(PageBreak())

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION 6: RISK GATES
    # ══════════════════════════════════════════════════════════════════════════
    section_header(6, 'Risk Gates')

    story.append(Paragraph('Every Order Must Pass All Six Gates', S['h2']))
    story.append(Paragraph(
        'The risk system is a mandatory sequential filter. No signal, however strong, bypasses '
        'these gates. They exist to protect the bankroll against bad luck streaks, connectivity '
        'failures, and pricing errors.',
        S['body']
    ))

    risk_fig = make_risk_flow_diagram()
    story.append(fig_to_image(risk_fig, width_mm=170, height_mm=105))
    story.append(Paragraph(
        'Figure 8: Risk gate flow diagram. Each gate is independently checked. '
        'A reject at any stage cancels the trade completely — no fallback entries.',
        S['caption']
    ))

    story.append(Spacer(1, 4*mm))

    risk_details = [
        [Paragraph('Gate', S['table_header']),
         Paragraph('Condition', S['table_header']),
         Paragraph('Why', S['table_header']),
         Paragraph('Where in code', S['table_header'])],
        [Paragraph('VPIN Gate', ParagraphStyle('g', fontName='Helvetica-Bold', fontSize=9, textColor=ACCENT_PURPLE, leading=13)),
         Paragraph('VPIN ≥ 0.50', S['table_cell']),
         Paragraph('No informed flow = no edge', S['table_cell_left']),
         Paragraph('window_evaluator.py L.140', S['table_cell'])],
        [Paragraph('Delta Gate', ParagraphStyle('g', fontName='Helvetica-Bold', fontSize=9, textColor=ACCENT_AMBER, leading=13)),
         Paragraph('|Δ| ≥ 0.02%', S['table_cell']),
         Paragraph('Below morning minimum edge threshold', S['table_cell_left']),
         Paragraph('window_evaluator.py L.144', S['table_cell'])],
        [Paragraph('Confidence Gate', ParagraphStyle('g', fontName='Helvetica-Bold', fontSize=9, textColor=ACCENT_BLUE, leading=13)),
         Paragraph('conf ≥ tier threshold', S['table_cell']),
         Paragraph('DECISIVE/HIGH/MODERATE depending on time', S['table_cell_left']),
         Paragraph('window_evaluator.py L.147', S['table_cell'])],
        [Paragraph('Risk Manager', ParagraphStyle('g', fontName='Helvetica-Bold', fontSize=9, textColor=ACCENT_RED, leading=13)),
         Paragraph('6 sub-gates', S['table_cell']),
         Paragraph('Kill switch, daily loss, position size, exposure, cooldown, venue', S['table_cell_left']),
         Paragraph('risk_manager.py', S['table_cell'])],
        [Paragraph('Token Price', ParagraphStyle('g', fontName='Helvetica-Bold', fontSize=9, textColor=ACCENT_GREEN, leading=13)),
         Paragraph('30–65¢ (5m)\n30–70¢ (15m)', S['table_cell']),
         Paragraph('Outside range = bad R/R. Stale price or mispriced market.', S['table_cell_left']),
         Paragraph('five_min_vpin.py', S['table_cell'])],
        [Paragraph('Fill Verify', ParagraphStyle('g', fontName='Helvetica-Bold', fontSize=9, textColor=ACCENT_BLUE, leading=13)),
         Paragraph('Poll 60s max', S['table_cell']),
         Paragraph('Confirm CLOB fill before recording trade', S['table_cell_left']),
         Paragraph('five_min_vpin.py L.~380', S['table_cell'])],
    ]
    risk_table = Table(risk_details, colWidths=[28*mm, 28*mm, 64*mm, 50*mm])
    risk_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), BG_CARD2),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [BG_CARD, BG_CARD2]),
        ('BOX', (0,0), (-1,-1), 0.5, BORDER_COLOR),
        ('INNERGRID', (0,0), (-1,-1), 0.5, BORDER_COLOR),
        ('TOPPADDING', (0,0), (-1,-1), 6),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
        ('LEFTPADDING', (0,0), (-1,-1), 6),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
    ]))
    story.append(risk_table)

    story.append(Spacer(1, 4*mm))

    # Risk Manager sub-gates
    story.append(Paragraph('Risk Manager Sub-Gates (risk_manager.py)', S['h3']))
    rm_data = [
        [Paragraph('Sub-gate', S['table_header']), Paragraph('Threshold', S['table_header']), Paragraph('Notes', S['table_header'])],
        [Paragraph('Kill switch', S['table_cell_left']), Paragraph('Drawdown ≥ max_drawdown_kill', S['table_cell']), Paragraph('Auto + manual trigger', S['table_cell'])],
        [Paragraph('Daily loss limit', S['table_cell_left']), Paragraph('Today P&L ≤ −10% starting balance', S['table_cell']), Paragraph('Skipped in paper mode', S['table_cell'])],
        [Paragraph('Position limit', S['table_cell_left']), Paragraph('Stake ≤ bankroll × bet_fraction', S['table_cell']), Paragraph('Per-trade size cap', S['table_cell'])],
        [Paragraph('Exposure limit', S['table_cell_left']), Paragraph('Open exposure ≤ 30% bankroll', S['table_cell']), Paragraph('Sum of all open orders', S['table_cell'])],
        [Paragraph('Cooldown', S['table_cell_left']), Paragraph('3 consecutive losses → 15 min pause', S['table_cell']), Paragraph('Skipped in paper mode', S['table_cell'])],
        [Paragraph('Venue connectivity', S['table_cell_left']), Paragraph('At least 1 venue connected', S['table_cell']), Paragraph('Polymarket or Opinion Markets', S['table_cell'])],
    ]
    rm_table = Table(rm_data, colWidths=[42*mm, 66*mm, 62*mm])
    rm_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), BG_CARD2),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [BG_CARD, BG_CARD2]),
        ('BOX', (0,0), (-1,-1), 0.5, BORDER_COLOR),
        ('INNERGRID', (0,0), (-1,-1), 0.5, BORDER_COLOR),
        ('TOPPADDING', (0,0), (-1,-1), 5),
        ('BOTTOMPADDING', (0,0), (-1,-1), 5),
        ('LEFTPADDING', (0,0), (-1,-1), 6),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('FONTSIZE', (0,0), (-1,-1), 8.5),
    ]))
    story.append(rm_table)

    story.append(PageBreak())

    # ══════════════════════════════════════════════════════════════════════════
    # SECTION 7: WHAT WE LEARNED
    # ══════════════════════════════════════════════════════════════════════════
    section_header(7, 'What We Learned')

    story.append(Paragraph('The Morning Session (73% Win Rate)', S['h2']))
    story.append(Paragraph(
        'The morning live session produced a clear pattern. Wins were concentrated in '
        'a specific signal zone, and losses revealed what we were doing wrong.',
        S['body']
    ))

    morning_data = [
        [Paragraph('Characteristic', S['table_header']),
         Paragraph('Morning Wins', S['table_header']),
         Paragraph('Afternoon Losses', S['table_header'])],
        [Paragraph('|Delta| range', S['table_cell_left']),
         Paragraph('0.03–0.09%', ParagraphStyle('gv', fontName='Helvetica-Bold', fontSize=9, textColor=ACCENT_GREEN, leading=13, alignment=TA_CENTER)),
         Paragraph('0.10%+ (big moves)', ParagraphStyle('rv', fontName='Helvetica-Bold', fontSize=9, textColor=ACCENT_RED, leading=13, alignment=TA_CENTER))],
        [Paragraph('VPIN range', S['table_cell_left']),
         Paragraph('0.65–0.93', ParagraphStyle('gv', fontName='Helvetica-Bold', fontSize=9, textColor=ACCENT_GREEN, leading=13, alignment=TA_CENTER)),
         Paragraph('Not checked properly', ParagraphStyle('rv', fontName='Helvetica-Bold', fontSize=9, textColor=ACCENT_RED, leading=13, alignment=TA_CENTER))],
        [Paragraph('Tier', S['table_cell_left']),
         Paragraph('MODERATE (T-30s to T-60s)', S['table_cell']),
         Paragraph('DECISIVE + MODERATE', S['table_cell'])],
        [Paragraph('Entry type', S['table_cell_left']),
         Paragraph('Limit orders at market', S['table_cell']),
         Paragraph('Market orders — bad fills', ParagraphStyle('rv', fontName='Helvetica-Bold', fontSize=9, textColor=ACCENT_RED, leading=13, alignment=TA_CENTER))],
        [Paragraph('Win rate', S['table_cell_left']),
         Paragraph('73%', ParagraphStyle('gv', fontName='Helvetica-Bold', fontSize=9, textColor=ACCENT_GREEN, leading=13, alignment=TA_CENTER)),
         Paragraph('< 40%', ParagraphStyle('rv', fontName='Helvetica-Bold', fontSize=9, textColor=ACCENT_RED, leading=13, alignment=TA_CENTER))],
    ]
    morning_table = Table(morning_data, colWidths=[50*mm, 60*mm, 60*mm])
    morning_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), BG_CARD2),
        ('BACKGROUND', (1,0), (1,0), BG_CARD2),
        ('BACKGROUND', (2,0), (2,0), BG_CARD2),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [BG_CARD, BG_CARD2]),
        ('BOX', (0,0), (-1,-1), 0.5, BORDER_COLOR),
        ('INNERGRID', (0,0), (-1,-1), 0.5, BORDER_COLOR),
        ('TOPPADDING', (0,0), (-1,-1), 7),
        ('BOTTOMPADDING', (0,0), (-1,-1), 7),
        ('LEFTPADDING', (0,0), (-1,-1), 8),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        # Green header for wins col
        ('BACKGROUND', (1,0), (1,0), HexColor('#1a3a1a')),
        # Red header for losses col
        ('BACKGROUND', (2,0), (2,0), HexColor('#3a1a1a')),
    ]))
    story.append(morning_table)
    story.append(Spacer(1, 5*mm))

    story.append(Paragraph('The Root Cause — v2 Overconfident Evaluator', S['h2']))
    story.append(Paragraph(
        'The afternoon failures traced back to a single bug in the v2 evaluator: delta was '
        'weighted at 5–7× while VPIN was capped at 2–3×. When delta was large (0.10%+), '
        'confidence would hit 0.85+ from delta alone, ignoring whether VPIN confirmed it. '
        'The system was "confident" in moves that had no smart-money confirmation.',
        S['body']
    ))

    # v2 vs v3 comparison
    ver_data = [
        [Paragraph('Aspect', S['table_header']),
         Paragraph('v2 (broken)', S['table_header']),
         Paragraph('v3.1 (current)', S['table_header'])],
        [Paragraph('Delta max weight', S['table_cell_left']),
         Paragraph('5–7', ParagraphStyle('rv', fontName='Helvetica-Bold', fontSize=9, textColor=ACCENT_RED, leading=13, alignment=TA_CENTER)),
         Paragraph('3.0', ParagraphStyle('gv', fontName='Helvetica-Bold', fontSize=9, textColor=ACCENT_GREEN, leading=13, alignment=TA_CENTER))],
        [Paragraph('VPIN max weight', S['table_cell_left']),
         Paragraph('2–3', ParagraphStyle('rv', fontName='Helvetica-Bold', fontSize=9, textColor=ACCENT_RED, leading=13, alignment=TA_CENTER)),
         Paragraph('3.0 (equal)', ParagraphStyle('gv', fontName='Helvetica-Bold', fontSize=9, textColor=ACCENT_GREEN, leading=13, alignment=TA_CENTER))],
        [Paragraph('active_max denominator', S['table_cell_left']),
         Paragraph('Fixed 12.0 (always)', ParagraphStyle('rv', fontName='Helvetica-Bold', fontSize=9, textColor=ACCENT_RED, leading=13, alignment=TA_CENTER)),
         Paragraph('Dynamic (active only)', ParagraphStyle('gv', fontName='Helvetica-Bold', fontSize=9, textColor=ACCENT_GREEN, leading=13, alignment=TA_CENTER))],
        [Paragraph('VPIN gate', S['table_cell_left']),
         Paragraph('Not enforced separately', ParagraphStyle('rv', fontName='Helvetica-Bold', fontSize=9, textColor=ACCENT_RED, leading=13, alignment=TA_CENTER)),
         Paragraph('Hard gate: VPIN ≥ 0.50', ParagraphStyle('gv', fontName='Helvetica-Bold', fontSize=9, textColor=ACCENT_GREEN, leading=13, alignment=TA_CENTER))],
        [Paragraph('Deadline fire', S['table_cell_left']),
         Paragraph('Yes (even weak signals)', ParagraphStyle('rv', fontName='Helvetica-Bold', fontSize=9, textColor=ACCENT_RED, leading=13, alignment=TA_CENTER)),
         Paragraph('No (removed)', ParagraphStyle('gv', fontName='Helvetica-Bold', fontSize=9, textColor=ACCENT_GREEN, leading=13, alignment=TA_CENTER))],
        [Paragraph('CoinGlass inflation', S['table_cell_left']),
         Paragraph('Zeros inflated confidence', ParagraphStyle('rv', fontName='Helvetica-Bold', fontSize=9, textColor=ACCENT_RED, leading=13, alignment=TA_CENTER)),
         Paragraph('Dynamic norm prevents this', ParagraphStyle('gv', fontName='Helvetica-Bold', fontSize=9, textColor=ACCENT_GREEN, leading=13, alignment=TA_CENTER))],
    ]
    ver_table = Table(ver_data, colWidths=[55*mm, 55*mm, 60*mm])
    ver_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), BG_CARD2),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [BG_CARD, BG_CARD2]),
        ('BOX', (0,0), (-1,-1), 0.5, BORDER_COLOR),
        ('INNERGRID', (0,0), (-1,-1), 0.5, BORDER_COLOR),
        ('TOPPADDING', (0,0), (-1,-1), 7),
        ('BOTTOMPADDING', (0,0), (-1,-1), 7),
        ('LEFTPADDING', (0,0), (-1,-1), 8),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        # Red old col header
        ('BACKGROUND', (1,0), (1,0), HexColor('#3a1a1a')),
        # Green new col header
        ('BACKGROUND', (2,0), (2,0), HexColor('#1a3a1a')),
    ]))
    story.append(ver_table)
    story.append(Spacer(1, 4*mm))

    # Key lessons box
    lessons_data = [[
        Paragraph(
            '<b>Key Lessons:</b><br/><br/>'
            '• <b>The edge is in VPIN, not delta alone.</b> Delta tells you direction. '
            'VPIN tells you whether to trust it. Without VPIN ≥ 0.50, delta is noise.<br/><br/>'
            '• <b>Moderate signals, not big moves.</b> Big delta (>0.10%) means the market '
            'has already moved. The edge is catching 0.03–0.09% with confirming VPIN '
            'before the market fully reprices.<br/><br/>'
            '• <b>Equal weighting matters.</b> When VPIN and delta have equal max weights (3.0 each), '
            'neither can dominate. Both must agree for high confidence.<br/><br/>'
            '• <b>Dynamic normalisation prevents phantom confidence.</b> Inactive components '
            'must not count toward the denominator.',
            ParagraphStyle('lessons', fontName='Helvetica', fontSize=10, textColor=TEXT_PRIMARY,
                           leading=16, leftIndent=4)
        )
    ]]
    lessons_table = Table(lessons_data, colWidths=[170*mm])
    lessons_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), BG_CARD2),
        ('BOX', (0,0), (-1,-1), 1.5, ACCENT_GREEN),
        ('TOPPADDING', (0,0), (-1,-1), 12),
        ('BOTTOMPADDING', (0,0), (-1,-1), 12),
        ('LEFTPADDING', (0,0), (-1,-1), 14),
        ('RIGHTPADDING', (0,0), (-1,-1), 14),
    ]))
    story.append(lessons_table)

    # ══════════════════════════════════════════════════════════════════════════
    # BUILD
    # ══════════════════════════════════════════════════════════════════════════
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    doc.build(story, onFirstPage=add_background, onLaterPages=add_background)
    print(f"✅ PDF saved: {OUTPUT_PATH}")


if __name__ == '__main__':
    print("Fetching trade data from DB...")
    trades = fetch_trade_data()
    print(f"Got {len(trades)} trades")
    print("Building PDF...")
    build_pdf(trades)
