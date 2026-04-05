"""
Window Chart Generator — dark-theme PNG for Telegram window snapshots.

Generates a clean 2-panel chart:
  Left: BTC price line with entry markers at T-240/T-180/T-120/T-90/T-60
  Right: Signal confidence bars (VPIN, TWAP, TimesFM, Gamma, Delta, CG)
  Bottom: What-if P&L table (separate from chart, no overlap)
  Footer: window ID, location tag, version

Returns PNG bytes. All rendering in-memory, no file I/O.
"""

from __future__ import annotations

import io
from typing import Optional


# ── Design tokens ────────────────────────────────────────────────────────────
BG = '#07070c'
CARD = '#0d0d18'
BORDER = '#1a1a2e'
TEXT = '#e2e8f0'
MUTED = '#64748b'
DIM = '#334155'
GREEN = '#4ade80'
RED = '#f87171'
AMBER = '#fbbf24'
CYAN = '#22d3ee'
PURPLE = '#a855f7'
WHITE = '#ffffff'

ENTRY_COLORS = {'T-240': AMBER, 'T-180': CYAN, 'T-120': PURPLE, 'T-90': '#f97316', 'T-60': WHITE}


def window_snapshot_chart(
    price_ticks: list[float],          # BTC prices since window open (1/sec or sparse)
    open_price: float,
    current_price: float,
    window_id: str,                    # e.g. "BTC-1775411600"
    t_label: str,                      # e.g. "T-240s"
    elapsed_s: int,                    # seconds elapsed in window
    # Signals
    vpin: float = 0.0,
    vpin_regime: str = "NORMAL",
    twap_direction: Optional[str] = None,
    twap_agreement: int = 0,
    timesfm_direction: Optional[str] = None,
    timesfm_confidence: float = 0.0,
    timesfm_predicted: float = 0.0,
    gamma_up: float = 0.50,
    gamma_down: float = 0.50,
    delta_pct: float = 0.0,
    cg_taker_buy_pct: float = 50.0,   # 0-100
    cg_funding_annual: float = 0.0,
    # What-if entries at each T-point
    entry_prices: Optional[dict] = None,  # {"T-240": 0.48, "T-180": 0.51, ...}
    stake_usd: float = 4.0,
    win_rate: float = 0.99,
    # Meta
    location: str = "MTL",
    engine_version: str = "v7.1",
) -> bytes:
    """Generate window snapshot chart. Returns PNG bytes."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        import matplotlib.gridspec as gridspec
        import numpy as np

        # ── Figure layout ─────────────────────────────────────────────────
        # Row 0 (tall): price chart | signal panel
        # Row 1 (short): P&L table spanning full width
        fig = plt.figure(figsize=(11, 6.5), facecolor=BG)
        gs = gridspec.GridSpec(
            2, 2,
            figure=fig,
            height_ratios=[3.2, 1.0],
            width_ratios=[1.6, 1.0],
            hspace=0.22,
            wspace=0.10,
            left=0.04, right=0.98, top=0.93, bottom=0.04,
        )

        ax_price = fig.add_subplot(gs[0, 0])
        ax_sig = fig.add_subplot(gs[0, 1])
        ax_table = fig.add_subplot(gs[1, :])

        for ax in (ax_price, ax_sig, ax_table):
            ax.set_facecolor(CARD)
            for s in ax.spines.values():
                s.set_edgecolor(BORDER)

        ax_table.tick_params(left=False, bottom=False,
                             labelleft=False, labelbottom=False)

        # ── Price chart ───────────────────────────────────────────────────
        n = len(price_ticks)
        if n < 2:
            price_ticks = [open_price, current_price]
            n = 2
        t = np.linspace(0, elapsed_s, n)

        prices = np.array(price_ticks, dtype=float)
        is_down = current_price < open_price
        line_col = RED if is_down else GREEN

        ax_price.plot(t, prices, color=line_col, linewidth=1.8, zorder=3)
        ax_price.fill_between(t, prices, open_price,
                              where=(prices < open_price),
                              color=RED, alpha=0.12, zorder=2)
        ax_price.fill_between(t, prices, open_price,
                              where=(prices >= open_price),
                              color=GREEN, alpha=0.12, zorder=2)
        ax_price.axhline(open_price, color=MUTED, linewidth=0.9,
                         linestyle='--', alpha=0.7, zorder=1)

        # Entry markers
        offset_secs = {'T-240': 0, 'T-180': 60, 'T-120': 120, 'T-90': 150, 'T-60': 180}
        ep = entry_prices or {}
        price_range = max(prices) - min(prices) or 10
        label_offset = price_range * 0.04

        for label, osec in offset_secs.items():
            if osec > elapsed_s:
                continue
            col = ENTRY_COLORS.get(label, MUTED)
            idx = min(int(osec / elapsed_s * (n - 1)), n - 1) if elapsed_s > 0 else 0
            p = prices[idx]
            ax_price.scatter([osec], [p], color=col, s=55, zorder=6, marker='o',
                             edgecolors=BG, linewidths=0.8)
            ax_price.axvline(osec, color=col, linewidth=0.7,
                             linestyle=':', alpha=0.45, zorder=1)
            ax_price.text(osec + elapsed_s * 0.01, p + label_offset,
                          label, color=col, fontsize=6.5, zorder=7,
                          fontweight='bold')

        # T-60 trade line
        if elapsed_s >= 180:
            ax_price.axvline(180, color=WHITE, linewidth=1.2,
                             linestyle='-', alpha=0.85, zorder=5)
            ax_price.text(183, min(prices) + price_range * 0.05,
                          'T-60\nTRADE', color=WHITE, fontsize=6.5, va='bottom')

        ax_price.set_xlim(0, max(elapsed_s, 10))
        ax_price.tick_params(colors=MUTED, labelsize=7)
        ax_price.yaxis.set_major_formatter(
            plt.FuncFormatter(lambda v, _: f'${v:,.0f}'))
        ax_price.set_xlabel('Seconds elapsed', color=MUTED, fontsize=7, labelpad=3)
        ax_price.grid(True, color=BORDER, alpha=0.5, linewidth=0.4)
        delta_sign = '+' if delta_pct >= 0 else ''
        ax_price.set_title(
            f'BTC 5m · {window_id} · {t_label}  '
            f'({delta_sign}{delta_pct:.3f}%)',
            color=TEXT, fontsize=8.5, pad=5, loc='left',
        )

        # ── Signal panel ──────────────────────────────────────────────────
        ax_sig.axis('off')
        ax_sig.set_xlim(0, 1)
        ax_sig.set_ylim(0, 1)

        def _bar(ax, y, label, value_str, conf, color, note=''):
            """Draw a labelled confidence bar."""
            ax.text(0.04, y + 0.025, label, color=MUTED, fontsize=7,
                    transform=ax.transAxes, va='bottom')
            ax.text(0.97, y + 0.025, value_str, color=color, fontsize=7.5,
                    fontweight='bold', ha='right', transform=ax.transAxes, va='bottom')
            # Track background
            track = mpatches.FancyBboxPatch(
                (0.04, y - 0.01), 0.92, 0.032,
                boxstyle='round,pad=0.002',
                facecolor=BORDER, edgecolor='none',
                transform=ax.transAxes, zorder=2,
            )
            ax.add_patch(track)
            # Filled bar
            fill_w = max(0.002, 0.92 * min(conf, 1.0))
            fill = mpatches.FancyBboxPatch(
                (0.04, y - 0.01), fill_w, 0.032,
                boxstyle='round,pad=0.002',
                facecolor=color, edgecolor='none', alpha=0.65,
                transform=ax.transAxes, zorder=3,
            )
            ax.add_patch(fill)
            if note:
                ax.text(0.04, y - 0.025, note, color=DIM, fontsize=6,
                        transform=ax.transAxes, va='top', style='italic')

        ax_sig.text(0.5, 0.97, 'Signal Snapshot', ha='center', color=TEXT,
                    fontsize=8.5, fontweight='bold', transform=ax_sig.transAxes)

        # VPIN
        vpin_col = RED if vpin >= 0.65 else (AMBER if vpin >= 0.55 else CYAN)
        _bar(ax_sig, 0.82, 'VPIN', f'{vpin:.3f}  {vpin_regime}', vpin, vpin_col,
             note='Informed flow')

        # TWAP
        if twap_direction:
            tw_col = GREEN if twap_direction == 'DOWN' else RED
            _bar(ax_sig, 0.67, 'TWAP', f'{twap_direction}  {twap_agreement}/3',
                 twap_agreement / 3, tw_col, note='Window trend')
        else:
            _bar(ax_sig, 0.67, 'TWAP', '—', 0.1, MUTED, note='No data')

        # TimesFM
        if timesfm_direction:
            tf_col = PURPLE
            _bar(ax_sig, 0.52, 'TimesFM', f'{timesfm_direction}  {timesfm_confidence:.0%}',
                 timesfm_confidence, tf_col,
                 note=f'→ ${timesfm_predicted:,.0f}' if timesfm_predicted else '')
        else:
            _bar(ax_sig, 0.52, 'TimesFM', '—', 0.1, MUTED, note='Unavailable')

        # Gamma
        g_dir = 'UP' if gamma_up > gamma_down else 'DOWN'
        g_conf = max(gamma_up, gamma_down)
        g_col = GREEN if g_dir == 'UP' else RED
        _bar(ax_sig, 0.37, 'Gamma', f'UP ${gamma_up:.2f} / DN ${gamma_down:.2f}',
             g_conf, g_col, note='Polymarket market price')

        # Delta
        d_col = GREEN if delta_pct > 0 else RED
        _bar(ax_sig, 0.22, 'Point', f'{delta_sign}{delta_pct:.4f}%',
             min(abs(delta_pct) / 0.2, 1.0), d_col, note='Open→now')

        # CoinGlass taker
        cg_note = f'Taker: {cg_taker_buy_pct:.0f}% buy'
        if abs(cg_funding_annual) > 50:
            cg_note += f'  Fund: {cg_funding_annual:.0f}%/yr'
        cg_col = GREEN if cg_taker_buy_pct > 55 else (RED if cg_taker_buy_pct < 45 else MUTED)
        _bar(ax_sig, 0.07, 'CoinGlass', f'{cg_taker_buy_pct:.0f}% buy',
             cg_taker_buy_pct / 100, cg_col, note=cg_note)

        # Conflict warning
        dirs = [d for d in [
            twap_direction,
            timesfm_direction,
            'UP' if delta_pct > 0 else 'DOWN',
        ] if d]
        if dirs and len(set(dirs)) > 1:
            ax_sig.text(0.5, 0.02, 'SIGNAL CONFLICT',
                        ha='center', color=AMBER, fontsize=7.5,
                        fontweight='bold', transform=ax_sig.transAxes,
                        bbox=dict(boxstyle='round,pad=0.2', facecolor='rgba(245,158,11,0.1)',
                                  edgecolor=AMBER, linewidth=0.8))

        # ── P&L table ─────────────────────────────────────────────────────
        ax_table.axis('off')
        ax_table.set_xlim(0, 1)
        ax_table.set_ylim(0, 1)

        # Column definitions
        cols = ['Entry', 'Token $', 'If WIN', 'If LOSS', 'EV @99%', 'Break-even WR']
        col_x = [0.01, 0.16, 0.30, 0.44, 0.58, 0.74]
        col_align = ['left', 'left', 'left', 'left', 'left', 'left']

        # Header row
        for cx, h, ca in zip(col_x, cols, col_align):
            ax_table.text(cx, 0.88, h, color=MUTED, fontsize=6.5,
                          fontweight='bold', transform=ax_table.transAxes, ha=ca)
        ax_table.axhline(0.78, color=BORDER, linewidth=0.8,
                         transform=ax_table.transAxes, xmin=0, xmax=1)

        # Data rows
        row_labels = ['T-240', 'T-180', 'T-120', 'T-90', 'T-60']
        row_y = [0.65, 0.50, 0.35, 0.20, 0.05]
        for rl, ry in zip(row_labels, row_y):
            entry_p = (ep.get(rl) or (gamma_down if delta_pct < 0 else gamma_up))
            shares = stake_usd / entry_p if entry_p > 0 else 0
            fee = 0.035 * min(entry_p, 1 - entry_p)
            payout = shares * (1 - fee)
            net_win = payout - stake_usd
            net_loss = -stake_usd
            ev = win_rate * net_win + (1 - win_rate) * net_loss
            be_wr = stake_usd / (net_win + stake_usd) * 100 if (net_win + stake_usd) > 0 else 100

            col = ENTRY_COLORS.get(rl, MUTED)
            vals = [rl, f'${entry_p:.3f}', f'+${net_win:.2f}', f'-${stake_usd:.2f}',
                    f'${ev:+.2f}', f'{be_wr:.1f}%']
            v_colors = [col, CYAN, GREEN, RED,
                        GREEN if ev > 0 else RED, MUTED]

            for cx, v, vc, ca in zip(col_x, vals, v_colors, col_align):
                ax_table.text(cx, ry, v, color=vc, fontsize=7,
                              transform=ax_table.transAxes, ha=ca,
                              fontfamily='monospace')

        # Footer tag
        fig.text(0.99, 0.005,
                 f'[{location}]  {window_id}  {t_label}  {engine_version}',
                 ha='right', color=DIM, fontsize=6.5)

        # Save
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=140, facecolor=BG,
                    bbox_inches='tight')
        plt.close(fig)
        buf.seek(0)
        return buf.read()

    except Exception as exc:
        # Return empty bytes — caller must handle gracefully
        import traceback
        traceback.print_exc()
        return b''
