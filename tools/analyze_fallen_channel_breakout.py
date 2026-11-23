"""CLI wrapper → foxtrade.lab_fallen_channel.run_scan"""
from __future__ import annotations

from foxtrade.lab_fallen_channel import run_scan


def main() -> None:
    print("Running fallen-channel Lab scan…")
    payload = run_scan(include_db=True, persist=True)
    s = payload["summary"]
    p = payload["params"]
    print(
        f"instruments={p['instruments']} fall_windows={p['fall_windows']} "
        f"channels={p['tradable_channels']} trades={s['n_trades_all']} "
        f"in_band={s['n_trades_band_30_130']}"
    )
    for t in payload["trades"]:
        o = t["outcomes"]["tp40"]
        print(
            f"  {t['expiry']} {t['side']}{t['strike']} {t['entry_ts']} "
            f"px={t['entry_px']} band={t['band']} dist={t['break_dist']} "
            f"tp40={o['exit']}:{o['pnl']:+}"
        )
    print("\nTOP setups:")
    for row in s["top_setups"][:15]:
        print(
            f"  {row['filter']:16s} band={row['band']:8s} tp={row['tp']} "
            f"n={row['n']:2d} WR={row['win_rate']:.0%} avg={row['avg_pnl']:+6.1f} "
            f"R={row['expectancy_R']:+.2f} PF={row['profit_factor']}"
        )


if __name__ == "__main__":
    main()
