"""
Generate compact Engine Config Reference PDF with architecture diagram.
"""

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.platypus.flowables import Flowable
from reportlab.pdfgen import canvas as pdfcanvas
import os

OUTPUT = "/root/.openclaw/workspace-novakash/novakash/docs/engine-config-reference.pdf"

# ── Colours ───────────────────────────────────────────────────────────────────
C_DARK    = colors.HexColor("#1a1a2e")
C_BLUE    = colors.HexColor("#0f3460")
C_ACCENT  = colors.HexColor("#e94560")
C_CYAN    = colors.HexColor("#16213e")
C_LIGHT   = colors.HexColor("#a8b2d8")
C_GREEN   = colors.HexColor("#2d6a4f")
C_ORANGE  = colors.HexColor("#b5451b")
C_PURPLE  = colors.HexColor("#4a1a5e")
C_TEAL    = colors.HexColor("#1b4d4a")
C_GREY    = colors.HexColor("#3a3a4a")
C_WHITE   = colors.white
C_OFFWHITE = colors.HexColor("#e8e8f0")
C_YELLOW  = colors.HexColor("#7a6000")

# ── Architecture Diagram Flowable ─────────────────────────────────────────────
class ArchDiagram(Flowable):
    def __init__(self, width, height):
        Flowable.__init__(self)
        self.width = width
        self.height = height

    def draw(self):
        c = self.canv
        w, h = self.width, self.height

        # Background
        c.setFillColor(C_DARK)
        c.roundRect(0, 0, w, h, 6, fill=1, stroke=0)

        # Title
        c.setFillColor(C_ACCENT)
        c.setFont("Helvetica-Bold", 9)
        c.drawCentredString(w/2, h - 14, "NOVAKASH ENGINE — ARCHITECTURE")

        # ── Main pipeline boxes ────────────────────────────────────────────
        # Box definitions: (label, sublabel, x, y, bw, bh, color)
        BOX_W = 110
        BOX_H = 16
        cx = w / 2

        pipeline = [
            ("Binance WebSocket",    "live BTC price feed",        C_TEAL),
            ("VPIN Calculator",      "volume-sync'd PIN signal",   C_BLUE),
            ("5-Min Strategy",       "delta + VPIN analysis",      C_BLUE),
            ("Risk Manager",         "bet size · exposure · kill", C_ORANGE),
            ("Polymarket CLOB",      "place order via API",        C_PURPLE),
            ("Order Manager",        "track fills · resolve",      C_CYAN),
            ("Telegram Alerter",     "notify Billy",               C_GREEN),
            ("PostgreSQL DB",        "persist trades",             C_GREY),
        ]

        top_y = h - 30
        step_y = 24
        box_xs = cx - BOX_W/2

        box_positions = []
        for i, (label, sub, col) in enumerate(pipeline):
            by = top_y - i * step_y - BOX_H
            box_positions.append((box_xs, by, BOX_W, BOX_H))

            # Box
            c.setFillColor(col)
            c.roundRect(box_xs, by, BOX_W, BOX_H, 3, fill=1, stroke=0)

            # Label
            c.setFillColor(C_WHITE)
            c.setFont("Helvetica-Bold", 7)
            c.drawString(box_xs + 4, by + BOX_H - 8, label)

            # Sub-label
            c.setFillColor(C_LIGHT)
            c.setFont("Helvetica", 5.5)
            c.drawString(box_xs + 4, by + 3, sub)

            # Arrow down (except last)
            if i < len(pipeline) - 1:
                ax = cx
                ay = by
                c.setStrokeColor(C_ACCENT)
                c.setLineWidth(1)
                c.line(ax, ay, ax, ay - (step_y - BOX_H))
                # Arrowhead
                arrowY = ay - (step_y - BOX_H)
                c.setFillColor(C_ACCENT)
                p = c.beginPath()
                p.moveTo(ax-3, arrowY+4)
                p.lineTo(ax+3, arrowY+4)
                p.lineTo(ax, arrowY)
                p.close()
                c.drawPath(p, fill=1, stroke=0)

        # ── Regime Classifier (side box next to VPIN) ─────────────────────
        rc_x = box_xs + BOX_W + 8
        rc_y = box_positions[1][1]  # same y as VPIN
        rc_w = 68
        rc_h = BOX_H

        c.setFillColor(C_BLUE)
        c.roundRect(rc_x, rc_y, rc_w, rc_h, 3, fill=1, stroke=0)
        c.setFillColor(C_WHITE)
        c.setFont("Helvetica-Bold", 7)
        c.drawString(rc_x + 4, rc_y + rc_h - 8, "Regime Classifier")
        c.setFillColor(C_LIGHT)
        c.setFont("Helvetica", 5.5)
        c.drawString(rc_x + 4, rc_y + 3, "informed/cascade/calm")

        # Arrow from VPIN to Regime Classifier
        vpin_bx, vpin_by, vpin_bw, vpin_bh = box_positions[1]
        c.setStrokeColor(C_CYAN)
        c.setLineWidth(0.8)
        c.line(vpin_bx + vpin_bw, vpin_by + vpin_bh/2, rc_x, rc_y + rc_h/2)
        # small arrow
        c.setFillColor(C_CYAN)
        ax2 = rc_x
        ay2 = rc_y + rc_h/2
        p2 = c.beginPath()
        p2.moveTo(ax2-4, ay2+2)
        p2.lineTo(ax2-4, ay2-2)
        p2.lineTo(ax2, ay2)
        p2.close()
        c.drawPath(p2, fill=1, stroke=0)

        # ── Feedback loops (right side) ────────────────────────────────────
        fb_x = w - 5
        fb_w = 72
        fb_label_x = w - fb_w - 2

        def draw_feedback(label, sublabel, top_idx, bot_idx, col, offset_x=0):
            top_box = box_positions[top_idx]
            bot_box = box_positions[bot_idx]
            top_cy = top_box[1] + top_box[3]/2
            bot_cy = bot_box[1] + bot_box[3]/2
            lx = fb_label_x - offset_x

            # Bracket line
            c.setStrokeColor(col)
            c.setLineWidth(0.7)
            c.line(lx + fb_w - 2, top_cy, lx + fb_w - 2, bot_cy)
            c.line(lx + fb_w - 2, top_cy, lx + fb_w + 2, top_cy)
            c.line(lx + fb_w - 2, bot_cy, lx + fb_w + 2, bot_cy)

            # Label box
            mid_y = (top_cy + bot_cy) / 2
            bh2 = 18
            c.setFillColor(col)
            c.roundRect(lx, mid_y - bh2/2, fb_w - 6, bh2, 2, fill=1, stroke=0)
            c.setFillColor(C_WHITE)
            c.setFont("Helvetica-Bold", 5.5)
            c.drawString(lx + 2, mid_y + 2, label)
            c.setFont("Helvetica", 4.8)
            c.drawString(lx + 2, mid_y - 5, sublabel)

        # Heartbeat: Polymarket CLOB (idx4) → Risk Manager (idx3)
        draw_feedback("Heartbeat (10s)",   "wallet balance → bankroll",    4, 3, C_GREEN, offset_x=0)
        # RuntimeConfig: DB (idx7) → Risk Manager (idx3)
        draw_feedback("RuntimeConfig",     "DB configs → hot reload",      7, 3, C_ORANGE, offset_x=78)
        # Gamma API: side note near Polymarket CLOB
        poly_box = box_positions[4]
        gamma_x = box_xs - 80
        gamma_y = poly_box[1]
        c.setFillColor(C_PURPLE)
        c.roundRect(gamma_x, gamma_y, 74, BOX_H, 3, fill=1, stroke=0)
        c.setFillColor(C_WHITE)
        c.setFont("Helvetica-Bold", 6)
        c.drawString(gamma_x + 3, gamma_y + BOX_H - 8, "Gamma API")
        c.setFillColor(C_LIGHT)
        c.setFont("Helvetica", 5)
        c.drawString(gamma_x + 3, gamma_y + 3, "market data + token prices")
        # arrow to CLOB
        c.setStrokeColor(C_PURPLE)
        c.setLineWidth(0.8)
        c.line(gamma_x + 74, gamma_y + BOX_H/2, box_xs, poly_box[1] + BOX_H/2)
        c.setFillColor(C_PURPLE)
        bx3 = box_xs
        by3 = poly_box[1] + BOX_H/2
        p3 = c.beginPath()
        p3.moveTo(bx3-4, by3+2)
        p3.lineTo(bx3-4, by3-2)
        p3.lineTo(bx3, by3)
        p3.close()
        c.drawPath(p3, fill=1, stroke=0)

        # ── Legend ────────────────────────────────────────────────────────
        legend_y = 4
        c.setFont("Helvetica", 5)
        items = [
            (C_ACCENT, "Main data flow"),
            (C_CYAN, "Signal branch"),
            (C_GREEN, "Heartbeat feedback"),
            (C_ORANGE, "RuntimeConfig sync"),
            (C_PURPLE, "External API"),
        ]
        lx = 6
        for col, txt in items:
            c.setFillColor(col)
            c.rect(lx, legend_y, 8, 5, fill=1, stroke=0)
            c.setFillColor(C_LIGHT)
            c.drawString(lx + 10, legend_y + 1, txt)
            lx += 72


