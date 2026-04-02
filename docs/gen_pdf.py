"""
Generate Engine Config Reference PDF with architecture diagram.
Redesigned: better spacing, legible tables, consistent layout.
"""

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether
)
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT
from reportlab.platypus.flowables import Flowable
import os

OUTPUT = "/root/.openclaw/workspace-novakash/novakash/docs/engine-config-reference.pdf"

# ── Colours ───────────────────────────────────────────────────────────────────
C_BG      = colors.HexColor("#0d0d16")
C_CARD    = colors.HexColor("#141422")
C_ROW_A   = colors.HexColor("#161626")
C_ROW_B   = colors.HexColor("#1c1c30")
C_BORDER  = colors.HexColor("#2a2a42")
C_TEXT    = colors.HexColor("#d4d4e8")
C_DIM     = colors.HexColor("#8888a8")
C_WHITE   = colors.white
C_ACCENT  = colors.HexColor("#a855f7")  # Purple accent
C_RED     = colors.HexColor("#f87171")
C_GREEN   = colors.HexColor("#4ade80")
C_CYAN    = colors.HexColor("#22d3ee")
C_ORANGE  = colors.HexColor("#fb923c")
C_TEAL    = colors.HexColor("#2dd4bf")
C_BLUE    = colors.HexColor("#60a5fa")
C_YELLOW  = colors.HexColor("#facc15")
C_PINK    = colors.HexColor("#f472b6")

# Category header colours (muted, not eye-scorching)
CAT_RISK    = colors.HexColor("#3b1a0a")
CAT_VPIN    = colors.HexColor("#1a2a3b")
CAT_ARB     = colors.HexColor("#2a1a3b")
CAT_CASCADE = colors.HexColor("#0a2a2a")
CAT_CREDS   = colors.HexColor("#1a1a3b")
CAT_FEED    = colors.HexColor("#1a2a1a")
CAT_GENERAL = colors.HexColor("#222233")
CAT_TELEGRAM = colors.HexColor("#1a2a2a")


