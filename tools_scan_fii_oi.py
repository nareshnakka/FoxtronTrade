"""CLI: scan Nifty fut OI + FII participant state, save, print actions."""
from __future__ import annotations

import argparse
import json
import sys

from foxtrade.config_store import load_config
from foxtrade.fii_oi_scanner import (
    PARTICIPANT,
    LATEST,
    get_fii_oi_state,
    ingest_participant_manual,
    scan_fii_oi,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="FII / futures OI short-cover scanner")
    ap.add_argument("--force", action="store_true")
    ap.add_argument(
        "--set-fii",
        nargs=2,
        type=float,
        metavar=("LONG", "SHORT"),
        help="Manually ingest NSE participant index-fut long/short for FII",
    )
    ap.add_argument("--as-of", default=None, help="date for --set-fii YYYY-MM-DD")
    args = ap.parse_args()

    cfg = {}
    try:
        cfg = load_config() or {}
    except Exception:
        cfg = {}

    if args.set_fii:
        row = ingest_participant_manual(
            fii_long=args.set_fii[0],
            fii_short=args.set_fii[1],
            as_of=args.as_of,
        )
        print("Saved participant:", json.dumps(row, indent=2))
        print(f"→ {PARTICIPANT}")

    state = scan_fii_oi(force=True, cfg=cfg) if args.force or args.set_fii else get_fii_oi_state(cfg)
    acts = state.get("actions") or {}
    print()
    print(f"Day {state.get('day')}  {state.get('symbol')}  LTP={state.get('ltp')}  OI={state.get('oi')}")
    print(
        f"Δprice day={state.get('price_chg_vs_day_open')}  "
        f"ΔOI day={state.get('oi_chg_vs_day_open')}"
    )
    print(f"Regime: {state.get('regime')}  smart_money={state.get('smart_money')}")
    print(
        f"FII net={state.get('fii_net')}  Δnet={state.get('fii_net_chg')}  "
        f"participant={state.get('participant_source') or state.get('participant_ok')}"
    )
    print("Actions:")
    print(f"  block_ce={acts.get('block_ce')}  caution_ce={acts.get('caution_ce')}")
    print(f"  prefer_pe={acts.get('prefer_pe')}  prefer_ce={acts.get('prefer_ce')}")
    print(f"  side_hint={acts.get('side_hint')}  conf_adj={acts.get('confidence_adj')}")
    for n in acts.get("notes") or []:
        print(f"  · {n}")
    if state.get("quote_error"):
        print(f"Quote note: {state['quote_error']}", file=sys.stderr)
    if state.get("participant_error"):
        print(f"Participant note: {state['participant_error']}", file=sys.stderr)
    print(f"\nSaved → {LATEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
