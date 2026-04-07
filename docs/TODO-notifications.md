# Notification TODOs

**Last updated:** Apr 7, 2026 12:35 UTC

## ✅ Fixed

- [x] SITREP W/L zeroes on restart → reads from DB now (`bb2c9d7`)
- [x] Session counter race condition → synchronous await (`bb2c9d7`)
- [x] GTC NOT FILLED shows CLOB price not cap → shows both (`01215ec`)
- [x] PLACED card shows Gamma price → shows limit cap + reason (`01215ec`)
- [x] AI Assessment shows "Gamma SNAPSHOT" → shows limit + reason (`01215ec`)
- [x] Resolution shows entry_price (wrong) → shows actual fill price (`1496d9e`)
- [x] entry_price in DB was CLOB cascade price → now records submission price (`01215ec`)
- [x] Fill price calc stake/shares wrong on partial fills → uses limit price (`1a3308a`)
- [x] Result card shows Polymarket aggregate → shows our trade from DB (`bfd50ac`)
- [x] RFQ cap bypassed dynamic cap → uses PRICE_CAP (`1496d9e`)

## 🟡 Remaining Issues

- [ ] **Gamma ↑$0.500 ↓$0.500 in window header** — Always shows 50/50 indicative price. Cosmetic but confusing. Should show CLOB best ask or remove entirely.
- [ ] **P&L in SITREP/session counter is unreliable** — DB pnl_usd values are wrong for pre-fix trades (stake/shares bug). Wallet is ground truth ($130.90). Consider reading wallet balance for P&L display.
- [ ] **NORMAL regime passes at T-70** — Both post-fix losses were VPIN 0.49/0.54 at T-70. Consider requiring TRANSITION+ (VPIN≥0.55) for T-70/T-60 offsets.
- [ ] **GTC fill confirmation timing** — `send_entry_alert` fires but user may not see it clearly between other notifications. Consider consolidating fill + trade card.
- [ ] **Multiple engine restarts cause duplicate orders** — When restarting, ensure old LIVE orders are cancelled before new ones placed.
