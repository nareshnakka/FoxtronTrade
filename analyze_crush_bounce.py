"""
Test crush→bounce observations on last 3 months spot data.

Data limit: no historical option LTPs in repo — use synthetic fixed-strike
weekly CE/PE from spot (δ≈0.5 + θ), same family as the causal backtest.
This tests the STRUCTURE of the claim, not exact broker premiums.

Hypotheses:
  H1 (PE week crush): week-start PE in [400,500], later falls into [100,130],
      then forward max rebound is ≥1x–3x from that low (i.e. ≥2×–4× the low).
  H2 (CE daily -50%): CE falls ≥50% day-over-day; next session shoots up.
  H3 (3-day fall): CE, PE, or spot falls ≥3 consecutive sessions; next day bounces.

No lookahead in event definition: trigger on day t close; measure t+1.. forward only.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from statistics import mean, median
from typing import Any

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "expiries" / "last_3_months"
OUT = ROOT / "data" / "backtests"

DELTA = 0.50
THETA_PER_DAY = 8.0  # premium bleed per session (rough weekly option)
SESSION_THETA_FRAC = 0.6  # most theta during session


def _load_meta() -> dict[str, Any]:
    return json.loads((DATA / "all_expiries_every_day.json").read_text(encoding="utf-8"))


def _load_closes(path: Path) -> list[float]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    return [float(r["close"]) for r in rows]


def _atm(spot: float) -> float:
    return round(spot / 50.0) * 50.0


@dataclass
class Session:
    date: str
    expiry_week: str
    is_expiry: bool
    open: float
    high: float
    low: float
    close: float
    closes: list[float]


def _sessions() -> list[Session]:
    meta = _load_meta()
    out: list[Session] = []
    for exp in meta.get("expiries") or []:
        for d in exp.get("days") or []:
            if not d.get("session"):
                continue
            path = DATA / d["minutes_file"]
            if not path.exists():
                continue
            closes = _load_closes(path)
            if len(closes) < 60:
                continue
            spot = d.get("spot") or {}
            out.append(
                Session(
                    date=d["date"],
                    expiry_week=str(d.get("expiry_week") or exp.get("expiry") or "")[:10],
                    is_expiry=bool(d.get("is_expiry_day")),
                    open=float(spot.get("open") or closes[0]),
                    high=float(spot.get("high") or max(closes)),
                    low=float(spot.get("low") or min(closes)),
                    close=float(spot.get("close") or closes[-1]),
                    closes=closes,
                )
            )
    # unique by date (prefer first)
    by: dict[str, Session] = {}
    for s in out:
        by.setdefault(s.date, s)
    return [by[k] for k in sorted(by)]


def _prem_path(
    closes: list[float],
    *,
    side: str,
    strike: float,
    start_prem: float,
    spot0: float,
) -> list[float]:
    """Intraday synthetic premium for fixed strike from session open premium."""
    out = []
    n = max(1, len(closes) - 1)
    for i, s in enumerate(closes):
        ds = float(s) - spot0
        # delta vs ATM-ish; tilt if ITM/OTM
        moneyness = (float(s) - strike) / max(strike, 1)
        if side == "CE":
            dlt = max(0.15, min(0.85, DELTA + moneyness))
            px = start_prem + dlt * ds - (THETA_PER_DAY * SESSION_THETA_FRAC) * (i / n)
        else:
            dlt = max(0.15, min(0.85, DELTA - moneyness))
            px = start_prem - dlt * ds - (THETA_PER_DAY * SESSION_THETA_FRAC) * (i / n)
        out.append(max(5.0, px))
    return out


def _week_start_prem(side: str, spot: float, strike: float, dte_sessions: int) -> float:
    """Rough weekly option open premium so deep ITM can reach 400–500."""
    if side == "CE":
        intrinsic = max(0.0, spot - strike)
    else:
        intrinsic = max(0.0, strike - spot)
    # time value larger early in week
    tv = 90.0 + 35.0 * max(1, dte_sessions) ** 0.5
    # bump if already ITM
    return max(25.0, intrinsic + tv)


def build_week_series(sessions: list[Session]) -> list[dict[str, Any]]:
    """Per expiry-week: fixed ATM strike at first session; track CE/PE day OHLC synth."""
    by_week: dict[str, list[Session]] = defaultdict(list)
    for s in sessions:
        if s.expiry_week:
            by_week[s.expiry_week].append(s)

    rows: list[dict[str, Any]] = []
    for week, days in sorted(by_week.items()):
        days = sorted(days, key=lambda x: x.date)
        if len(days) < 2:
            continue
        spot0 = days[0].open
        strike = _atm(spot0)
        dte0 = len(days)
        pe0 = _week_start_prem("PE", spot0, strike, dte0)
        ce0 = _week_start_prem("CE", spot0, strike, dte0)
        # carry premium day to day from prior close
        pe = pe0
        ce = ce0
        for i, day in enumerate(days):
            pe_path = _prem_path(
                day.closes, side="PE", strike=strike, start_prem=pe, spot0=day.open
            )
            ce_path = _prem_path(
                day.closes, side="CE", strike=strike, start_prem=ce, spot0=day.open
            )
            # align start to carried premium
            pe_path = [max(5.0, p - pe_path[0] + pe) for p in pe_path]
            ce_path = [max(5.0, p - ce_path[0] + ce) for p in ce_path]
            row = {
                "date": day.date,
                "week": week,
                "i": i,
                "n_week": len(days),
                "strike": strike,
                "spot_open": day.open,
                "spot_close": day.close,
                "spot_chg": round(day.close - day.open, 1),
                "pe_open": round(pe_path[0], 1),
                "pe_high": round(max(pe_path), 1),
                "pe_low": round(min(pe_path), 1),
                "pe_close": round(pe_path[-1], 1),
                "ce_open": round(ce_path[0], 1),
                "ce_high": round(max(ce_path), 1),
                "ce_low": round(min(ce_path), 1),
                "ce_close": round(ce_path[-1], 1),
                "is_expiry": day.is_expiry,
            }
            rows.append(row)
            pe = pe_path[-1]
            ce = ce_path[-1]
            # overnight extra theta
            pe = max(5.0, pe - THETA_PER_DAY * (1 - SESSION_THETA_FRAC))
            ce = max(5.0, ce - THETA_PER_DAY * (1 - SESSION_THETA_FRAC))
    return rows


def summarize_forward(mults: list[float]) -> dict[str, Any]:
    if not mults:
        return {"n": 0}
    hits_1x = sum(1 for m in mults if m >= 2.0)  # 1x gain = 2× price
    hits_2x = sum(1 for m in mults if m >= 3.0)
    hits_3x = sum(1 for m in mults if m >= 4.0)
    return {
        "n": len(mults),
        "avg_max_mult": round(mean(mults), 2),
        "med_max_mult": round(median(mults), 2),
        "pct_ge_1x_gain": round(100 * hits_1x / len(mults), 1),
        "pct_ge_2x_gain": round(100 * hits_2x / len(mults), 1),
        "pct_ge_3x_gain": round(100 * hits_3x / len(mults), 1),
        "pct_any_bounce": round(100 * sum(1 for m in mults if m > 1.0) / len(mults), 1),
    }


def test_h1_pe_week_crush(week_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Within a week: PE open of day0 in [400,500] OR week max early pe_open in band,
    then some later day pe_low/pe_close enters [100,130]. Measure forward max pe_high
    / trigger_low after trigger day (same week only).
    """
    by_week: dict[str, list[dict]] = defaultdict(list)
    for r in week_rows:
        by_week[r["week"]].append(r)

    events = []
    for week, days in by_week.items():
        days = sorted(days, key=lambda x: x["date"])
        start_pe = days[0]["pe_open"]
        # also allow first-day high in band
        start_ok = 400 <= start_pe <= 500 or 400 <= days[0]["pe_high"] <= 550
        if not start_ok:
            # check if pe was in band any of first 2 days open
            early = days[:2]
            if not any(400 <= d["pe_open"] <= 500 or 400 <= d["pe_high"] <= 550 for d in early):
                continue
        trigger_i = None
        trigger_low = None
        for i, d in enumerate(days):
            if i == 0:
                continue
            low = d["pe_low"]
            close = d["pe_close"]
            if 100 <= low <= 130 or 100 <= close <= 130:
                trigger_i = i
                trigger_low = min(low, close)
                break
        if trigger_i is None or trigger_low is None or trigger_low <= 0:
            continue
        # forward within week after trigger day
        fwd = days[trigger_i + 1 :]
        same_day_after = days[trigger_i]["pe_high"]  # rest of day already in high
        max_fwd = same_day_after
        for d in fwd:
            max_fwd = max(max_fwd, d["pe_high"])
        # if trigger was on last day, still use same-day high after low (path order unknown)
        # Conservative: only use next sessions if available
        if fwd:
            max_fwd = max(d["pe_high"] for d in fwd)
            horizon = "next_sessions_in_week"
        else:
            max_fwd = days[trigger_i]["pe_close"]  # no future — weak
            horizon = "close_only_no_future"
        mult = max_fwd / trigger_low
        events.append(
            {
                "week": week,
                "start_pe": days[0]["pe_open"],
                "trigger_date": days[trigger_i]["date"],
                "trigger_low": round(trigger_low, 1),
                "max_fwd": round(max_fwd, 1),
                "max_mult": round(mult, 2),
                "horizon": horizon,
                "days_after": len(fwd),
            }
        )

    # Also: broader crush — pe drops ≥60% from week-start open to a day low, then bounce
    broad = []
    for week, days in by_week.items():
        days = sorted(days, key=lambda x: x["date"])
        pe0 = days[0]["pe_open"]
        for i, d in enumerate(days):
            if i == 0:
                continue
            if d["pe_low"] <= pe0 * 0.35 and d["pe_low"] <= 150:
                low = d["pe_low"]
                fwd = days[i + 1 :]
                if not fwd:
                    continue
                max_fwd = max(x["pe_high"] for x in fwd)
                broad.append(
                    {
                        "week": week,
                        "start_pe": pe0,
                        "trigger_date": d["date"],
                        "trigger_low": round(low, 1),
                        "max_fwd": round(max_fwd, 1),
                        "max_mult": round(max_fwd / low, 2),
                        "crush_pct": round(100 * (1 - low / pe0), 1),
                    }
                )
                break

    strict_mults = [e["max_mult"] for e in events if e["horizon"] != "close_only_no_future"]
    broad_mults = [e["max_mult"] for e in broad]
    return {
        "hypothesis": "H1 PE week start ~400–500 → crush to ~100–130 → shoot 1x–3x",
        "data_note": "synthetic fixed-strike weekly PE from spot",
        "strict_band_events": events,
        "strict_summary": summarize_forward(strict_mults),
        "broad_crush_60pct_events": broad,
        "broad_summary": summarize_forward(broad_mults),
        "verdict_strict": _verdict_h1(strict_mults),
        "verdict_broad": _verdict_h1(broad_mults),
    }


