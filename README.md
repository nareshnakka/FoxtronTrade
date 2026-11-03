# FoxtronTrade — Nifty Convergence Algos

Dual parallel engines on Zerodha Kite:

1. **Algo 1 — Convergence Breakout** — predict CE/PE at converge (125–145), enter early, target 169–175, SL 120  
2. **Algo 2 — Dual-leg** — buy both at converge, book breakout on reverse, ride fallen bounce  

## Run

```powershell
cd D:\NiftyTrading
.\.venv\Scripts\Activate.ps1
$env:PYTHONPATH="D:\NiftyTrading"
python login.py          # each morning after ~6 AM IST
python app.py            # http://127.0.0.1:5050
```

**Live trading defaults OFF** (paper). Toggle in the header. Reports track paper and live separately.

## Dashboard

- Top: Algo1 + Algo2 option line charts with convergence band  
- Bottom: Nifty day candles + entries / live P&L table  
- Settings: lots per algo, enable flags  
- Reports: Daily / Weekly / Monthly P&L  

## Notes

- Session window for entries: **09:15–13:30** (weekends allowed for paper dry-run); **60 min cool-down** after a close before next entry  
- On expiry day, scanner uses **next** weekly expiry  
- Live order placement bridge is gated; ledger marks live mode — wire real `place_order` when you are ready  
