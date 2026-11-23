"""
Train Lab — week-indexed causal BT on captured option history.

Supports underlying picker (NIFTY now; auto-discovers future captures) and
projecting live algos (Algo-CW / Algo-NW).
"""
from __future__ import annotations

import csv
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import tools_bt_algo1_exit_learn as L
import tools_bt_final_daily_report as cw
from foxtrade.algo_ids import DISPLAY as ALGO_DISPLAY
from foxtrade.config_store import load_config, save_config
from foxtrade.indicators import support_resistance
from foxtrade.learner import _append_lesson
from foxtrade.underlyings import BY_ID

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parents[1]
CHAIN = ROOT / "data" / "option_chain_1m"
OUT = ROOT / "data" / "backtests"
LAST_RUN = OUT / "lab_last_run.json"
LIVE_DB = ROOT / "data" / "option_chain_live" / "chain.db"

_lock = threading.Lock()

# Compact train grid (fast enough for UI)
_EXIT_MULTS = (1.35, 1.40, 1.45, 1.50, 1.55, 1.60)
_SKIP_GE = (1.0, 3000.0, 5000.0, 15000.0, 1e18)
_RETRACE_PCTS = (0.0, 10.0, 15.0)
_MAX_SETUPS = (1, 2)

PROJECTABLE_ALGOS = ("algo_cw", "algo_nw")


def _now_iso() -> str:
    return datetime.now(IST).isoformat()


def _load_meta(day: str) -> dict[str, Any]:
    path = CHAIN / day / "meta.json"
    if not path.exists():
        viewer = CHAIN / day / "viewer.json"
        if viewer.exists():
            try:
                return dict(json.loads(viewer.read_text(encoding="utf-8")).get("meta") or {})
            except Exception:
                return {}
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def day_underlying(day: str) -> str:
    """Infer underlying for a captured 1m day (future-proof for SENSEX etc.)."""
    day_dir = CHAIN / day
    if day_dir.is_dir():
        spots = sorted(day_dir.glob("*_SPOT_1m.csv"))
        if spots:
            name = spots[0].name.replace("_SPOT_1m.csv", "").upper()
            if name:
                return name
    meta = _load_meta(day)
    if meta.get("underlying"):
        return str(meta["underlying"]).upper()
    # Infer from first contract / strike symbol in meta
    for key in ("contracts", "strike_universe"):
        pass
    viewer = CHAIN / day / "viewer.json"
    if viewer.exists():
        try:
            data = json.loads(viewer.read_text(encoding="utf-8"))
            for fr in (data.get("frames") or [])[:3]:
                for lk, leg in (fr.get("legs") or {}).items():
                    sym = str((leg or {}).get("sym") or "")
                    if sym.startswith("NIFTY"):
                        return "NIFTY"
                    if sym.startswith("SENSEX"):
                        return "SENSEX"
                    if sym.startswith("BANKNIFTY"):
                        return "BANKNIFTY"
        except Exception:
            pass
    return "NIFTY"


def available_days(underlying: str | None = None) -> list[str]:
    if not CHAIN.exists():
        return []
    days = sorted(
        p.name
        for p in CHAIN.iterdir()
        if p.is_dir() and (p / "viewer.json").exists()
    )
    uid = (underlying or "").strip().upper()
    if not uid:
        return days
    return [d for d in days if day_underlying(d) == uid]


def available_nw_days(underlying: str | None = None) -> list[str]:
    """Next-week days from live SQLite (currently NIFTY capture)."""
    uid = (underlying or "NIFTY").strip().upper() or "NIFTY"
    # Live chain DB today is NIFTY-only; other underlyings appear when captured.
    if uid != "NIFTY" or not LIVE_DB.exists():
        return []
    try:
        import tools_bt_nextweek_daily_report as nw

        return list(nw.available_days())
    except Exception:
        return []


def discover_underlyings() -> list[dict[str, Any]]:
    """Underlyings that have Lab-usable history (auto-grows as you capture more)."""
    found: dict[str, dict[str, Any]] = {}
    for d in available_days():
        uid = day_underlying(d)
        slot = found.setdefault(
            uid,
            {
                "id": uid,
                "label": (BY_ID.get(uid) or {}).get("display") or uid,
                "days_cw": 0,
                "days_nw": 0,
                "has_data": True,
            },
        )
        slot["days_cw"] += 1
    for uid in list(found.keys()) + ["NIFTY"]:
        nw_n = len(available_nw_days(uid))
        if nw_n:
            slot = found.setdefault(
                uid,
                {
                    "id": uid,
                    "label": (BY_ID.get(uid) or {}).get("display") or uid,
                    "days_cw": 0,
                    "days_nw": 0,
                    "has_data": True,
                },
            )
            slot["days_nw"] = nw_n
            slot["has_data"] = True
    if not found:
        found["NIFTY"] = {
            "id": "NIFTY",
            "label": (BY_ID.get("NIFTY") or {}).get("display") or "NIFTY",
            "days_cw": 0,
            "days_nw": 0,
            "has_data": False,
        }
    return [found[k] for k in sorted(found.keys())]


