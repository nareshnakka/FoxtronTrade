"""Sensex spot + futures OI regime for today (short covering vs buildup)."""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from foxtrade.fii_oi_scanner import _actions_for, classify_oi_price
from kite_client import get_authenticated_kite

IST = ZoneInfo("Asia/Kolkata")


def main() -> int:
    k = get_authenticated_kite()
    # Spot
    try:
        ltp = k.ltp("BSE:SENSEX")
        print("Spot LTP:", ltp)
    except Exception as exc:
        print("Spot LTP error:", exc)

    bins = k.instruments("BSE")
    sx_idx = [
        i
        for i in bins
        if str(i.get("tradingsymbol", "")).upper() == "SENSEX"
        or str(i.get("name", "")).upper() == "SENSEX"
    ]
    print("BSE SENSEX instruments:", [
        (i.get("tradingsymbol"), i.get("instrument_token"), i.get("segment"))
        for i in sx_idx[:6]
    ])

    # Index minutes today
    idx_tok = None
    for i in sx_idx:
        if "INDEX" in str(i.get("segment", "")).upper() or i.get("instrument_type") == "EQ":
            idx_tok = int(i["instrument_token"])
            break
    if idx_tok is None and sx_idx:
        idx_tok = int(sx_idx[0]["instrument_token"])

    start = dt.datetime(2026, 11, 3, 9, 15, tzinfo=IST)
    end = dt.datetime(2026, 11, 3, 15, 30, tzinfo=IST)
    if idx_tok:
        rows = k.historical_data(idx_tok, start, end, "minute") or []
        print(f"Index bars token={idx_tok}: {len(rows)}")
        if rows:
            o = float(rows[0]["open"])
            c = float(rows[-1]["close"])
            h = max(float(r["high"]) for r in rows)
            l = min(float(r["low"]) for r in rows)
            print(
                f"  Sensex {o:.2f} → {c:.2f} ({c-o:+.1f})  "
                f"H {h:.2f} L {l:.2f}"
            )
            # simple levels
            print(f"  Day high (resistance): {h:.0f}")
            print(f"  Nearest round above: {int(c // 100) * 100 + 100}")
            print(f"  Support day open/low: {o:.0f} / {l:.0f}")

    # Futures OI
    bfo = k.instruments("BFO")
    futs = [
        i
        for i in bfo
        if i.get("name") == "SENSEX" and i.get("instrument_type") == "FUT"
    ]
    futs = sorted(futs, key=lambda x: x.get("expiry") or dt.date.max)
    print("SENSEX FUTs:", [
        (i["tradingsymbol"], str(i.get("expiry")), i["instrument_token"])
        for i in futs[:4]
    ])
    if not futs:
        print("No Sensex fut found")
        return 1
    fut = futs[0]
    tok = int(fut["instrument_token"])
    q = k.quote([f"BFO:{fut['tradingsymbol']}"])
    print("Fut quote keys:", list((q or {}).keys())[:3])
    qrow = (q or {}).get(f"BFO:{fut['tradingsymbol']}") or {}
    print(
        "Fut LTP/OI:",
        qrow.get("last_price"),
        (qrow.get("oi") if "oi" in qrow else (qrow.get("depth") or {})),
    )

    rows = k.historical_data(tok, start, end, "minute", oi=True) or []
    print(f"Fut minute bars: {len(rows)}")
    if not rows:
        return 1
    o = rows[0]
    c = rows[-1]
    open_px = float(o.get("open") or o["close"])
    open_oi = int(o.get("oi") or 0)
    close_px = float(c["close"])
    close_oi = int(c.get("oi") or 0)
    dpx = close_px - open_px
    doi = close_oi - open_oi
    # prefer first non-zero oi as day open oi if needed
    if open_oi <= 0:
        for r in rows:
            if int(r.get("oi") or 0) > 0:
                open_oi = int(r["oi"])
                open_px = float(r.get("open") or r["close"])
                break
        doi = close_oi - open_oi
        dpx = close_px - open_px

    reg = classify_oi_price(price_chg=dpx, oi_chg=doi, price_eps=40.0, oi_eps=2000)
    acts = _actions_for(reg, fii_net=None, fii_net_chg=None, cfg={
        "fii_oi_block_ce_on_short_build": False,
    })
    print(
        f"\nSensex fut {fut['tradingsymbol']}: "
        f"px {open_px:.1f}→{close_px:.1f} ({dpx:+.1f})  "
        f"OI {open_oi}→{close_oi} ({doi:+d})"
    )
    print(f"Regime: {reg}")
    print(
        f"Actions: caution_ce={acts['caution_ce']} prefer_ce={acts['prefer_ce']} "
        f"prefer_pe={acts['prefer_pe']} block_ce={acts['block_ce']}"
    )
    print("Notes:", "; ".join(acts.get("notes") or []))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
