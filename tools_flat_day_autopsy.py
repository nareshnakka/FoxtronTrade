"""Why causal algo stayed flat on most days of a full-range backtest."""
from __future__ import annotations

import json
from collections import Counter
from datetime import date
from pathlib import Path

from backtest_detailed import _day_prem_opens, _load_bars, _seed_decay_days_before, simulate_day_detailed
from backtest_month_ledger import LedgerState, _load_month_days, month_cfg
from foxtrade.decay_memory import DecayMemory
from foxtrade.market_direction import analyze_market
from tools_backtest_week import aggressive_overrides

OUT = Path("data/backtests/flat_day_autopsy.json")


def _hhmm(i: int) -> str:
    tot = 9 * 60 + 15 + i
    return f"{tot // 60:02d}:{tot % 60:02d}"


def classify_day(bars: list[dict], cfg: dict, *, is_expiry: bool, decay_mem: DecayMemory, day: str) -> dict:
    closes = [float(b["close"]) for b in bars]
    if len(closes) < 40:
        return {"bucket": "no_data", "detail": "too few bars", "best": None}

    # Mirror backtest session cut
    max_i = min(len(closes) - 1, int(cfg.get("no_entry_after_minutes", 300)))
    counts: Counter[str] = Counter()
    best = None  # highest conf near-miss
    allow_minutes = 0
    persist_need = int(cfg.get("signal_persist_minutes", 2))
    persist_side = None
    persist_n = 0

    for i in range(15, max_i + 1):
        path = closes[: i + 1]
        analysis = analyze_market(
            path,
            cfg=cfg,
            is_expiry_day=is_expiry,
        )
        allow = bool(analysis.get("allow_entry"))
        side = analysis.get("side")
        conf = float(analysis.get("confidence") or 0)
        event = str(analysis.get("event") or "")
        reason = str(analysis.get("reason") or "no_signal")

        # Post-decay overlay peek
        pd = {}
        if decay_mem is not None:
            try:
                # decay already fed by simulate; here we only classify directional misses
                pass
            except Exception:
                pass

        if not allow or side not in ("CE", "PE"):
            # Bucket the denial
            r = reason.lower()
            if "chop" in r or event.upper() == "CHOP":
                bucket = "chop / no edge"
            elif "expiry" in r and ("wait" in r or "early" in r or "late" in r):
                bucket = "expiry window gate"
            elif "orb" in r or "range" in r:
                bucket = "ORB not ready / spent"
            elif "sr" in r or "support" in r or "resist" in r:
                bucket = "SR not clean"
            elif "sharp" in r or "fall" in r or "crush" in r:
                bucket = "sharp-fall / crush block"
            elif "efficiency" in r or "path" in r:
                bucket = "path efficiency block"
            elif "mom" in r or "momentum" in r:
                bucket = "momentum too weak"
            elif "conf" in r or "margin" in r:
                bucket = "confidence / margin"
            elif "session" in r or "late" in r or "no entry" in r:
                bucket = "session timing cut"
            elif "trend" in r or "vs open" in r:
                bucket = "vs-open / trend filter"
            elif not side or event in ("", "NONE", "FLAT"):
                bucket = "no directional side"
            else:
                bucket = f"other: {event or reason[:40]}"
            counts[bucket] += 1
            persist_side, persist_n = None, 0
            if best is None or conf > float(best.get("conf") or 0):
                best = {
                    "t": _hhmm(i),
                    "side": side,
                    "conf": round(conf, 3),
                    "event": event,
                    "reason": reason[:160],
                    "allow": False,
                }
            continue

        # allow true — persist gate
        allow_minutes += 1
        need = persist_need
        if event in ("SR_RECLAIM", "SR_REJECT") and need > 0:
            sr_vs = abs(float(analysis.get("vs_open") or 0))
            sr_mom = abs(float(analysis.get("momentum") or 0))
            hard_pts = float(cfg.get("trend_hard_pts", 70))
            mid_vs = float(cfg.get("sr_fast_persist_min_vs", 50))
            fast_mom = float(cfg.get("sr_fast_persist_min_mom", 18))
            fast = (sr_vs >= hard_pts and sr_mom >= fast_mom) or (
                mid_vs <= sr_vs < hard_pts
            )
            need = int(cfg.get("sr_signal_persist_minutes", 1)) if fast else max(need, 2)
        if need > 0:
            if side == persist_side:
                persist_n += 1
            else:
                persist_side, persist_n = side, 1
            if persist_n < need:
                counts["signal died in persist"] += 1
                if best is None or conf > float(best.get("conf") or 0):
                    best = {
                        "t": _hhmm(i),
                        "side": side,
                        "conf": round(conf, 3),
                        "event": event,
                        "reason": f"persist {persist_n}/{need} · {reason[:120]}",
                        "allow": True,
                    }
                continue
        counts["ALLOW would enter"] += 1
        if best is None or conf >= float(best.get("conf") or 0):
            best = {
                "t": _hhmm(i),
                "side": side,
                "conf": round(conf, 3),
                "event": event,
                "reason": reason[:160],
                "allow": True,
            }

    if not counts:
        top = "no_data"
    else:
        top = counts.most_common(1)[0][0]
    # Prefer a clearer primary bucket
    if counts.get("ALLOW would enter", 0) > 0:
        primary = "had allow (sim may have banked / sized / post-decay path)"
    else:
        primary = top
    return {
        "bucket": primary,
        "allow_minutes": allow_minutes,
        "counts": dict(counts.most_common(8)),
        "best": best,
    }