def _verdict_h1(mults: list[float]) -> str:
    if len(mults) < 3:
        return "INCONCLUSIVE — too few matching events in 3mo synthetic sample"
    s = summarize_forward(mults)
    # "correct" if majority get at least 1x gain (2× price)
    if s["pct_ge_1x_gain"] >= 55:
        return (
            f"MOSTLY SUPPORTED — {s['pct_ge_1x_gain']}% of events reached ≥1x gain "
            f"(avg max mult {s['avg_max_mult']}× from low)"
        )
    if s["pct_any_bounce"] >= 60:
        return (
            f"WEAKLY SUPPORTED — bounce often ({s['pct_any_bounce']}%) but "
            f"≥1x gain only {s['pct_ge_1x_gain']}% (avg mult {s['avg_max_mult']}×)"
        )
    return (
        f"NOT SUPPORTED — ≥1x gain only {s['pct_ge_1x_gain']}%, "
        f"any bounce {s['pct_any_bounce']}% (n={s['n']})"
    )


def test_h2_ce_half_daily(week_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """CE close falls ≥50% vs prior CE close; measure next day CE max/open bounce."""
    rows = sorted(week_rows, key=lambda x: x["date"])
    events = []
    for i in range(1, len(rows) - 1):
        prev, cur, nxt = rows[i - 1], rows[i], rows[i + 1]
        if prev["ce_close"] <= 0:
            continue
        # same week preferred
        chg = cur["ce_close"] / prev["ce_close"] - 1.0
        if chg > -0.50:
            continue
        # next day shoot: max mult vs cur close
        mult_close = nxt["ce_close"] / cur["ce_close"]
        mult_high = nxt["ce_high"] / cur["ce_close"]
        events.append(
            {
                "date": cur["date"],
                "prev_ce": prev["ce_close"],
                "ce_close": cur["ce_close"],
                "day_chg_pct": round(100 * chg, 1),
                "next_date": nxt["date"],
                "next_ce_close": nxt["ce_close"],
                "next_ce_high": nxt["ce_high"],
                "next_mult_close": round(mult_close, 2),
                "next_mult_high": round(mult_high, 2),
                "shot_up": mult_high >= 1.25 or mult_close >= 1.15,
            }
        )

    # Also consecutive 50% falls streak
    streak_events = []
    i = 1
    while i < len(rows) - 1:
        if rows[i - 1]["ce_close"] <= 0:
            i += 1
            continue
        if rows[i]["ce_close"] / rows[i - 1]["ce_close"] - 1 > -0.50:
            i += 1
            continue
        streak = 1
        j = i
        while j + 1 < len(rows) and rows[j]["ce_close"] > 0:
            if rows[j + 1]["ce_close"] / rows[j]["ce_close"] - 1 <= -0.50:
                streak += 1
                j += 1
            else:
                break
        if streak >= 1 and j + 1 < len(rows):
            cur = rows[j]
            nxt = rows[j + 1]
            streak_events.append(
                {
                    "end_date": cur["date"],
                    "streak_50pct_days": streak,
                    "ce_close": cur["ce_close"],
                    "next_mult_high": round(nxt["ce_high"] / cur["ce_close"], 2),
                    "next_mult_close": round(nxt["ce_close"] / cur["ce_close"], 2),
                    "shot_up": nxt["ce_high"] / cur["ce_close"] >= 1.25,
                }
            )
        i = j + 1

    shot = sum(1 for e in events if e["shot_up"])
    return {
        "hypothesis": "H2 CE falls ≥50% in a day → next day shoots up",
        "events": events,
        "n": len(events),
        "pct_shot_up": round(100 * shot / len(events), 1) if events else 0,
        "avg_next_mult_high": round(mean(e["next_mult_high"] for e in events), 2)
        if events
        else 0,
        "streak_events": streak_events,
        "verdict": (
            "MOSTLY SUPPORTED"
            if events and shot / len(events) >= 0.55
            else (
                "WEAKLY SUPPORTED"
                if events and shot / len(events) >= 0.40
                else ("INCONCLUSIVE" if len(events) < 5 else "NOT SUPPORTED")
            )
        )
        + (f" — next-day shoot-up {round(100*shot/len(events),1)}% of {len(events)} events" if events else " — no ≥50% CE day-crush events"),
    }


def test_h3_three_day_fall(week_rows: list[dict[str, Any]], sessions: list[Session]) -> dict[str, Any]:
    """Anything falling ≥3 consecutive sessions → next session bounce."""

    def run_series(name: str, values: list[tuple[str, float]]) -> dict[str, Any]:
        events = []
        for i in range(2, len(values) - 1):
            d0, v0 = values[i - 2]
            d1, v1 = values[i - 1]
            d2, v2 = values[i]
            dn, vn = values[i + 1]
            if v0 <= 0 or v1 <= 0 or v2 <= 0:
                continue
            if not (v1 < v0 and v2 < v1):
                continue
            # 3 consecutive down closes
            # need one more check: values[i-2], i-1, i are three downs from prior
            if i >= 3:
                _, v_prev = values[i - 3]
                if not (v0 < v_prev and v1 < v0 and v2 < v1):
                    # still count as 3-day down chain ending at i if v0>v1>v2 — need 3 falls = 4 points
                    pass
            # Require strictly 3 consecutive negative day changes ending at i
            # values[i-2] -> i-1 -> i : two falls only. Need i-3.
            continue
        # redo properly with 4 points
        events = []
        for i in range(3, len(values) - 1):
            (_, a), (_, b), (_, c), (d_end, d) = (
                values[i - 3],
                values[i - 2],
                values[i - 1],
                values[i],
            )
            if min(a, b, c, d) <= 0:
                continue
            if b < a and c < b and d < c:
                # 3 consecutive falls
                d_next, v_next = values[i + 1]
                # bounce: next close > d OR we need high — only have close for spot series
                bounce_close = v_next > d
                ret = v_next / d - 1.0
                events.append(
                    {
                        "end_date": d_end,
                        "next_date": d_next,
                        "fall_pct_3d": round(100 * (d / a - 1), 1),
                        "next_ret_pct": round(100 * ret, 1),
                        "bounced": bounce_close,
                    }
                )
        n = len(events)
        nb = sum(1 for e in events if e["bounced"])
        return {
            "series": name,
            "n": n,
            "pct_next_day_bounce": round(100 * nb / n, 1) if n else 0,
            "avg_next_ret_pct": round(mean(e["next_ret_pct"] for e in events), 1) if n else 0,
            "med_next_ret_pct": round(median(e["next_ret_pct"] for e in events), 1) if n else 0,
            "events": events[:40],
            "verdict": (
                "MOSTLY SUPPORTED"
                if n >= 5 and nb / n >= 0.55
                else (
                    "WEAKLY SUPPORTED"
                    if n >= 5 and nb / n >= 0.45
                    else ("INCONCLUSIVE" if n < 5 else "NOT SUPPORTED")
                )
            )
            + (
                f" — next close up {round(100*nb/n,1)}% after 3-day fall (n={n}, avg next {round(mean(e['next_ret_pct'] for e in events),1) if n else 0}%)"
                if n
                else " — too few events"
            ),
        }

    by_date = {r["date"]: r for r in week_rows}
    dates = sorted(by_date)
    ce_series = [(d, by_date[d]["ce_close"]) for d in dates]
    pe_series = [(d, by_date[d]["pe_close"]) for d in dates]
    spot_series = [(s.date, s.close) for s in sessions]

    # For CE/PE also measure next day HIGH bounce (stronger "shoot up")
    def run_prem_with_high(name: str, close_key: str, high_key: str) -> dict[str, Any]:
        events = []
        for i in range(3, len(dates) - 1):
            a = by_date[dates[i - 3]][close_key]
            b = by_date[dates[i - 2]][close_key]
            c = by_date[dates[i - 1]][close_key]
            d = by_date[dates[i]][close_key]
            if min(a, b, c, d) <= 0:
                continue
            if b < a and c < b and d < c:
                nxt = by_date[dates[i + 1]]
                bounce = nxt[high_key] / d >= 1.15 or nxt[close_key] > d
                events.append(
                    {
                        "end_date": dates[i],
                        "next_date": dates[i + 1],
                        "fall_pct_3d": round(100 * (d / a - 1), 1),
                        "next_ret_close_pct": round(100 * (nxt[close_key] / d - 1), 1),
                        "next_mult_high": round(nxt[high_key] / d, 2),
                        "bounced": bounce,
                    }
                )
        n = len(events)
        nb = sum(1 for e in events if e["bounced"])
        return {
            "series": name,
            "n": n,
            "pct_bounce": round(100 * nb / n, 1) if n else 0,
            "avg_next_close_pct": round(mean(e["next_ret_close_pct"] for e in events), 1)
            if n
            else 0,
            "avg_next_mult_high": round(mean(e["next_mult_high"] for e in events), 2)
            if n
            else 0,
            "events": events,
            "verdict": (
                "MOSTLY SUPPORTED"
                if n >= 5 and nb / n >= 0.55
                else (
                    "WEAKLY SUPPORTED"
                    if n >= 5 and nb / n >= 0.45
                    else ("INCONCLUSIVE" if n < 5 else "NOT SUPPORTED")
                )
            )
            + (
                f" — bounce {round(100*nb/n,1)}% (n={n})"
                if n
                else " — too few events"
            ),
        }

    return {
        "hypothesis": "H3 continuous fall ≥3 days → next day shoots up",
        "spot_close": run_series("spot_close", spot_series),
        "synth_ce": run_prem_with_high("synth_ce", "ce_close", "ce_high"),
        "synth_pe": run_prem_with_high("synth_pe", "pe_close", "pe_high"),
    }


def main() -> None:
    sessions = _sessions()
    week_rows = build_week_series(sessions)
    print(f"Sessions {len(sessions)} · week-rows {len(week_rows)}")

    # How often week-start PE lands in 400–500
    weeks = defaultdict(list)
    for r in week_rows:
        weeks[r["week"]].append(r)
    start_in_band = 0
    start_prems = []
    for w, days in weeks.items():
        days = sorted(days, key=lambda x: x["date"])
        pe0 = days[0]["pe_open"]
        start_prems.append(pe0)
        if 400 <= pe0 <= 500:
            start_in_band += 1
    print(
        f"Weeks {len(weeks)} · week-start synth PE in [400,500]: {start_in_band} "
        f"· avg start PE {mean(start_prems):.0f} · min {min(start_prems):.0f} · max {max(start_prems):.0f}"
    )

    h1 = test_h1_pe_week_crush(week_rows)
    h2 = test_h2_ce_half_daily(week_rows)
    h3 = test_h3_three_day_fall(week_rows, sessions)

    print("\n=== H1 PE crush 400–500 → 100–130 → bounce ===")
    print("strict", h1["strict_summary"], h1["verdict_strict"])
    print("broad ", h1["broad_summary"], h1["verdict_broad"])
    for e in h1["strict_band_events"][:12]:
        print(" ", e)
    print("\n=== H2 CE −50% day → next shoot ===")
    print(h2["verdict"], "avg next high mult", h2["avg_next_mult_high"])
    for e in h2["events"][:12]:
        print(" ", e)
    print("\n=== H3 ≥3-day fall → bounce ===")
    print("spot:", h3["spot_close"]["verdict"])
    print("CE:  ", h3["synth_ce"]["verdict"])
    print("PE:  ", h3["synth_pe"]["verdict"])

    report = {
        "title": "Crush→bounce observation test (3 months)",
        "generated_at": date.today().isoformat(),
        "cheat": False,
        "data_limitation": (
            "No historical option LTP chain in repo. Used synthetic fixed-strike "
            "weekly CE/PE from spot (δ≈0.5 + θ). Structure test only."
        ),
        "week_start_pe_stats": {
            "weeks": len(weeks),
            "in_400_500": start_in_band,
            "avg": round(mean(start_prems), 1),
            "min": round(min(start_prems), 1),
            "max": round(max(start_prems), 1),
        },
        "H1_pe_week_crush": h1,
        "H2_ce_half_daily": h2,
        "H3_three_day_fall": h3,
    }
    # trim huge event lists in nested for JSON readability — keep all but cap spot events already

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "crush_bounce_analysis.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"/><title>Crush→Bounce Analysis</title>
<style>
body{{font-family:Segoe UI,system-ui,sans-serif;background:#0f1419;color:#e7ecf3;margin:24px;max-width:1100px}}
h1,h2{{color:#fff}} .good{{color:#7dcea0}} .bad{{color:#f1948a}} .warn{{color:#f7dc6f}} .muted{{color:#9aa4b2}}
.card{{background:#1a222d;border-radius:10px;padding:14px 16px;margin:12px 0}}
code{{background:#243041;padding:1px 6px;border-radius:4px}}
</style></head><body>
<h1>Crush → bounce observations · 3 months</h1>
<p class="muted">{report['data_limitation']}</p>
<div class="card">
<b>Week-start synth PE:</b> avg {mean(start_prems):.0f}
(min {min(start_prems):.0f}, max {max(start_prems):.0f}) ·
in [400,500]: {start_in_band}/{len(weeks)} weeks
</div>
<h2>H1 — PE week 400–500 → 100–130 → 1x–3x shoot</h2>
<div class="card">
Strict band events: {h1['strict_summary']}<br/>
<span class="{'good' if 'SUPPORTED' in h1['verdict_strict'] and 'NOT' not in h1['verdict_strict'] else 'warn'}">{h1['verdict_strict']}</span><br/><br/>
Broad (≥60% crush to ≤150): {h1['broad_summary']}<br/>
<span class="{'good' if 'SUPPORTED' in h1['verdict_broad'] and 'NOT' not in h1['verdict_broad'] else 'warn'}">{h1['verdict_broad']}</span>
</div>
<h2>H2 — CE −50% in a day → next day shoot up</h2>
<div class="card">
Events: {h2['n']} · shoot-up rate: {h2['pct_shot_up']}% · avg next high mult: {h2['avg_next_mult_high']}×<br/>
<span class="{'good' if h2['verdict'].startswith('MOSTLY') else 'warn'}">{h2['verdict']}</span>
</div>
<h2>H3 — Fall ≥3 sessions → next day bounce</h2>
<div class="card">
<b>Spot:</b> {h3['spot_close']['verdict']}<br/>
<b>Synth CE:</b> {h3['synth_ce']['verdict']}<br/>
<b>Synth PE:</b> {h3['synth_pe']['verdict']}
</div>
</body></html>"""
    (OUT / "crush_bounce_analysis.html").write_text(html, encoding="utf-8")
    print("\nJSON", path)
    print("HTML", OUT / "crush_bounce_analysis.html")


if __name__ == "__main__":
    main()
