# -*- coding: utf-8 -*-
"""
Cloud Recovery Desk: automated Capitulation-Recovery / Momentum watchlist.

Screening and status updates only. Nothing in this package can place, draft or
submit an order: there is no broker client here, and there is no order code.
Each status is a label for a human to review.

Layout:
- indicators.py  pure maths on completed bars (SMA, Wilder RSI, MACD, VWAP)
- rules.py       the locked rule text (Rule 1/2/3, battery, Anti-Knife, tiers)
- providers.py   paid data feeds with rate limiting and a fallback order
- store.py       where state and snapshots live (local folder or Google Drive)
- desk.py        one cycle: qualify daily, overlay the live session, diff, publish
"""