def main() -> int:
    as_of = date(2026, 11, 2)
    days = _load_month_days(as_of=as_of, calendar_days=200, all_available=True)
    cfg = month_cfg()
    cfg.update(aggressive_overrides(1_000_000, 1.0))
    state = LedgerState(initial=1_000_000, balance=1_000_000, deploy_pct=1.0, deploy_cap=0.0)
    seed = _seed_decay_days_before(days[0]["date"]) if days else []
    decay_mem = DecayMemory(persist=False, seed_days=seed)

    bucket_n: Counter[str] = Counter()
    rows = []
    traded = 0
    for d in days:
        bars = _load_bars(d["minutes_path"])
        ce0, pe0 = _day_prem_opens(d["date"])
        is_exp = bool(d.get("is_expiry_day"))
        row = simulate_day_detailed(
            bars,
            cfg,
            state,
            day=d["date"],
            is_expiry_day=is_exp,
            decay_mem=decay_mem,
            ce_open=ce0,
            pe_open=pe0,
            verbose=False,
        )
        decay_mem.finalize_day(d["date"])
        setups = int(row.get("setups") or 0)
        if setups > 0:
            traded += 1
            bucket_n["TRADED"] += 1
            rows.append(
                {
                    "date": d["date"],
                    "expiry": is_exp,
                    "bucket": "TRADED",
                    "net": row.get("net"),
                    "event": (row.get("trades") or [{}])[0].get("event"),
                }
            )
            continue
        if str(row.get("skip") or "") == "too few bars" or len(bars) < 40:
            bucket_n["no_data / too few bars"] += 1
            rows.append({"date": d["date"], "bucket": "no_data / too few bars"})
            continue
        info = classify_day(
            bars, cfg, is_expiry=is_exp, decay_mem=decay_mem, day=d["date"]
        )
        bucket_n[info["bucket"]] += 1
        rows.append(
            {
                "date": d["date"],
                "expiry": is_exp,
                "bucket": info["bucket"],
                "allow_minutes": info["allow_minutes"],
                "counts": info["counts"],
                "best": info["best"],
                "net": 0,
            }
        )

    print(f"Sessions {len(days)}  traded {traded}  flat {len(days) - traded}")
    print("\nWhy flat / traded:")
    for k, v in bucket_n.most_common():
        print(f"  {v:3d}  {k}")

    print("\nFlat-day nearest miss (top conf):")
    flat = [r for r in rows if r.get("bucket") not in ("TRADED",)]
    shown = 0
    for r in flat:
        b = r.get("best") or {}
        if not b:
            continue
        if shown >= 20:
            break
        if r.get("bucket", "").startswith("no_data"):
            continue
        print(
            f"  {r['date']}  [{r['bucket']}]  "
            f"{b.get('t')} {b.get('side')} conf={b.get('conf')} "
            f"{b.get('event')} · {b.get('reason')}"
        )
        shown += 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(
            {
                "range": {"start": days[0]["date"], "end": days[-1]["date"]},
                "sessions": len(days),
                "traded": traded,
                "buckets": dict(bucket_n),
                "days": rows,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nJSON {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