# ── Build PDF ─────────────────────────────────────────────────────────────────
def build():
    doc = SimpleDocTemplate(
        OUTPUT,
        pagesize=A4,
        leftMargin=10*mm, rightMargin=10*mm,
        topMargin=10*mm, bottomMargin=10*mm,
    )

    W = A4[0] - 20*mm  # usable width

    # Styles
    styles = getSampleStyleSheet()
    BODY = ParagraphStyle("body", fontSize=8, leading=10, fontName="Helvetica")
    H1   = ParagraphStyle("h1",   fontSize=11, leading=13, fontName="Helvetica-Bold",
                          textColor=C_ACCENT, spaceAfter=3)
    H2   = ParagraphStyle("h2",   fontSize=9,  leading=11, fontName="Helvetica-Bold",
                          textColor=C_LIGHT,  spaceAfter=2)
    MONO = ParagraphStyle("mono", fontSize=7,  leading=9,  fontName="Courier")
    NOTE = ParagraphStyle("note", fontSize=7,  leading=9,  fontName="Helvetica-Oblique",
                          textColor=C_LIGHT)

    story = []

    # ── Page 1: Title + Architecture ──────────────────────────────────────────
    story.append(Paragraph("⚙ Novakash Engine — Config Reference", H1))
    story.append(Paragraph(
        "Auto-generated from source. Production values reflect Railway deployment as of 2026-04-02. "
        "Priority: DB trading_configs &gt; env vars &gt; code defaults.",
        NOTE
    ))
    story.append(Spacer(1, 2*mm))

    # Architecture diagram — full width, ~165mm tall to fit on page with tables
    story.append(ArchDiagram(W, 185))
    story.append(Spacer(1, 2*mm))

    # ── Dense table helper ─────────────────────────────────────────────────────
    def cat_row(label, col):
        return [Paragraph(f"<b>{label}</b>", ParagraphStyle(
            "ch", fontSize=7, fontName="Helvetica-Bold", textColor=C_WHITE
        )), "", "", ""]

    def var_row(var, val, default, desc):
        s = ParagraphStyle("tr", fontSize=6.5, leading=8, fontName="Courier")
        sd = ParagraphStyle("td", fontSize=6.5, leading=8, fontName="Helvetica")
        return [
            Paragraph(var, s),
            Paragraph(str(val), s),
            Paragraph(str(default), s),
            Paragraph(desc, sd),
        ]

    HDR_ROW = [
        Paragraph("<b>Variable</b>", ParagraphStyle("hd", fontSize=7, fontName="Helvetica-Bold", textColor=C_WHITE)),
        Paragraph("<b>Prod Value</b>", ParagraphStyle("hd", fontSize=7, fontName="Helvetica-Bold", textColor=C_WHITE)),
        Paragraph("<b>Default</b>", ParagraphStyle("hd", fontSize=7, fontName="Helvetica-Bold", textColor=C_WHITE)),
        Paragraph("<b>Description</b>", ParagraphStyle("hd", fontSize=7, fontName="Helvetica-Bold", textColor=C_WHITE)),
    ]

    COL_W = [W*0.30, W*0.14, W*0.14, W*0.42]

    def make_table(rows, cat_indices):
        """Build a TableStyle with colored category header rows."""
        all_rows = [HDR_ROW] + rows
        t = Table(all_rows, colWidths=COL_W, repeatRows=1)

        style_cmds = [
            # Header
            ("BACKGROUND", (0,0), (-1,0), C_DARK),
            ("TEXTCOLOR",  (0,0), (-1,0), C_WHITE),
            ("FONTSIZE",   (0,0), (-1,0), 7),
            ("BOTTOMPADDING", (0,0), (-1,0), 2),
            ("TOPPADDING",    (0,0), (-1,0), 2),
            # All rows
            ("FONTSIZE",      (0,1), (-1,-1), 6.5),
            ("BOTTOMPADDING", (0,1), (-1,-1), 1),
            ("TOPPADDING",    (0,1), (-1,-1), 1),
            ("LEFTPADDING",   (0,0), (-1,-1), 3),
            ("RIGHTPADDING",  (0,0), (-1,-1), 2),
            ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.HexColor("#12121e"), colors.HexColor("#1a1a2e")]),
            ("TEXTCOLOR",     (0,1), (-1,-1), C_OFFWHITE),
            ("LINEBELOW",     (0,0), (-1,-1), 0.2, colors.HexColor("#2a2a3e")),
        ]
        # Category rows (offset by 1 for header)
        cat_colors = [C_BLUE, C_TEAL, C_ORANGE, C_GREEN, C_PURPLE, C_GREY, C_YELLOW, C_CYAN,
                      colors.HexColor("#4a2a0e"), colors.HexColor("#1a3a1a")]
        for ci, (row_idx, col) in enumerate(cat_indices):
            actual = row_idx + 1  # +1 for header
            style_cmds += [
                ("BACKGROUND", (0, actual), (-1, actual), col),
                ("TEXTCOLOR",  (0, actual), (-1, actual), C_WHITE),
                ("SPAN",       (0, actual), (-1, actual)),
                ("FONTSIZE",   (0, actual), (-1, actual), 7),
                ("BOTTOMPADDING", (0, actual), (-1, actual), 2),
                ("TOPPADDING",    (0, actual), (-1, actual), 2),
            ]

        t.setStyle(TableStyle(style_cmds))
        return t

    # ── Section 1: Core / Mode ────────────────────────────────────────────────
    story.append(Paragraph("1. Core Trading Mode", H2))

    rows = []
    cats = []
    cats.append((len(rows), C_BLUE)); rows.append(cat_row("CORE / MODE", C_BLUE))
    rows.append(var_row("PAPER_MODE",              "false",  "true",  "Disable paper trading (real orders)"))
    rows.append(var_row("LIVE_TRADING_ENABLED",    "true",   "false", "Master live-trading gate"))
    rows.append(var_row("STARTING_BANKROLL",       "208",    "500.0", "Initial bankroll USD"))
    cats.append((len(rows), C_TEAL)); rows.append(cat_row("DATABASE", C_TEAL))
    rows.append(var_row("DATABASE_URL",            "****",   "(required)", "Async PostgreSQL DSN (asyncpg)"))
    story.append(make_table(rows, cats))
    story.append(Spacer(1, 2*mm))

    # ── Section 2: Polymarket ─────────────────────────────────────────────────
    story.append(Paragraph("2. Polymarket / Execution", H2))

    rows = []; cats = []
    cats.append((len(rows), C_PURPLE)); rows.append(cat_row("POLYMARKET CREDENTIALS", C_PURPLE))
    rows.append(var_row("POLY_PRIVATE_KEY",        "****",   "",      "Ethereum private key (EIP-712 signer)"))
    rows.append(var_row("POLY_API_KEY",            "****",   "",      "CLOB API key"))
    rows.append(var_row("POLY_API_SECRET",         "****",   "",      "CLOB API secret"))
    rows.append(var_row("POLY_API_PASSPHRASE",     "****",   "",      "CLOB API passphrase"))
    rows.append(var_row("POLY_FUNDER_ADDRESS",     "0x330e…0b6b", "",  "Polymarket funder wallet address"))
    rows.append(var_row("POLY_SIGNATURE_TYPE",     "1",      "0",     "Sig type: 0=EOA, 1=contract wallet"))
    cats.append((len(rows), C_TEAL)); rows.append(cat_row("POLYMARKET SETTINGS", C_TEAL))
    rows.append(var_row("POLY_BTC_TOKEN_IDS",      "(set)",  "",      "Comma-separated token IDs to watch"))
    rows.append(var_row("POLY_WINDOW_SECONDS",     "300",    "300",   "Resolution window seconds"))
    rows.append(var_row("POLYMARKET_FEE_MULT",     "0.072",  "0.072", "Fee multiplier for Polymarket crypto"))
    story.append(make_table(rows, cats))
    story.append(Spacer(1, 2*mm))

    # ── Section 3: Risk Manager ────────────────────────────────────────────────
    story.append(Paragraph("3. Risk Manager", H2))

    rows = []; cats = []
    cats.append((len(rows), C_ORANGE)); rows.append(cat_row("RISK GATES", C_ORANGE))
    rows.append(var_row("BET_FRACTION",            "0.10",   "0.025", "Stake as fraction of bankroll per trade"))
    rows.append(var_row("MIN_BET_USD",             "2.0",    "2.0",   "Minimum bet size USD"))
    rows.append(var_row("MAX_POSITION_USD",        "(set)",  "500.0", "Hard cap on single position USD"))
    rows.append(var_row("MAX_OPEN_EXPOSURE_PCT",   "0.45",   "0.30",  "Max total open positions / bankroll"))
    rows.append(var_row("MAX_DRAWDOWN_KILL",       "0.45",   "0.45",  "Kill switch: drawdown from peak"))
    rows.append(var_row("DAILY_LOSS_LIMIT_PCT",    "0.30",   "0.10",  "Max daily loss as % of day-start balance"))
    rows.append(var_row("DAILY_LOSS_LIMIT_USD",    "(derived)", "50.0","Absolute daily loss cap (fallback)"))
    cats.append((len(rows), C_GREY)); rows.append(cat_row("COOLDOWN / STREAK", C_GREY))
    rows.append(var_row("CONSECUTIVE_LOSS_COOLDOWN","3",     "3",     "Losses in a row before cooldown triggers"))
    rows.append(var_row("COOLDOWN_SECONDS",        "300",    "900",   "Cooldown pause duration in seconds"))
    story.append(make_table(rows, cats))
    story.append(Spacer(1, 2*mm))

    # ── Section 4: VPIN ────────────────────────────────────────────────────────
    story.append(Paragraph("4. VPIN Signal", H2))

    rows = []; cats = []
    cats.append((len(rows), C_BLUE)); rows.append(cat_row("VPIN PARAMETERS", C_BLUE))
    rows.append(var_row("VPIN_BUCKET_SIZE_USD",    "50000",  "50000", "Dollar volume per VPIN bucket"))
    rows.append(var_row("VPIN_LOOKBACK_BUCKETS",   "50",     "50",    "Rolling window for VPIN calculation"))
    rows.append(var_row("VPIN_INFORMED_THRESHOLD", "0.55",   "0.55",  "VPIN level → 'informed' regime"))
    rows.append(var_row("VPIN_CASCADE_THRESHOLD",  "0.70",   "0.70",  "VPIN level → 'cascade' regime"))
    cats.append((len(rows), C_TEAL)); rows.append(cat_row("CASCADE DETECTOR", C_TEAL))
    rows.append(var_row("CASCADE_OI_DROP_THRESHOLD","0.02",  "0.02",  "OI drop % to signal cascade"))
    rows.append(var_row("CASCADE_LIQ_VOLUME_THRESHOLD","5e6","5000000","Liquidation volume USD threshold"))
    story.append(make_table(rows, cats))
    story.append(Spacer(1, 2*mm))

    # ── Section 5: 5-Min Strategy ──────────────────────────────────────────────
    story.append(Paragraph("5. 5-Minute Strategy", H2))

    rows = []; cats = []
    cats.append((len(rows), C_GREEN)); rows.append(cat_row("5-MINUTE POLYMARKET TRADING", C_GREEN))
    rows.append(var_row("FIVE_MIN_ENABLED",        "true",   "false", "Enable 5-min Polymarket strategy"))
    rows.append(var_row("FIVE_MIN_ASSETS",         "BTC",    "BTC",   "Comma-separated assets to trade"))
    rows.append(var_row("FIVE_MIN_MODE",           "safe",   "safe",  "Mode: flat / safe / degen"))
    rows.append(var_row("FIVE_MIN_ENTRY_OFFSET",   "60",     "10",    "Seconds before window close to enter"))
    rows.append(var_row("FIVE_MIN_MIN_DELTA_PCT",  "0.001",  "0.001", "Min price delta to trigger entry (skip below)"))
    rows.append(var_row("FIVE_MIN_MIN_CONFIDENCE", "0.30",   "0.30",  "Min model confidence to trade"))
    cats.append((len(rows), C_BLUE)); rows.append(cat_row("15-MINUTE STRATEGY", C_BLUE))
    rows.append(var_row("FIFTEEN_MIN_ENABLED",     "true",   "false", "Enable 15-min Polymarket strategy"))
    rows.append(var_row("FIFTEEN_MIN_ASSETS",      "BTC",    "BTC",   "Assets for 15-min strategy"))
    story.append(make_table(rows, cats))
    story.append(Spacer(1, 2*mm))

    # ── Section 6: Telegram ────────────────────────────────────────────────────
    story.append(Paragraph("6. Telegram Alerts", H2))

    rows = []; cats = []
    cats.append((len(rows), C_CYAN)); rows.append(cat_row("TELEGRAM", C_CYAN))
    rows.append(var_row("TELEGRAM_BOT_TOKEN",      "****",   "",      "Bot token from @BotFather"))
    rows.append(var_row("TELEGRAM_CHAT_ID",        "****",   "",      "Chat ID for alert delivery"))
    rows.append(var_row("TELEGRAM_ALERTS_LIVE",    "true",   "false", "Send alerts for live trades"))
    rows.append(var_row("TELEGRAM_ALERTS_PAPER",   "true",   "true",  "Send alerts for paper trades"))
    story.append(make_table(rows, cats))
    story.append(Spacer(1, 2*mm))

    # ── Section 7: External APIs ───────────────────────────────────────────────
    story.append(Paragraph("7. External APIs", H2))

    rows = []; cats = []
    cats.append((len(rows), C_GREY)); rows.append(cat_row("BINANCE (data only)", C_GREY))
    rows.append(var_row("BINANCE_API_KEY",         "****",   "",      "Binance API key (WebSocket auth)"))
    rows.append(var_row("BINANCE_API_SECRET",      "****",   "",      "Binance API secret"))
    cats.append((len(rows), C_ORANGE)); rows.append(cat_row("COINGLASS", C_ORANGE))
    rows.append(var_row("COINGLASS_API_KEY",       "****",   "",      "CoinGlass API key (OI/liq data)"))
    cats.append((len(rows), C_BLUE)); rows.append(cat_row("POLYGON RPC", C_BLUE))
    rows.append(var_row("POLYGON_RPC_URL",         "****",   "",      "Polygon RPC endpoint"))
    cats.append((len(rows), C_PURPLE)); rows.append(cat_row("OPINION EXCHANGE", C_PURPLE))
    rows.append(var_row("OPINION_API_KEY",         "****",   "",      "Opinion exchange API key"))
    rows.append(var_row("OPINION_WALLET_KEY",      "****",   "",      "Opinion wallet private key"))
    rows.append(var_row("OPINION_FEE_MULT",        "0.04",   "0.04",  "Fee multiplier for Opinion exchange"))
    rows.append(var_row("PREFERRED_VENUE",         "opinion","opinion","Execution venue: polymarket / opinion"))
    story.append(make_table(rows, cats))
    story.append(Spacer(1, 2*mm))

    # ── Section 8: Arbitrage ───────────────────────────────────────────────────
    story.append(Paragraph("8. Sub-$1 Arbitrage", H2))

    rows = []; cats = []
    cats.append((len(rows), C_YELLOW)); rows.append(cat_row("ARB STRATEGY", C_YELLOW))
    rows.append(var_row("ARB_MIN_SPREAD",          "0.015",  "0.015", "Minimum spread to trigger arb"))
    rows.append(var_row("ARB_MAX_POSITION",        "50.0",   "50.0",  "Max position size USD for arb"))
    rows.append(var_row("ARB_MAX_EXECUTION_MS",    "500",    "500",   "Max execution latency ms"))
    story.append(make_table(rows, cats))
    story.append(Spacer(1, 2*mm))

    # ── Section 9: RuntimeConfig DB Keys ──────────────────────────────────────
    story.append(Paragraph("9. RuntimeConfig — DB trading_configs Keys", H2))
    story.append(Paragraph(
        "The <font name='Courier'>trading_configs</font> table stores JSON configs that override env vars at runtime. "
        "The engine syncs every ~10s (heartbeat). Priority: DB &gt; env &gt; code defaults.",
        NOTE
    ))
    story.append(Spacer(1, 1*mm))

    DB_HDR = [
        Paragraph("<b>DB Key (JSON)</b>", ParagraphStyle("hd2", fontSize=7, fontName="Helvetica-Bold", textColor=C_WHITE)),
        Paragraph("<b>Engine Attribute</b>", ParagraphStyle("hd2", fontSize=7, fontName="Helvetica-Bold", textColor=C_WHITE)),
        Paragraph("<b>Type</b>", ParagraphStyle("hd2", fontSize=7, fontName="Helvetica-Bold", textColor=C_WHITE)),
        Paragraph("<b>Notes</b>", ParagraphStyle("hd2", fontSize=7, fontName="Helvetica-Bold", textColor=C_WHITE)),
    ]

    db_map = [
        ("starting_bankroll",       "starting_bankroll",          "float", "Initial bankroll; replaces STARTING_BANKROLL"),
        ("bet_fraction",            "bet_fraction",               "float", "Stake fraction; replaces BET_FRACTION"),
        ("max_position_usd",        "max_position_usd",           "float", "Hard position cap"),
        ("max_drawdown_pct",        "max_drawdown_kill",          "float", "Kill-switch drawdown threshold"),
        ("daily_loss_limit",        "daily_loss_limit_usd",       "float", "Absolute daily loss cap USD"),
        ("vpin_informed_threshold", "vpin_informed_threshold",    "float", "VPIN informed regime level"),
        ("vpin_cascade_threshold",  "vpin_cascade_threshold",     "float", "VPIN cascade regime level"),
        ("vpin_bucket_size_usd",    "vpin_bucket_size_usd",       "float", "Volume bucket size USD"),
        ("vpin_lookback_buckets",   "vpin_lookback_buckets",      "int",   "VPIN rolling window"),
        ("arb_min_spread",          "arb_min_spread",             "float", "Arb minimum spread"),
        ("arb_max_position",        "arb_max_position",           "float", "Arb max position USD"),
        ("arb_max_execution_ms",    "arb_max_execution_ms",       "int",   "Arb execution timeout ms"),
        ("enable_arb_strategy",     "arb_enabled",                "bool",  "Toggle arb strategy on/off"),
        ("cascade_cooldown_seconds","cooldown_seconds",           "int",   "Cooldown pause after streak"),
        ("cascade_min_liq_usd",     "cascade_liq_volume_threshold","float","Min liq volume for cascade"),
        ("enable_cascade_strategy", "cascade_enabled",            "bool",  "Toggle cascade strategy"),
        ("polymarket_fee_mult",     "polymarket_fee_mult",        "float", "Polymarket fee multiplier"),
        ("opinion_fee_mult",        "opinion_fee_mult",           "float", "Opinion fee multiplier"),
        ("preferred_venue",         "preferred_venue",            "str",   "Active execution venue"),
    ]

    s6 = ParagraphStyle("s6", fontSize=6.5, leading=8, fontName="Courier")
    s6d = ParagraphStyle("s6d", fontSize=6.5, leading=8, fontName="Helvetica")

    db_rows = [DB_HDR]
    for dk, attr, typ, note in db_map:
        db_rows.append([
            Paragraph(dk, s6),
            Paragraph(attr, s6),
            Paragraph(typ, s6),
            Paragraph(note, s6d),
        ])

    db_col_w = [W*0.28, W*0.28, W*0.08, W*0.36]
    dt = Table(db_rows, colWidths=db_col_w, repeatRows=1)
    dt.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,0), C_DARK),
        ("TEXTCOLOR",     (0,0), (-1,0), C_WHITE),
        ("FONTSIZE",      (0,0), (-1,0), 7),
        ("FONTSIZE",      (0,1), (-1,-1), 6.5),
        ("BOTTOMPADDING", (0,0), (-1,-1), 1),
        ("TOPPADDING",    (0,0), (-1,-1), 1),
        ("LEFTPADDING",   (0,0), (-1,-1), 3),
        ("RIGHTPADDING",  (0,0), (-1,-1), 2),
        ("ROWBACKGROUNDS",(0,1), (-1,-1), [colors.HexColor("#12121e"), colors.HexColor("#1a1a2e")]),
        ("TEXTCOLOR",     (0,1), (-1,-1), C_OFFWHITE),
        ("LINEBELOW",     (0,0), (-1,-1), 0.2, colors.HexColor("#2a2a3e")),
    ]))
    story.append(dt)
    story.append(Spacer(1, 2*mm))

    # ── Section 10: Risk Manager Logic ─────────────────────────────────────────
    story.append(Paragraph("10. Risk Manager — Approval Gates (in order)", H2))

    gate_data = [
        [Paragraph("<b>#</b>", s6), Paragraph("<b>Gate</b>", s6), Paragraph("<b>Condition</b>", s6d), Paragraph("<b>Bypass in paper?</b>", s6d)],
        ["1", "Kill Switch",      "drawdown ≥ MAX_DRAWDOWN_KILL (45%) OR manual",   "No — always blocks"],
        ["2", "Daily Loss Limit", "daily_pnl ≤ −(day_start × DAILY_LOSS_LIMIT_PCT)","Yes — skipped in paper"],
        ["3", "Position Limit",   "stake > bankroll × BET_FRACTION",                "No"],
        ["4", "Exposure Limit",   "open_exposure + stake > bankroll × MAX_OPEN_EXPOSURE_PCT", "No"],
        ["5", "Cooldown",         f"consecutive_losses ≥ CONSECUTIVE_LOSS_COOLDOWN → pause COOLDOWN_SECONDS", "Yes — skipped in paper"],
        ["6", "Venue Connectivity","both Polymarket + Opinion offline",              "No"],
    ]
    gate_rows_styled = [gate_data[0]]
    for row in gate_data[1:]:
        gate_rows_styled.append([
            Paragraph(row[0], s6),
            Paragraph(row[1], ParagraphStyle("gb", fontSize=6.5, fontName="Helvetica-Bold", textColor=C_ACCENT)),
            Paragraph(row[2], s6d),
            Paragraph(row[3], s6d),
        ])

    gt = Table(gate_rows_styled, colWidths=[W*0.04, W*0.15, W*0.52, W*0.29], repeatRows=1)
    gt.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,0), C_DARK),
        ("TEXTCOLOR",     (0,0), (-1,0), C_WHITE),
        ("FONTSIZE",      (0,0), (-1,0), 7),
        ("FONTSIZE",      (0,1), (-1,-1), 6.5),
        ("BOTTOMPADDING", (0,0), (-1,-1), 1),
        ("TOPPADDING",    (0,0), (-1,-1), 1),
        ("LEFTPADDING",   (0,0), (-1,-1), 3),
        ("RIGHTPADDING",  (0,0), (-1,-1), 2),
        ("ROWBACKGROUNDS",(0,1), (-1,-1), [colors.HexColor("#12121e"), colors.HexColor("#1a1a2e")]),
        ("TEXTCOLOR",     (0,1), (-1,-1), C_OFFWHITE),
        ("LINEBELOW",     (0,0), (-1,-1), 0.2, colors.HexColor("#2a2a3e")),
    ]))
    story.append(gt)
    story.append(Spacer(1, 2*mm))

    # ── Footer note ────────────────────────────────────────────────────────────
    story.append(Paragraph(
        "Generated by Novakash agent · Source: engine/config/settings.py · constants.py · "
        "runtime_config.py · execution/risk_manager.py · Production: Railway 2026-04-02",
        NOTE
    ))

    doc.build(story)
    print(f"✓ Written: {OUTPUT}")


if __name__ == "__main__":
    build()