# ── Architecture Diagram ─────────────────────────────────────────────────────
class ArchDiagram(Flowable):
    def __init__(self, width, height):
        Flowable.__init__(self)
        self.width = width
        self.height = height

    def draw(self):
        c = self.canv
        w, h = self.width, self.height

        # Background
        c.setFillColor(C_BG)
        c.roundRect(0, 0, w, h, 8, fill=1, stroke=0)

        # Inner border
        c.setStrokeColor(C_BORDER)
        c.setLineWidth(0.5)
        c.roundRect(2, 2, w - 4, h - 4, 7, fill=0, stroke=1)

        # Title
        c.setFillColor(C_ACCENT)
        c.setFont("Helvetica-Bold", 10)
        c.drawCentredString(w / 2, h - 18, "NOVAKASH ENGINE — ARCHITECTURE")

        # ── Pipeline boxes ─────────────────────────────────────────────
        BOX_W = 130
        BOX_H = 22
        cx = w / 2
        step_y = 28
        top_y = h - 34

        pipeline = [
            ("Binance WebSocket",    "live BTC price feed",        colors.HexColor("#1b4d4a")),
            ("VPIN Calculator",      "volume-sync'd PIN signal",   colors.HexColor("#0f3460")),
            ("5-Min Strategy",       "delta + VPIN analysis",      colors.HexColor("#0f3460")),
            ("Risk Manager",         "bet size · exposure · kill", colors.HexColor("#7a3a0a")),
            ("Polymarket CLOB",      "place order via API",        colors.HexColor("#4a1a5e")),
            ("Order Manager",        "track fills · resolve",      colors.HexColor("#16213e")),
            ("Telegram Alerter",     "notify Billy",               colors.HexColor("#1a3a2a")),
            ("PostgreSQL DB",        "persist trades",             colors.HexColor("#2a2a3a")),
        ]

        box_xs = cx - BOX_W / 2
        box_positions = []

        for i, (label, sub, col) in enumerate(pipeline):
            by = top_y - i * step_y - BOX_H
            box_positions.append((box_xs, by, BOX_W, BOX_H))

            # Box with subtle border
            c.setFillColor(col)
            c.roundRect(box_xs, by, BOX_W, BOX_H, 4, fill=1, stroke=0)
            c.setStrokeColor(colors.HexColor("#ffffff18"))
            c.setLineWidth(0.3)
            c.roundRect(box_xs, by, BOX_W, BOX_H, 4, fill=0, stroke=1)

            # Label
            c.setFillColor(C_WHITE)
            c.setFont("Helvetica-Bold", 8)
            c.drawString(box_xs + 6, by + BOX_H - 10, label)

            # Sub-label
            c.setFillColor(C_DIM)
            c.setFont("Helvetica", 6)
            c.drawString(box_xs + 6, by + 4, sub)

            # Arrow down
            if i < len(pipeline) - 1:
                ax = cx
                ay = by
                c.setStrokeColor(C_ACCENT)
                c.setLineWidth(1.2)
                c.line(ax, ay, ax, ay - (step_y - BOX_H))
                arrowY = ay - (step_y - BOX_H)
                c.setFillColor(C_ACCENT)
                p = c.beginPath()
                p.moveTo(ax - 3, arrowY + 5)
                p.lineTo(ax + 3, arrowY + 5)
                p.lineTo(ax, arrowY)
                p.close()
                c.drawPath(p, fill=1, stroke=0)

        # ── Side boxes ─────────────────────────────────────────────────
        def side_box(label, sub, bx, by, bw, bh, col):
            c.setFillColor(col)
            c.roundRect(bx, by, bw, bh, 4, fill=1, stroke=0)
            c.setFillColor(C_WHITE)
            c.setFont("Helvetica-Bold", 7)
            c.drawString(bx + 5, by + bh - 9, label)
            c.setFillColor(C_DIM)
            c.setFont("Helvetica", 5.5)
            c.drawString(bx + 5, by + 3, sub)

        def arrow_h(x1, y1, x2, y2, col):
            c.setStrokeColor(col)
            c.setLineWidth(0.8)
            c.line(x1, y1, x2, y2)
            c.setFillColor(col)
            d = 1 if x2 > x1 else -1
            p = c.beginPath()
            p.moveTo(x2 - d * 4, y2 + 2)
            p.moveTo(x2 - d * 4, y2 - 2)
            p.lineTo(x2, y2)
            p.close()
            c.drawPath(p, fill=1, stroke=0)

        # Regime Classifier (right of VPIN)
        rc_x = box_xs + BOX_W + 14
        rc_y = box_positions[1][1]
        side_box("Regime Classifier", "informed / cascade / calm", rc_x, rc_y, 90, BOX_H, colors.HexColor("#0f3460"))
        arrow_h(box_xs + BOX_W, rc_y + BOX_H / 2, rc_x, rc_y + BOX_H / 2, C_CYAN)

        # Gamma API (left of Polymarket)
        gamma_x = box_xs - 100
        gamma_y = box_positions[4][1]
        side_box("Gamma API", "market data + token prices", gamma_x, gamma_y, 90, BOX_H, colors.HexColor("#4a1a5e"))
        arrow_h(gamma_x + 90, gamma_y + BOX_H / 2, box_xs, gamma_y + BOX_H / 2, C_PINK)

        # Feedback labels (right side)
        def feedback_label(label, sub, y, col):
            lx = rc_x + 96
            bw = 80
            bh = 20
            c.setFillColor(col)
            c.roundRect(lx, y - bh / 2, bw, bh, 3, fill=1, stroke=0)
            c.setFillColor(C_WHITE)
            c.setFont("Helvetica-Bold", 6)
            c.drawString(lx + 4, y + 2, label)
            c.setFillColor(C_DIM)
            c.setFont("Helvetica", 5)
            c.drawString(lx + 4, y - 6, sub)

        hb_y = (box_positions[3][1] + box_positions[4][1]) / 2 + BOX_H / 2
        feedback_label("Heartbeat (10s)", "wallet bal → bankroll", hb_y, colors.HexColor("#1a3a2a"))

        rc_fb_y = (box_positions[5][1] + box_positions[7][1]) / 2 + BOX_H / 2
        feedback_label("RuntimeConfig", "DB → hot reload", rc_fb_y, colors.HexColor("#3a2a0a"))

        # ── Legend ─────────────────────────────────────────────────────
        legend_y = 8
        c.setFont("Helvetica", 6)
        items = [
            (C_ACCENT, "Main flow"),
            (C_CYAN, "Signal branch"),
            (C_GREEN, "Heartbeat"),
            (C_ORANGE, "Config sync"),
            (C_PINK, "External API"),
        ]
        lx = 12
        for col, txt in items:
            c.setFillColor(col)
            c.rect(lx, legend_y, 10, 6, fill=1, stroke=0)
            c.setFillColor(C_DIM)
            c.drawString(lx + 13, legend_y + 1, txt)
            lx += 80