def list_algos(cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Projectable live algorithms in Lab."""
    c = cfg or load_config()
    return [
        {
            "id": "algo_cw",
            "label": ALGO_DISPLAY.get("algo_cw", "Algo-CW"),
            "week": "current",
            "enabled_live": bool(c.get("algo_cw_enabled", True)),
            "default_band_lo": float(c.get("premium_low") or 70),
            "default_band_hi": float(c.get("premium_high") or 77),
            "default_win_mult": float(c.get("algo_cw_pair_exit_mult") or 1.45),
            "default_max_setups": int(c.get("algo_cw_max_setups_per_day") or 2),
            "default_skip_ge": float(c.get("algo_cw_skip_second_if_day_pnl_ge") or 3000),
        },
        {
            "id": "algo_nw",
            "label": ALGO_DISPLAY.get("algo_nw", "Algo-NW"),
            "week": "next",
            "enabled_live": bool(c.get("algo_nw_enabled", True)),
            "default_band_lo": float(c.get("algo_nw_premium_low") or 46),
            "default_band_hi": float(c.get("algo_nw_premium_high") or 54),
            "default_win_mult": float(c.get("algo_nw_pair_exit_mult") or 1.70),
            "default_max_setups": int(c.get("algo_nw_max_setups_per_day") or 1),
            "default_skip_ge": 1e18,
        },
        {
            "id": "all",
            "label": "All (project)",
            "week": "both",
            "enabled_live": True,
            "default_band_lo": None,
            "default_band_hi": None,
            "default_win_mult": None,
            "default_max_setups": None,
            "default_skip_ge": None,
        },
    ]


def list_weeks(
    underlying: str | None = "NIFTY",
    algo: str | None = "algo_cw",
) -> dict[str, Any]:
    """Group captured days by option expiry for the selected underlying + algo book."""
    uid = (underlying or "NIFTY").strip().upper() or "NIFTY"
    algo_id = (algo or "algo_cw").strip().lower()
    if algo_id == "algo_nw":
        days = available_nw_days(uid)
        day_meta: dict[str, dict[str, Any]] = {}
        by_exp: dict[str, list[str]] = {}
        for d in days:
            # Prefer 1m meta next_expiry when present
            meta = _load_meta(d) if (CHAIN / d).exists() else {}
            exp = (
                meta.get("next_expiry_scan")
                or meta.get("expiry")
                or "next-week"
            )
            day_meta[d] = {
                "day": d,
                "expiry": exp,
                "week": "next",
                "source": "option_chain_live",
                "underlying": uid,
            }
            by_exp.setdefault(str(exp), []).append(d)
    else:
        days = available_days(uid)
        by_exp = {}
        day_meta = {}
        for d in days:
            meta = _load_meta(d)
            day_meta[d] = {
                "day": d,
                "expiry": meta.get("expiry") or meta.get("current_expiry_scan") or "unknown",
                "week": meta.get("week"),
                "source": meta.get("source"),
                "underlying": day_underlying(d),
            }
            exp = str(day_meta[d]["expiry"] or "unknown")
            by_exp.setdefault(exp, []).append(d)

    weeks = []
    for exp in sorted(by_exp.keys()):
        wdays = sorted(by_exp[exp])
        weeks.append(
            {
                "expiry": exp,
                "label": f"Week exp {exp}" if exp not in ("unknown", "next-week") else str(exp),
                "days": wdays,
                "day_count": len(wdays),
            }
        )

    known = [w for w in weeks if w["expiry"] not in ("unknown",)]
    current = known[-1] if known else (weeks[-1] if weeks else None)
    past = known[-2] if len(known) >= 2 else None
    cfg = load_config()

    return {
        "ok": True,
        "underlying": uid,
        "algo": algo_id,
        "underlyings": discover_underlyings(),
        "algos": list_algos(cfg),
        "days": days,
        "weeks": weeks,
        "current_week": current,
        "past_week": past,
        "presets": {
            "current": (current or {}).get("days") or [],
            "past": (past or {}).get("days") or [],
            "all": days,
        },
        "day_meta": day_meta,
    }


def days_for_preset(
    preset: str = "current",
    days: list[str] | None = None,
    *,
    underlying: str = "NIFTY",
    algo: str = "algo_cw",
) -> list[str]:
    info = list_weeks(underlying=underlying, algo=algo)
    preset = (preset or "current").strip().lower()
    if preset == "custom" and days:
        avail = set(info["days"])
        return [d for d in days if d in avail]
    if preset == "past":
        return list(info["presets"]["past"])
    if preset == "all":
        return list(info["presets"]["all"])
    out = list(info["presets"]["current"])
    if not out:
        return list(info["days"][-5:])
    return out


def _load_frames_cw(days: list[str]) -> dict[str, list[dict]]:
    frames: dict[str, list[dict]] = {}
    for d in days:
        path = CHAIN / d / "viewer.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            frames[d] = list(data.get("frames") or [])
        except Exception:
            continue
    return frames


def _load_frames_nw(days: list[str]) -> dict[str, list[dict]]:
    import tools_bt_nextweek_daily_report as nw

    frames: dict[str, list[dict]] = {}
    for d in days:
        try:
            frs, _meta = nw.load_nextweek_frames(d)
            if frs:
                frames[d] = frs
        except Exception:
            continue
    return frames


def _load_frames(days: list[str], *, algo: str = "algo_cw") -> dict[str, list[dict]]:
    if (algo or "").strip().lower() == "algo_nw":
        return _load_frames_nw(days)
    return _load_frames_cw(days)


def _spot_closes(
    day: str,
    frames: list[dict] | None = None,
    *,
    underlying: str = "NIFTY",
) -> list[float]:
    uid = (underlying or "NIFTY").upper()
    csv_path = CHAIN / day / f"{uid}_SPOT_1m.csv"
    if not csv_path.exists():
        # fallback any spot file
        alts = sorted((CHAIN / day).glob("*_SPOT_1m.csv")) if (CHAIN / day).is_dir() else []
        csv_path = alts[0] if alts else csv_path
    closes: list[float] = []
    if csv_path.exists():
        try:
            with csv_path.open(encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    for key in ("close", "Close", "c"):
                        if row.get(key) not in (None, ""):
                            closes.append(float(row[key]))
                            break
        except Exception:
            closes = []
    if not closes and frames:
        for fr in frames:
            spot = fr.get("spot")
            if spot is not None:
                try:
                    closes.append(float(spot))
                except Exception:
                    pass
    return closes


def analyze_sr(
    days: list[str],
    frames: dict[str, list[dict]] | None = None,
    *,
    underlying: str = "NIFTY",
) -> list[dict[str, Any]]:
    """Per-day support / resistance from spot closes."""
    frames = frames or {}
    rows = []
    for d in days:
        closes = _spot_closes(d, frames.get(d), underlying=underlying)
        sr = support_resistance(closes, lookback=min(80, max(5, len(closes))))
        mid = None
        if closes:
            mid = round(closes[-1], 2)
        rng = None
        if sr.get("support") is not None and sr.get("resistance") is not None:
            rng = round(float(sr["resistance"]) - float(sr["support"]), 2)
        rows.append(
            {
                "day": d,
                "spot_last": mid,
                "support": round(float(sr["support"]), 2) if sr.get("support") is not None else None,
                "resistance": round(float(sr["resistance"]), 2) if sr.get("resistance") is not None else None,
                "range": rng,
                "samples": len(closes),
            }
        )
    return rows


def _score_row(daily: list[dict]) -> dict[str, Any]:
    nets = [float(d.get("net") or 0) for d in daily]
    return {
        "net": round(sum(nets), 2),
        "green": sum(1 for n in nets if n > 0),
        "red": sum(1 for n in nets if n < 0),
        "flat": sum(1 for n in nets if n == 0),
        "worst": round(min(nets), 2) if nets else 0.0,
        "best": round(max(nets), 2) if nets else 0.0,
        "avg": round(sum(nets) / len(nets), 2) if nets else 0.0,
        "daily": [{"day": d["day"], "net": round(float(d.get("net") or 0), 2), "setups": int(d.get("setup_count") or 0)} for d in daily],
    }


def _policy_payload(pol: L.Policy) -> dict[str, Any]:
    return {
        "name": pol.name,
        "win_mult": pol.win_mult,
        "mode": pol.mode,
        "retrace_pct": pol.retrace_pct,
        "max_setups": pol.max_setups,
        "skip_2nd_if_net_ge": pol.skip_2nd_if_net_ge if pol.skip_2nd_if_net_ge < 1e17 else None,
        "reentry_after": pol.reentry_after,
        "last_entry": pol.last_entry,
    }


def _leg_close(frame: dict, side: str, strike: int) -> float | None:
    row = (frame.get("legs") or {}).get(f"{side}:{int(strike)}")
    if not row or row.get("c") is None:
        return None
    try:
        return float(row["c"])
    except Exception:
        return None


def _spot_at(frames: list[dict], hm: str | None) -> float | None:
    if not hm:
        return None
    # Exact match, else last frame at/before hm
    last: float | None = None
    for fr in frames:
        t = fr.get("t") or ""
        spot = fr.get("spot")
        if spot is not None:
            try:
                last = float(spot)
            except Exception:
                pass
        if t == hm:
            return last
        if t > hm:
            break
    return last


def _enrich_setup(setup: dict[str, Any], frames: list[dict], lot_size: int) -> dict[str, Any]:
    out = dict(setup)
    entry_t = str(out.get("entry_t") or "")
    exit_t = str(out.get("exit_t") or "")
    out["index_entry"] = _spot_at(frames, entry_t)
    out["index_exit"] = _spot_at(frames, exit_t)
    lots = int(out.get("lots") or 0)
    out["qty"] = lots * int(lot_size)
    out["lot_size"] = int(lot_size)
    # Round money fields
    for k in ("gross", "charges", "net", "ce_entry", "pe_entry", "ce_exit", "pe_exit", "mid", "gap"):
        if out.get(k) is not None:
            try:
                out[k] = round(float(out[k]), 2)
            except Exception:
                pass
    # Premium points gained/lost (exit − entry)
    try:
        ce0 = float(out.get("ce_entry") or 0)
        pe0 = float(out.get("pe_entry") or 0)
        cex = float(out.get("ce_exit") or 0)
        pex = float(out.get("pe_exit") or 0)
        ce_pts = round(cex - ce0, 2)
        pe_pts = round(pex - pe0, 2)
        pair_pts = round(ce_pts + pe_pts, 2)
        out["ce_pts"] = ce_pts
        out["pe_pts"] = pe_pts
        out["pair_pts"] = pair_pts
        # Index points while in trade
        ie = out.get("index_entry")
        ix = out.get("index_exit")
        if ie is not None and ix is not None:
            out["index_pts"] = round(float(ix) - float(ie), 2)
        else:
            out["index_pts"] = None
    except Exception:
        out["ce_pts"] = out["pe_pts"] = out["pair_pts"] = None
        out["index_pts"] = None
    return out


def _day_chart(
    day: str,
    frames: list[dict],
    setups: list[dict],
    *,
    band_lo: float,
    band_hi: float,
) -> dict[str, Any]:
    """CE/PE premium series for the day's primary pair + buy/exit markers."""
    if not setups:
        # Fallback: ATM CE/PE if present
        ce_k = pe_k = None
        for fr in frames:
            atm = fr.get("atm")
            if atm is None:
                continue
            ce_k = pe_k = int(atm)
            break
    else:
        ce_k = int(setups[0].get("ce_strike") or 0) or None
        pe_k = int(setups[0].get("pe_strike") or 0) or None

    ce_pts: list[dict[str, Any]] = []
    pe_pts: list[dict[str, Any]] = []
    spot_pts: list[dict[str, Any]] = []
    last_ce = last_pe = None
    for fr in frames:
        t = fr.get("t") or ""
        if not t:
            continue
        if ce_k is not None:
            c = _leg_close(fr, "CE", ce_k)
            if c is not None:
                last_ce = c
            if last_ce is not None:
                ce_pts.append({"t": t, "v": round(last_ce, 2)})
        if pe_k is not None:
            p = _leg_close(fr, "PE", pe_k)
            if p is not None:
                last_pe = p
            if last_pe is not None:
                pe_pts.append({"t": t, "v": round(last_pe, 2)})
        spot = fr.get("spot")
        if spot is not None:
            try:
                spot_pts.append({"t": t, "v": round(float(spot), 2)})
            except Exception:
                pass

    markers: list[dict[str, Any]] = []
    for s in setups:
        sid = s.get("setup_id")
        et = s.get("entry_t")
        xt = s.get("exit_t")
        if et:
            markers.append(
                {
                    "t": et,
                    "kind": "buy",
                    "setup_id": sid,
                    "text": f"BUY#{sid}",
                    "ce": s.get("ce_entry"),
                    "pe": s.get("pe_entry"),
                }
            )
        if xt:
            markers.append(
                {
                    "t": xt,
                    "kind": "exit",
                    "setup_id": sid,
                    "text": f"EXIT#{sid}",
                    "ce": s.get("ce_exit"),
                    "pe": s.get("pe_exit"),
                }
            )

    return {
        "day": day,
        "ce_strike": ce_k,
        "pe_strike": pe_k,
        "band": [float(band_lo), float(band_hi)],
        "ce": ce_pts,
        "pe": pe_pts,
        "spot": spot_pts,
        "markers": markers,
    }


def _build_day_reports(
    daily: list[dict],
    frames: dict[str, list[dict]],
    *,
    band_lo: float,
    band_hi: float,
    lot_size: int,
    sr_by_day: dict[str, dict] | None = None,
) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    sr_by_day = sr_by_day or {}
    for drow in daily:
        day = str(drow.get("day") or "")
        frs = frames.get(day) or []
        raw_setups = list(drow.get("setups") or [])
        setups = [_enrich_setup(s, frs, lot_size) for s in raw_setups]
        pair_pts = round(
            sum(float(s.get("pair_pts") or 0) for s in setups),
            2,
        )
        ce_pts = round(sum(float(s.get("ce_pts") or 0) for s in setups), 2)
        pe_pts = round(sum(float(s.get("pe_pts") or 0) for s in setups), 2)
        reports.append(
            {
                "day": day,
                "setup_count": int(drow.get("setup_count") or len(setups)),
                "gross": round(float(drow.get("gross") or 0), 2),
                "charges": round(float(drow.get("charges") or 0), 2),
                "net": round(float(drow.get("net") or 0), 2),
                "pair_pts": pair_pts,
                "ce_pts": ce_pts,
                "pe_pts": pe_pts,
                "note": drow.get("note"),
                "setups": setups,
                "sr": sr_by_day.get(day),
                "chart": _day_chart(day, frs, setups, band_lo=band_lo, band_hi=band_hi),
            }
        )
    return reports


def _run_policy(
    days: list[str],
    frames: dict[str, list[dict]],
    *,
    band_lo: float,
    band_hi: float,
    policy: L.Policy,
    lot_size: int,
    cfg: dict,
    capital: float,
    with_daily: bool = False,
) -> dict[str, Any]:
    old_band = L.BAND
    old_cap = L.CAPITAL
    L.BAND = (float(band_lo), float(band_hi))
    L.CAPITAL = float(capital)
    try:
        daily, _ = L.run_universe(days, frames, policy=policy, lot_size=lot_size, cfg=cfg)
        sc = _score_row(daily)
        out = {
            "label": policy.name,
            "policy": _policy_payload(policy),
            "band": [float(band_lo), float(band_hi)],
            **sc,
        }
        if with_daily:
            out["_daily"] = daily
        return out
    finally:
        L.BAND = old_band
        L.CAPITAL = old_cap


def _capital_for_algo(algo: str, cfg: dict) -> float:
    if (algo or "").strip().lower() == "algo_nw":
        return float(
            cfg.get("algo_nw_capital_inr")
            or cfg.get("algo_cw_capital_inr")
            or cfg.get("capital_inr")
            or 100_000
        )
    return float(cfg.get("algo_cw_capital_inr") or cfg.get("capital_inr") or 100_000)


def _lot_size(underlying: str, cfg: dict) -> int:
    uid = (underlying or "NIFTY").upper()
    spec = BY_ID.get(uid) or {}
    if spec.get("lot_size"):
        return int(spec["lot_size"])
    return int(cfg.get("lot_size") or 75)


def _algo_defaults(algo: str, cfg: dict) -> dict[str, Any]:
    for a in list_algos(cfg):
        if a["id"] == algo:
            return a
    return list_algos(cfg)[0]


def _rank_key(row: dict[str, Any]) -> tuple:
    return (int(row["green"]), float(row["worst"]), float(row["net"]))


def build_train_grid(
    *,
    band_lo: float,
    band_hi: float,
    exit_mults: list[float] | None = None,
    include_retrace: bool = True,
) -> list[L.Policy]:
    mults = exit_mults or list(_EXIT_MULTS)
    pols: list[L.Policy] = []
    # Baselines + exit mult × max setups × skip
    for m in mults:
        for mx in _MAX_SETUPS:
            for skip in _SKIP_GE:
                if mx == 1 and skip not in (1e18,):
                    continue  # skip threshold irrelevant for max1
                skip_lab = "never" if skip >= 1e17 else f"skip@{int(skip)}"
                name = f"hit {m:g}x max{mx} {skip_lab}"
                pols.append(
                    L.Policy(
                        name,
                        float(m),
                        "hit",
                        max_setups=int(mx),
                        skip_2nd_if_net_ge=float(skip),
                    )
                )
    if include_retrace:
        for pct in _RETRACE_PCTS:
            if pct <= 0:
                continue
            pols.append(
                L.Policy(
                    f"retrace {pct:g}% after 1.45x max1",
                    1.45,
                    "retrace",
                    retrace_pct=float(pct),
                    max_setups=1,
                    skip_2nd_if_net_ge=1e18,
                )
            )
    # Deduplicate by name
    seen: set[str] = set()
    out: list[L.Policy] = []
    for p in pols:
        if p.name in seen:
            continue
        seen.add(p.name)
        out.append(p)
    return out


def _analyze_one(
    *,
    algo: str,
    underlying: str,
    preset: str,
    days: list[str] | None,
    band_lo: float,
    band_hi: float,
    win_mult: float,
    max_setups: int,
    skip_2nd_if_net_ge: float,
    mode: str,
    retrace_pct: float,
) -> dict[str, Any]:
    if band_hi <= band_lo:
        return {"ok": False, "error": "band_hi must be > band_lo", "algo": algo}

    selected = days_for_preset(preset, days, underlying=underlying, algo=algo)
    frames = _load_frames(selected, algo=algo)
    selected = [d for d in selected if d in frames and frames[d]]
    if not selected:
        return {
            "ok": False,
            "error": f"no {algo} frames for {underlying} / {preset}",
            "algo": algo,
            "underlying": underlying,
        }

    cfg = load_config()
    lot = _lot_size(underlying, cfg)
    capital = _capital_for_algo(algo, cfg)
    pol = L.Policy(
        f"analyze {mode} {win_mult:g}x",
        float(win_mult),
        str(mode or "hit"),
        retrace_pct=float(retrace_pct or 0),
        max_setups=int(max_setups),
        skip_2nd_if_net_ge=float(skip_2nd_if_net_ge),
    )
    row = _run_policy(
        selected,
        frames,
        band_lo=band_lo,
        band_hi=band_hi,
        policy=pol,
        lot_size=lot,
        cfg=cfg,
        capital=capital,
        with_daily=True,
    )
    daily = list(row.pop("_daily", []) or [])
    sr = analyze_sr(selected, frames, underlying=underlying)
    sr_by_day = {r["day"]: r for r in sr}
    day_reports = _build_day_reports(
        daily,
        frames,
        band_lo=band_lo,
        band_hi=band_hi,
        lot_size=lot,
        sr_by_day=sr_by_day,
    )
    return {
        "ok": True,
        "kind": "analyze",
        "algo": algo,
        "algo_label": ALGO_DISPLAY.get(algo, algo),
        "underlying": underlying,
        "preset": preset,
        "days": selected,
        "band": [float(band_lo), float(band_hi)],
        "capital": capital,
        "lot_size": lot,
        "result": row,
        "sr": sr,
        "day_reports": day_reports,
        "at": _now_iso(),
    }


def analyze(
    *,
    algo: str = "algo_cw",
    underlying: str = "NIFTY",
    preset: str = "current",
    days: list[str] | None = None,
    band_lo: float | None = None,
    band_hi: float | None = None,
    win_mult: float | None = None,
    max_setups: int | None = None,
    skip_2nd_if_net_ge: float | None = None,
    mode: str = "hit",
    retrace_pct: float = 0.0,
) -> dict[str, Any]:
    """Causal analyze for one algo, or project all (algo='all')."""
    uid = (underlying or "NIFTY").strip().upper() or "NIFTY"
    algo_raw = (algo or "algo_cw").strip().lower()
    cfg = load_config()

    if algo_raw == "all":
        projects = []
        for aid in PROJECTABLE_ALGOS:
            defs = _algo_defaults(aid, cfg)
            projects.append(
                _analyze_one(
                    algo=aid,
                    underlying=uid,
                    preset=preset,
                    days=days,
                    band_lo=float(defs["default_band_lo"]),
                    band_hi=float(defs["default_band_hi"]),
                    win_mult=float(defs["default_win_mult"]),
                    max_setups=int(defs["default_max_setups"]),
                    skip_2nd_if_net_ge=float(defs["default_skip_ge"]),
                    mode=mode,
                    retrace_pct=retrace_pct,
                )
            )
        ok_projects = [p for p in projects if p.get("ok")]
        # Summary KPI from first ok / combined
        nets = [float((p.get("result") or {}).get("net") or 0) for p in ok_projects]
        result = {
            "ok": bool(ok_projects),
            "kind": "analyze",
            "algo": "all",
            "algo_label": "All (project)",
            "underlying": uid,
            "preset": preset,
            "projects": projects,
            "days": sorted({d for p in ok_projects for d in (p.get("days") or [])}),
            "result": {
                "label": "all projects",
                "net": round(sum(nets), 2),
                "green": sum(int((p.get("result") or {}).get("green") or 0) for p in ok_projects),
                "red": sum(int((p.get("result") or {}).get("red") or 0) for p in ok_projects),
                "worst": min((float((p.get("result") or {}).get("worst") or 0) for p in ok_projects), default=0),
                "best": max((float((p.get("result") or {}).get("best") or 0) for p in ok_projects), default=0),
                "band": None,
            },
            "day_reports": [],
            "sr": [],
            "at": _now_iso(),
        }
        # Flatten day reports tagged by algo for one-page scroll
        flat = []
        for p in ok_projects:
            for dr in p.get("day_reports") or []:
                tagged = dict(dr)
                tagged["algo"] = p.get("algo")
                tagged["algo_label"] = p.get("algo_label")
                flat.append(tagged)
        result["day_reports"] = flat
        _persist(result)
        return result

    if algo_raw not in PROJECTABLE_ALGOS:
        return {"ok": False, "error": "algo must be algo_cw, algo_nw, or all"}

    defs = _algo_defaults(algo_raw, cfg)
    out = _analyze_one(
        algo=algo_raw,
        underlying=uid,
        preset=preset,
        days=days,
        band_lo=float(band_lo if band_lo is not None else defs["default_band_lo"]),
        band_hi=float(band_hi if band_hi is not None else defs["default_band_hi"]),
        win_mult=float(win_mult if win_mult is not None else defs["default_win_mult"]),
        max_setups=int(max_setups if max_setups is not None else defs["default_max_setups"]),
        skip_2nd_if_net_ge=float(
            skip_2nd_if_net_ge if skip_2nd_if_net_ge is not None else defs["default_skip_ge"]
        ),
        mode=mode,
        retrace_pct=retrace_pct,
    )
    if out.get("ok"):
        _persist(out)
    return out


def train(
    *,
    algo: str = "algo_cw",
    underlying: str = "NIFTY",
    preset: str = "current",
    days: list[str] | None = None,
    band_lo: float | None = None,
    band_hi: float | None = None,
    band_sweep: bool = False,
    exit_mults: list[float] | None = None,
) -> dict[str, Any]:
    """
    Sweep exit policies → pick best green/safe winner for one algo.
    Does not write live config until promote().
    """
    uid = (underlying or "NIFTY").strip().upper() or "NIFTY"
    algo_id = (algo or "algo_cw").strip().lower()
    if algo_id == "all":
        return {"ok": False, "error": "Train one algo at a time (Algo-CW or Algo-NW)"}
    if algo_id not in PROJECTABLE_ALGOS:
        return {"ok": False, "error": "algo must be algo_cw or algo_nw"}

    cfg = load_config()
    defs = _algo_defaults(algo_id, cfg)
    blo = float(band_lo if band_lo is not None else defs["default_band_lo"])
    bhi = float(band_hi if band_hi is not None else defs["default_band_hi"])
    if bhi <= blo:
        return {"ok": False, "error": "band_hi must be > band_lo"}

    selected = days_for_preset(preset, days, underlying=uid, algo=algo_id)
    frames = _load_frames(selected, algo=algo_id)
    selected = [d for d in selected if d in frames and frames[d]]
    if not selected:
        return {"ok": False, "error": f"no {algo_id} frames for {uid} / {preset}"}

    lot = _lot_size(uid, cfg)
    capital = _capital_for_algo(algo_id, cfg)

    bands: list[tuple[float, float]] = [(blo, bhi)]
    if band_sweep:
        mid = (blo + bhi) / 2.0
        width = max(4.0, bhi - blo)
        for w in (width - 2, width, width + 2):
            if w < 4:
                continue
            lo = round(mid - w / 2, 1)
            hi = round(mid + w / 2, 1)
            bands.append((lo, hi))
        seen_b: set[tuple[float, float]] = set()
        uniq: list[tuple[float, float]] = []
        for b in bands:
            if b in seen_b:
                continue
            seen_b.add(b)
            uniq.append(b)
        bands = uniq

    # NW trains with its exit mults around live default
    if algo_id == "algo_nw" and not exit_mults:
        exit_mults = [1.55, 1.60, 1.70, 1.80, 1.90]

    pols = build_train_grid(band_lo=blo, band_hi=bhi, exit_mults=exit_mults)
    rows: list[dict[str, Any]] = []
    with _lock:
        for band_lo_i, band_hi_i in bands:
            for pol in pols:
                row = _run_policy(
                    selected,
                    frames,
                    band_lo=band_lo_i,
                    band_hi=band_hi_i,
                    policy=pol,
                    lot_size=lot,
                    cfg=cfg,
                    capital=capital,
                )
                rows.append(row)

    rows.sort(key=_rank_key, reverse=True)
    best = rows[0] if rows else None
    best_net = max(rows, key=lambda r: (r["net"], r["green"], r["worst"])) if rows else None
    zero_red = [r for r in rows if r["red"] == 0]
    best_safe = max(zero_red, key=_rank_key) if zero_red else best

    sr = analyze_sr(selected, frames, underlying=uid)
    result = {
        "ok": True,
        "kind": "train",
        "algo": algo_id,
        "algo_label": ALGO_DISPLAY.get(algo_id, algo_id),
        "underlying": uid,
        "preset": preset,
        "days": selected,
        "band_input": [blo, bhi],
        "bands_tested": bands,
        "capital": capital,
        "lot_size": lot,
        "policies_tested": len(rows),
        "rows": rows[:40],
        "best": best,
        "best_net": best_net,
        "best_safe": best_safe,
        "sr": sr,
        "at": _now_iso(),
        "ready_to_promote": best is not None,
    }
    _persist(result)
    return result


def winner_to_config_updates(algo: str, winner: dict[str, Any]) -> dict[str, Any]:
    """Map a train/analyze row → live Algo-CW or Algo-NW config keys."""
    pol = winner.get("policy") or {}
    band = winner.get("band") or [70.0, 77.0]
    mode = str(pol.get("mode") or "hit")
    win_mult = float(pol.get("win_mult") or 1.45)
    max_setups = int(pol.get("max_setups") or 2)
    skip = pol.get("skip_2nd_if_net_ge")
    skip_ge = float(skip) if skip is not None else 15_000.0
    retrace = float(pol.get("retrace_pct") or 0)
    wait_retrace = mode == "retrace" and retrace > 0
    algo_id = (algo or "algo_cw").strip().lower()

    if algo_id == "algo_nw":
        updates = {
            "algo_nw_premium_low": float(band[0]),
            "algo_nw_premium_high": float(band[1]),
            "algo_nw_pair_exit_mult": win_mult,
            "algo_nw_max_setups_per_day": max_setups,
            "algo1_enabled": False,
            "algo1_convergence_dual": False,
        }
        return updates

    updates = {
        "premium_low": float(band[0]),
        "premium_high": float(band[1]),
        "algo_cw_pair_exit_mult": win_mult,
        "algo_cw_max_setups_per_day": max_setups,
        "algo_cw_skip_second_if_day_pnl_ge": skip_ge,
        "algo_cw_pair_exit_wait_retrace": wait_retrace,
        "algo1_enabled": False,
        "algo1_convergence_dual": False,
    }
    if wait_retrace:
        updates["algo_cw_pair_exit_retrace_min_pct"] = max(5.0, retrace * 0.5)
        updates["algo_cw_pair_exit_retrace_max_pct"] = max(retrace, 10.0)
    return updates


def promote(
    *,
    algo: str | None = None,
    winner: dict[str, Any] | None = None,
    use_last_best: bool = True,
    reload_engine: bool = True,
) -> dict[str, Any]:
    """Write winning knobs into config.json and reload engine."""
    last = load_last_run()
    algo_id = (algo or (last or {}).get("algo") or "algo_cw").strip().lower()
    if algo_id == "all":
        return {"ok": False, "error": "Promote one algo at a time (pick Algo-CW or Algo-NW)"}
    if algo_id not in PROJECTABLE_ALGOS:
        algo_id = "algo_cw"
    if winner is None and use_last_best:
        winner = (last or {}).get("best") or (last or {}).get("result")
    if not winner:
        return {"ok": False, "error": "no winner to promote — run Train first"}

    updates = winner_to_config_updates(algo_id, winner)
    updates.update(
        {
            "train_lab_at": _now_iso(),
            "train_lab_tag": str((winner.get("label") or winner.get("policy", {}).get("name") or "lab")[:120]),
            "train_lab_algo": algo_id,
            "train_lab_pnl": float(winner.get("net") or 0),
        }
    )
    save_config(updates)

    try:
        _append_lesson(
            {
                "at": _now_iso(),
                "kind": "lab_train_promote",
                "algo": algo_id,
                "label": winner.get("label"),
                "net": winner.get("net"),
                "green": winner.get("green"),
                "red": winner.get("red"),
                "band": winner.get("band"),
                "policy": winner.get("policy"),
                "updates": updates,
            }
        )
    except Exception:
        pass

    engine_status = None
    if reload_engine:
        try:
            from foxtrade.engine import ENGINE

            ENGINE.reload_config()
            engine_status = "reloaded"
        except Exception as exc:  # noqa: BLE001
            engine_status = f"reload_failed: {exc}"

    out = {
        "ok": True,
        "algo": algo_id,
        "updates": updates,
        "winner": {
            "label": winner.get("label"),
            "net": winner.get("net"),
            "green": winner.get("green"),
            "red": winner.get("red"),
            "band": winner.get("band"),
            "policy": winner.get("policy"),
        },
        "engine": engine_status,
        "at": _now_iso(),
    }
    # Keep last run but mark promoted
    if last:
        last["promoted"] = out
        _persist(last)
    return out


def _persist(payload: dict[str, Any]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        LAST_RUN.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass


def load_last_run() -> dict[str, Any] | None:
    if not LAST_RUN.exists():
        return None
    try:
        return json.loads(LAST_RUN.read_text(encoding="utf-8"))
    except Exception:
        return None