# ── Build PDF ─────────────────────────────────────────────────────────────────
def build():
    doc = SimpleDocTemplate(
        OUTPUT,
        pagesize=A4,
        leftMargin=12 * mm, rightMargin=12 * mm,
        topMargin=12 * mm, bottomMargin=12 * mm,
    )

    W = A4[0] - 24 * mm  # usable width

    # ── Styles ────────────────────────────────────────────────────────────────
    H1 = ParagraphStyle("h1", fontSize=14, leading=17, fontName="Helvetica-Bold",
                        textColor=C_ACCENT, spaceBefore=0, spaceAfter=2)
    H2 = ParagraphStyle("h2", fontSize=10, leading=13, fontName="Helvetica-Bold",
                        textColor=C_TEXT, spaceBefore=6, spaceAfter=3)
    NOTE = ParagraphStyle("note", fontSize=7.5, leading=10, fontName="Helvetica-Oblique",
                          textColor=C_DIM, spaceAfter=2)
    MONO_SM = ParagraphStyle("mono_sm", fontSize=7, leading=9.5, fontName="Courier",
                             textColor=C_TEXT)
    DESC_SM = ParagraphStyle("desc_sm", fontSize=7, leading=9.5, fontName="Helvetica",
                             textColor=C_DIM)
    HDR_SM = ParagraphStyle("hdr_sm", fontSize=7.5, leading=10, fontName="Helvetica-Bold",
                            textColor=C_WHITE)
    CAT_LABEL = ParagraphStyle("cat_label", fontSize=7.5, leading=10, fontName="Helvetica-Bold",
                               textColor=C_WHITE)
    GATE_NAME = ParagraphStyle("gate_name", fontSize=7, leading=9.5, fontName="Helvetica-Bold",
                               textColor=C_ACCENT)

    # ── Table helpers ─────────────────────────────────────────────────────────
    COL_W = [W * 0.28, W * 0.12, W * 0.12, W * 0.48]

    def hdr_row():
        return [Paragraph("<b>Variable</b>", HDR_SM),
                Paragraph("<b>Prod</b>", HDR_SM),
                Paragraph("<b>Default</b>", HDR_SM),
                Paragraph("<b>Description</b>", HDR_SM)]

    def cat_row(label):
        return [Paragraph(f"<b>{label}</b>", CAT_LABEL), "", "", ""]

    def var_row(var, val, default, desc):
        return [Paragraph(var, MONO_SM),
                Paragraph(str(val), MONO_SM),
                Paragraph(str(default), MONO_SM),
                Paragraph(desc, DESC_SM)]

    def make_table(rows, cat_indices):
        all_rows = [hdr_row()] + rows
        t = Table(all_rows, colWidths=COL_W, repeatRows=1)

        style_cmds = [
            # Header row
            ("BACKGROUND",    (0, 0), (-1, 0), C_BG),
            ("TEXTCOLOR",     (0, 0), (-1, 0), C_WHITE),
            ("FONTSIZE",      (0, 0), (-1, 0), 7.5),
            ("TOPPADDING",    (0, 0), (-1, 0), 4),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 4),
            # Data rows
            ("FONTSIZE",      (0, 1), (-1, -1), 7),
            ("TOPPADDING",    (0, 1), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 1), (-1, -1), 3),
            ("LEFTPADDING",   (0, 0), (-1, -1), 5),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
            # Alternating row bg
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [C_ROW_A, C_ROW_B]),
            ("TEXTCOLOR",     (0, 1), (-1, -1), C_TEXT),
            # Subtle grid lines
            ("LINEBELOW",     (0, 0), (-1, 0), 0.5, C_ACCENT),
            ("LINEBELOW",     (0, 1), (-1, -2), 0.15, C_BORDER),
            ("LINEBELOW",     (0, -1), (-1, -1), 0.5, C_BORDER),
            # Vertical column separators
            ("LINEAFTER",     (0, 0), (0, -1), 0.15, C_BORDER),
            ("LINEAFTER",     (1, 0), (1, -1), 0.15, C_BORDER),
            ("LINEAFTER",     (2, 0), (2, -1), 0.15, C_BORDER),
            # Outer box
            ("BOX",           (0, 0), (-1, -1), 0.5, C_BORDER),
        ]

        # Category header rows
        for row_idx, col in cat_indices:
            actual = row_idx + 1  # +1 for header
            style_cmds += [
                ("BACKGROUND",    (0, actual), (-1, actual), col),
                ("TEXTCOLOR",     (0, actual), (-1, actual), C_WHITE),
                ("SPAN",          (0, actual), (-1, actual)),
                ("FONTSIZE",      (0, actual), (-1, actual), 7.5),
                ("TOPPADDING",    (0, actual), (-1, actual), 3),
                ("BOTTOMPADDING", (0, actual), (-1, actual), 3),
            ]

        t.setStyle(TableStyle(style_cmds))
        return t

    # ── Story ─────────────────────────────────────────────────────────────────
    story = []

    # Title
    story.append(Paragraph("⚙ Novakash Engine — Config Reference", H1))
    story.append(Paragraph(
        "Auto-generated from source. Production values reflect Railway deployment as of 2026-04-02. "
        "Priority: DB trading_configs &gt; env vars &gt; code defaults.", NOTE))
    story.append(Spacer(1, 3 * mm))

    # Architecture diagram (reduced height to fit page 1 better)
    story.append(ArchDiagram(W, 210))
    story.append(Spacer(1, 4 * mm))

    # ── 1. Core / Mode ────────────────────────────────────────────────────────
    story.append(Paragraph("1. Core Trading Mode", H2))
    rows, cats = [], []
    cats.append((len(rows), CAT_GENERAL)); rows.append(cat_row("CORE / MODE"))
    rows.append(var_row("PAPER_MODE",           "false",  "true",       "Disable paper trading (real orders)"))
    rows.append(var_row("LIVE_TRADING_ENABLED",  "true",  "false",      "Master live-trading gate"))
    rows.append(var_row("STARTING_BANKROLL",     "208",   "500.0",      "Initial bankroll USD"))
    cats.append((len(rows), CAT_FEED)); rows.append(cat_row("DATABASE"))
    rows.append(var_row("DATABASE_URL",          "****",  "(required)", "Async PostgreSQL DSN (asyncpg)"))
    story.append(make_table(rows, cats))
    story.append(Spacer(1, 3 * mm))

    # ── 2. Polymarket ─────────────────────────────────────────────────────────
    story.append(Paragraph("2. Polymarket / Execution", H2))
    rows, cats = [], []
    cats.append((len(rows), CAT_CREDS)); rows.append(cat_row("POLYMARKET CREDENTIALS"))
    rows.append(var_row("POLY_PRIVATE_KEY",      "****",  "",           "Ethereum private key (EIP-712 signer)"))
    rows.append(var_row("POLY_API_KEY",          "****",  "",           "CLOB API key"))
    rows.append(var_row("POLY_API_SECRET",       "****",  "",           "CLOB API secret"))
    rows.append(var_row("POLY_API_PASSPHRASE",   "****",  "",           "CLOB API passphrase"))
    rows.append(var_row("POLY_FUNDER_ADDRESS",   "0x330e…0b6b", "",    "Polymarket funder wallet address"))
    rows.append(var_row("POLY_SIGNATURE_TYPE",   "1",     "0",          "Sig type: 0=EOA, 1=contract wallet"))
    cats.append((len(rows), CAT_VPIN)); rows.append(cat_row("POLYMARKET SETTINGS"))
    rows.append(var_row("POLY_BTC_TOKEN_IDS",    "(set)", "",           "Comma-separated token IDs to watch"))
    rows.append(var_row("POLY_WINDOW_SECONDS",   "300",   "300",        "Resolution window seconds"))
    rows.append(var_row("POLYMARKET_FEE_MULT",   "0.072", "0.072",      "Fee multiplier for Polymarket crypto"))
    story.append(make_table(rows, cats))
    story.append(Spacer(1, 3 * mm))

    # ── 3. Risk Manager ───────────────────────────────────────────────────────
    story.append(Paragraph("3. Risk Manager", H2))
    rows, cats = [], []
    cats.append((len(rows), CAT_RISK)); rows.append(cat_row("RISK GATES"))
    rows.append(var_row("BET_FRACTION",          "0.10",  "0.025",      "Stake as fraction of bankroll per trade"))
    rows.append(var_row("MIN_BET_USD",           "2.0",   "2.0",        "Minimum bet size USD"))
    rows.append(var_row("MAX_POSITION_USD",      "(set)", "500.0",      "Hard cap on single position USD"))
    rows.append(var_row("MAX_OPEN_EXPOSURE_PCT", "0.45",  "0.30",       "Max total open positions / bankroll"))
    rows.append(var_row("MAX_DRAWDOWN_KILL",     "0.45",  "0.45",       "Kill switch: drawdown from peak"))
    rows.append(var_row("DAILY_LOSS_LIMIT_PCT",  "0.30",  "0.10",       "Max daily loss as % of day-start balance"))
    rows.append(var_row("DAILY_LOSS_LIMIT_USD",  "(derived)", "50.0",   "Absolute daily loss cap (fallback)"))
    cats.append((len(rows), CAT_GENERAL)); rows.append(cat_row("COOLDOWN / STREAK"))
    rows.append(var_row("CONSECUTIVE_LOSS_COOLDOWN", "3",  "3",         "Losses in a row before cooldown triggers"))
    rows.append(var_row("COOLDOWN_SECONDS",      "300",   "900",        "Cooldown pause duration in seconds"))
    story.append(make_table(rows, cats))
    story.append(Spacer(1, 3 * mm))

    # ── 4. VPIN ───────────────────────────────────────────────────────────────
    story.append(Paragraph("4. VPIN Signal", H2))
    rows, cats = [], []
    cats.append((len(rows), CAT_VPIN)); rows.append(cat_row("VPIN PARAMETERS"))
    rows.append(var_row("VPIN_BUCKET_SIZE_USD",  "50,000", "50,000",    "Dollar volume per VPIN bucket"))
    rows.append(var_row("VPIN_LOOKBACK_BUCKETS", "50",     "50",        "Rolling window for VPIN calculation"))
    rows.append(var_row("VPIN_INFORMED_THRESHOLD","0.55",  "0.55",      "VPIN level → informed regime"))
    rows.append(var_row("VPIN_CASCADE_THRESHOLD","0.70",   "0.70",      "VPIN level → cascade regime"))
    cats.append((len(rows), CAT_CASCADE)); rows.append(cat_row("CASCADE DETECTOR"))
    rows.append(var_row("CASCADE_OI_DROP_THRESHOLD","0.02","0.02",      "OI drop % to signal cascade"))
    rows.append(var_row("CASCADE_LIQ_VOLUME_THRESHOLD","5,000,000","5,000,000","Liquidation volume USD threshold"))
    story.append(make_table(rows, cats))
    story.append(Spacer(1, 3 * mm))

    # ── 5. 5-Min Strategy ─────────────────────────────────────────────────────
    story.append(Paragraph("5. 5-Minute Strategy", H2))
    rows, cats = [], []
    cats.append((len(rows), CAT_FEED)); rows.append(cat_row("5-MINUTE POLYMARKET TRADING"))
    rows.append(var_row("FIVE_MIN_ENABLED",      "true",  "false",      "Enable 5-min Polymarket strategy"))
    rows.append(var_row("FIVE_MIN_ASSETS",       "BTC",   "BTC",        "Comma-separated assets to trade"))
    rows.append(var_row("FIVE_MIN_MODE",         "safe",  "safe",       "Mode: flat / safe / degen"))
    rows.append(var_row("FIVE_MIN_ENTRY_OFFSET", "60",    "10",         "Seconds before window close to enter"))
    rows.append(var_row("FIVE_MIN_MIN_DELTA_PCT","0.001", "0.001",      "Min price delta to trigger entry"))
    rows.append(var_row("FIVE_MIN_MIN_CONFIDENCE","0.30", "0.30",       "Min model confidence to trade"))
    cats.append((len(rows), CAT_VPIN)); rows.append(cat_row("15-MINUTE STRATEGY"))
    rows.append(var_row("FIFTEEN_MIN_ENABLED",   "true",  "false",      "Enable 15-min Polymarket strategy"))
    rows.append(var_row("FIFTEEN_MIN_ASSETS",    "BTC",   "BTC",        "Assets for 15-min strategy"))
    story.append(make_table(rows, cats))
    story.append(Spacer(1, 3 * mm))

    # ── 6. Telegram ───────────────────────────────────────────────────────────
    story.append(Paragraph("6. Telegram Alerts", H2))
    rows, cats = [], []
    cats.append((len(rows), CAT_TELEGRAM)); rows.append(cat_row("TELEGRAM"))
    rows.append(var_row("TELEGRAM_BOT_TOKEN",    "****",  "",            "Bot token from @BotFather"))
    rows.append(var_row("TELEGRAM_CHAT_ID",      "****",  "",            "Chat ID for alert delivery"))
    rows.append(var_row("TELEGRAM_ALERTS_LIVE",  "true",  "false",       "Send alerts for live trades"))
    rows.append(var_row("TELEGRAM_ALERTS_PAPER", "true",  "true",        "Send alerts for paper trades"))
    story.append(make_table(rows, cats))
    story.append(Spacer(1, 3 * mm))

    # ── 7. External APIs ──────────────────────────────────────────────────────
    story.append(Paragraph("7. External APIs", H2))
    rows, cats = [], []
    cats.append((len(rows), CAT_GENERAL)); rows.append(cat_row("BINANCE (data only)"))
    rows.append(var_row("BINANCE_API_KEY",       "****",  "",            "Binance API key (WebSocket auth)"))
    rows.append(var_row("BINANCE_API_SECRET",    "****",  "",            "Binance API secret"))
    cats.append((len(rows), CAT_RISK)); rows.append(cat_row("COINGLASS"))
    rows.append(var_row("COINGLASS_API_KEY",     "****",  "",            "CoinGlass API key (OI / liq data)"))
    cats.append((len(rows), CAT_VPIN)); rows.append(cat_row("POLYGON RPC"))
    rows.append(var_row("POLYGON_RPC_URL",       "****",  "",            "Polygon RPC endpoint"))
    cats.append((len(rows), CAT_CREDS)); rows.append(cat_row("OPINION EXCHANGE"))
    rows.append(var_row("OPINION_API_KEY",       "****",  "",            "Opinion exchange API key"))
    rows.append(var_row("OPINION_WALLET_KEY",    "****",  "",            "Opinion wallet private key"))
    rows.append(var_row("OPINION_FEE_MULT",      "0.04",  "0.04",       "Fee multiplier for Opinion exchange"))
    rows.append(var_row("PREFERRED_VENUE",       "opinion","opinion",    "Execution venue: polymarket / opinion"))
    story.append(make_table(rows, cats))
    story.append(Spacer(1, 3 * mm))

    # ── 8. Arb ────────────────────────────────────────────────────────────────
    story.append(Paragraph("8. Sub-$1 Arbitrage", H2))
    rows, cats = [], []
    cats.append((len(rows), CAT_ARB)); rows.append(cat_row("ARB STRATEGY"))
    rows.append(var_row("ARB_MIN_SPREAD",        "0.015", "0.015",      "Minimum spread to trigger arb"))
    rows.append(var_row("ARB_MAX_POSITION",      "50.0",  "50.0",       "Max position size USD for arb"))
    rows.append(var_row("ARB_MAX_EXECUTION_MS",  "500",   "500",        "Max execution latency ms"))
    story.append(make_table(rows, cats))
    story.append(Spacer(1, 3 * mm))

    # ── 9. RuntimeConfig DB Keys ──────────────────────────────────────────────
    story.append(Paragraph("9. RuntimeConfig — DB trading_configs Keys", H2))
    story.append(Paragraph(
        "The <font name='Courier'>trading_configs</font> table stores JSON configs that override env vars at runtime. "
        "The engine syncs every ~10s (heartbeat). Priority: DB &gt; env &gt; code defaults.", NOTE))
    story.append(Spacer(1, 2 * mm))

    DB_HDR = [Paragraph("<b>DB Key (JSON)</b>", HDR_SM),
              Paragraph("<b>Engine Attribute</b>", HDR_SM),
              Paragraph("<b>Type</b>", HDR_SM),
              Paragraph("<b>Notes</b>", HDR_SM)]

    db_col_w = [W * 0.26, W * 0.26, W * 0.07, W * 0.41]

    db_map = [
        ("starting_bankroll",       "starting_bankroll",           "float", "Initial bankroll; replaces STARTING_BANKROLL"),
        ("bet_fraction",            "bet_fraction",                "float", "Stake fraction; replaces BET_FRACTION"),
        ("max_position_usd",        "max_position_usd",            "float", "Hard position cap"),
        ("max_drawdown_pct",        "max_drawdown_kill",           "float", "Kill-switch drawdown threshold"),
        ("daily_loss_limit",        "daily_loss_limit_usd",        "float", "Absolute daily loss cap USD"),
        ("vpin_informed_threshold", "vpin_informed_threshold",     "float", "VPIN informed regime level"),
        ("vpin_cascade_threshold",  "vpin_cascade_threshold",      "float", "VPIN cascade regime level"),
        ("vpin_bucket_size_usd",    "vpin_bucket_size_usd",        "float", "Volume bucket size USD"),
        ("vpin_lookback_buckets",   "vpin_lookback_buckets",       "int",   "VPIN rolling window"),
        ("arb_min_spread",          "arb_min_spread",              "float", "Arb minimum spread"),
        ("arb_max_position",        "arb_max_position",            "float", "Arb max position USD"),
        ("arb_max_execution_ms",    "arb_max_execution_ms",        "int",   "Arb execution timeout ms"),
        ("enable_arb_strategy",     "arb_enabled",                 "bool",  "Toggle arb strategy on/off"),
        ("cascade_cooldown_seconds","cooldown_seconds",            "int",   "Cooldown pause after streak"),
        ("cascade_min_liq_usd",     "cascade_liq_volume_threshold","float", "Min liq volume for cascade"),
        ("enable_cascade_strategy", "cascade_enabled",             "bool",  "Toggle cascade strategy"),
        ("polymarket_fee_mult",     "polymarket_fee_mult",         "float", "Polymarket fee multiplier"),
        ("opinion_fee_mult",        "opinion_fee_mult",            "float", "Opinion fee multiplier"),
        ("preferred_venue",         "preferred_venue",             "str",   "Active execution venue"),
    ]

    db_rows = [DB_HDR]
    for dk, attr, typ, note in db_map:
        db_rows.append([Paragraph(dk, MONO_SM), Paragraph(attr, MONO_SM),
                        Paragraph(typ, MONO_SM), Paragraph(note, DESC_SM)])

    dt = Table(db_rows, colWidths=db_col_w, repeatRows=1)
    dt.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), C_BG),
        ("TEXTCOLOR",     (0, 0), (-1, 0), C_WHITE),
        ("FONTSIZE",      (0, 0), (-1, 0), 7.5),
        ("TOPPADDING",    (0, 0), (-1, 0), 4),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 4),
        ("FONTSIZE",      (0, 1), (-1, -1), 7),
        ("TOPPADDING",    (0, 1), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 3),
        ("LEFTPADDING",   (0, 0), (-1, -1), 5),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [C_ROW_A, C_ROW_B]),
        ("TEXTCOLOR",     (0, 1), (-1, -1), C_TEXT),
        ("LINEBELOW",     (0, 0), (-1, 0), 0.5, C_ACCENT),
        ("LINEBELOW",     (0, 1), (-1, -2), 0.15, C_BORDER),
        ("LINEBELOW",     (0, -1), (-1, -1), 0.5, C_BORDER),
        ("LINEAFTER",     (0, 0), (0, -1), 0.15, C_BORDER),
        ("LINEAFTER",     (1, 0), (1, -1), 0.15, C_BORDER),
        ("LINEAFTER",     (2, 0), (2, -1), 0.15, C_BORDER),
        ("BOX",           (0, 0), (-1, -1), 0.5, C_BORDER),
    ]))
    story.append(dt)
    story.append(Spacer(1, 4 * mm))

    # ── 10. Risk Manager Gates ────────────────────────────────────────────────
    story.append(Paragraph("10. Risk Manager — Approval Gates (in order)", H2))

    gate_col_w = [W * 0.05, W * 0.16, W * 0.52, W * 0.27]
    gate_hdr = [Paragraph("<b>#</b>", HDR_SM), Paragraph("<b>Gate</b>", HDR_SM),
                Paragraph("<b>Condition</b>", HDR_SM), Paragraph("<b>Bypass in paper?</b>", HDR_SM)]

    gate_data = [
        ("1", "Kill Switch",       "drawdown ≥ MAX_DRAWDOWN_KILL (45%) OR manual",                 "No — always blocks"),
        ("2", "Daily Loss Limit",  "daily_pnl ≤ −(day_start × DAILY_LOSS_LIMIT_PCT)",              "Yes — skipped in paper"),
        ("3", "Position Limit",    "stake > bankroll × BET_FRACTION",                               "No"),
        ("4", "Exposure Limit",    "open_exposure + stake > bankroll × MAX_OPEN_EXPOSURE_PCT",       "No"),
        ("5", "Cooldown",          "consecutive_losses ≥ CONSECUTIVE_LOSS_COOLDOWN → pause",         "Yes — skipped in paper"),
        ("6", "Venue Connectivity","both Polymarket + Opinion offline",                              "No"),
    ]

    gate_rows = [gate_hdr]
    for num, name, cond, bypass in gate_data:
        gate_rows.append([
            Paragraph(num, MONO_SM),
            Paragraph(name, GATE_NAME),
            Paragraph(cond, DESC_SM),
            Paragraph(bypass, DESC_SM),
        ])

    gt = Table(gate_rows, colWidths=gate_col_w, repeatRows=1)
    gt.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), C_BG),
        ("TEXTCOLOR",     (0, 0), (-1, 0), C_WHITE),
        ("FONTSIZE",      (0, 0), (-1, 0), 7.5),
        ("TOPPADDING",    (0, 0), (-1, 0), 4),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 4),
        ("FONTSIZE",      (0, 1), (-1, -1), 7),
        ("TOPPADDING",    (0, 1), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 3),
        ("LEFTPADDING",   (0, 0), (-1, -1), 5),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [C_ROW_A, C_ROW_B]),
        ("TEXTCOLOR",     (0, 1), (-1, -1), C_TEXT),
        ("LINEBELOW",     (0, 0), (-1, 0), 0.5, C_ACCENT),
        ("LINEBELOW",     (0, 1), (-1, -2), 0.15, C_BORDER),
        ("LINEBELOW",     (0, -1), (-1, -1), 0.5, C_BORDER),
        ("LINEAFTER",     (0, 0), (0, -1), 0.15, C_BORDER),
        ("LINEAFTER",     (1, 0), (1, -1), 0.15, C_BORDER),
        ("LINEAFTER",     (2, 0), (2, -1), 0.15, C_BORDER),
        ("BOX",           (0, 0), (-1, -1), 0.5, C_BORDER),
    ]))
    story.append(gt)
    story.append(Spacer(1, 4 * mm))

    # ── Footer ────────────────────────────────────────────────────────────────
    story.append(Paragraph(
        "Generated by Novakash agent · Source: engine/config/settings.py · constants.py · "
        "runtime_config.py · execution/risk_manager.py · Production: Railway 2026-04-02", NOTE))

    doc.build(story)
    print(f"✓ Written: {OUTPUT}")


if __name__ == "__main__":
    build()
