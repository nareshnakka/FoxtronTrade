"""
Macro / news / risk overlay for confidence & side bias.

Fetches (best-effort, never blocks trading):
  - India VIX (Kite → Yahoo fallback)
  - Crude (CL=F), USDINR, Gold, US ES/NQ futures
  - Multi-feed headlines: markets, politics, Fed/RBI, geopolitics, FII
  - Soft "smart money" proxies: VIX spike, USDINR spike, US overnight risk-off

All network calls have short timeouts. Cached ~2 minutes.
"""

from __future__ import annotations

import json
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

_CACHE: dict[str, Any] = {"ts": 0.0, "data": {}}

_FEEDS = [
    # Markets / India
    "https://news.google.com/rss/search?q=Nifty+OR+Sensex+OR+RBI+OR+FII+OR+DII&hl=en-IN&gl=IN&ceid=IN:en",
    # Crude / commodity
    "https://news.google.com/rss/search?q=crude+oil+OR+Brent+OR+OPEC&hl=en-IN&gl=IN&ceid=IN:en",
    # Politics / geopolitics
    "https://news.google.com/rss/search?q=India+election+OR+geopolitical+OR+war+OR+sanctions+OR+Middle+East&hl=en-IN&gl=IN&ceid=IN:en",
    # Global macro / Fed
    "https://news.google.com/rss/search?q=Federal+Reserve+OR+US+futures+OR+Treasury+yields+OR+risk+off&hl=en-IN&gl=IN&ceid=IN:en",
    # Big money / shorting / operators
    "https://news.google.com/rss/search?q=FII+selling+OR+short+selling+OR+bulk+deal+OR+promoter+pledge+Nifty&hl=en-IN&gl=IN&ceid=IN:en",
]

_NEG = (
    "war", "attack", "crash", "sanctions", "conflict", "missile", "selloff", "panic",
    "default", "invasion", "terror", "collapse", "recession", "hawkish", "hike rates",
    "fii sell", "outflow", "short selling", "ban", "emergency", "coup", "strike",
    "crude spike", "rupee crash", "circuit", "fraud", "scam",
)
_POS = (
    "ceasefire", "rally", "record high", "cut rates", "stimulus", "peace",
    "dovish", "fii buy", "inflow", "cool inflation", "soft landing", "breakthrough",
    "deal signed", "truce",
)


def _yahoo_last(symbol: str) -> tuple[float | None, float | None]:
    """Return (last, prior_close) from Yahoo chart API."""
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?range=5d&interval=1d"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "FoxtronTrade/1.0"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        payload = json.loads(resp.read().decode())
    result = payload["chart"]["result"][0]
    meta = result["meta"]
    last = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    return (
        float(last) if last is not None else None,
        float(prev) if prev is not None else None,
    )


def _yahoo_chg(symbol: str) -> tuple[float | None, float | None]:
    last, prev = _yahoo_last(symbol)
    if last is None or prev is None or prev == 0:
        return last, None
    return last, round((last - prev) / prev * 100.0, 2)


def _rss_titles(url: str, limit: int = 12) -> list[str]:
    req = urllib.request.Request(url, headers={"User-Agent": "FoxtronTrade/1.0"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        raw = resp.read()
    root = ET.fromstring(raw)
    titles: list[str] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if title:
            titles.append(title)
        if len(titles) >= limit:
            break
    return titles


def _headline_bias() -> tuple[str, list[str], list[str]]:
    """Skim several RSS feeds — returns regime, hit tags, sample headlines."""
    hits: list[str] = []
    samples: list[str] = []
    score = 0
    for feed in _FEEDS:
        try:
            titles = _rss_titles(feed)
            samples.extend(titles[:3])
            blob = " ".join(titles).lower()
            for w in _NEG:
                if w in blob:
                    score -= 1
                    hits.append(f"neg:{w}")
            for w in _POS:
                if w in blob:
                    score += 1
                    hits.append(f"pos:{w}")
        except Exception:
            continue
    # unique preserve order
    seen: set[str] = set()
    uniq_hits: list[str] = []
    for h in hits:
        if h not in seen:
            seen.add(h)
            uniq_hits.append(h)
    if score <= -3:
        return "risk_off", uniq_hits[:10], samples[:8]
    if score >= 3:
        return "risk_on", uniq_hits[:10], samples[:8]
    return "neutral", uniq_hits[:10], samples[:8]


def _kite_vix() -> tuple[float | None, float | None, str | None]:
    try:
        from kite_client import get_authenticated_kite

        kite = get_authenticated_kite()
        vix = float(kite.ltp(["NSE:INDIA VIX"])["NSE:INDIA VIX"]["last_price"])
        q = kite.quote(["NSE:INDIA VIX"]).get("NSE:INDIA VIX", {})
        prev = (q.get("ohlc") or {}).get("close")
        chg = None
        if prev:
            chg = round((vix - float(prev)) / float(prev) * 100, 2)
        return vix, chg, None
    except Exception as exc:  # noqa: BLE001
        return None, None, str(exc)


_FUT_CACHE: dict[str, Any] = {"day": "", "symbol": None}


def _fut_oi_bias() -> dict[str, Any]:
    """
    Soft smart-money proxy from Nifty futures quote when Kite is up.
    Price weakness vs prior close ≈ short pressure (big-money proxy).
    """
    out: dict[str, Any] = {
        "fut_ltp": None,
        "fut_change": None,
        "fut_oi": None,
        "smart_money": "unknown",
        "note": None,
    }
    try:
        from kite_client import get_authenticated_kite

        kite = get_authenticated_kite()
        today = datetime.now(IST).date().isoformat()
        sym = _FUT_CACHE.get("symbol") if _FUT_CACHE.get("day") == today else None
        if not sym:
            try:
                instruments = kite.instruments("NFO")
            except Exception:
                return out
            futs = [
                i
                for i in instruments
                if i.get("name") == "NIFTY"
                and i.get("instrument_type") == "FUT"
                and str(i.get("expiry") or "")[:10] >= today
            ]
            if not futs:
                return out
            futs.sort(key=lambda x: str(x.get("expiry")))
            sym = futs[0].get("tradingsymbol")
            _FUT_CACHE["day"] = today
            _FUT_CACHE["symbol"] = sym
        if not sym:
            return out
        key = f"NFO:{sym}"
        q = kite.quote([key]).get(key) or {}
        ltp = q.get("last_price")
        ohlc = q.get("ohlc") or {}
        prev = ohlc.get("close")
        oi = q.get("oi")
        chg = None
        if ltp is not None and prev:
            chg = float(ltp) - float(prev)
        out["fut_ltp"] = float(ltp) if ltp is not None else None
        out["fut_change"] = round(chg, 2) if chg is not None else None
        out["fut_oi"] = int(oi) if oi is not None else None
        out["symbol"] = sym
        if chg is not None:
            if chg <= -40:
                out["smart_money"] = "short_pressure"
                out["note"] = "Nifty fut weak vs prior close (short pressure)"
            elif chg >= 40:
                out["smart_money"] = "long_pressure"
                out["note"] = "Nifty fut firm vs prior close"
            else:
                out["smart_money"] = "balanced"
    except Exception as exc:  # noqa: BLE001
        out["note"] = f"fut_err:{exc}"
    return out


def get_macro(force: bool = False) -> dict[str, Any]:
    now = time.time()
    if not force and _CACHE["data"] and now - float(_CACHE["ts"]) < 120:
        return dict(_CACHE["data"])

    out: dict[str, Any] = {
        "ts": datetime.now(IST).isoformat(),
        "vix": None,
        "vix_change_pct": None,
        "crude": None,
        "crude_change_pct": None,
        "usdinr": None,
        "usdinr_change_pct": None,
        "gold": None,
        "gold_change_pct": None,
        "es_fut": None,
        "es_change_pct": None,
        "nq_change_pct": None,
        "regime": "neutral",
        "headline_bias": "neutral",
        "headlines": [],
        "headline_samples": [],
        "smart_money": "unknown",
        "confidence_adj": 0.0,
        "side_hint": None,  # CE | PE | None
        "notes": [],
        "factors": {},
    }

    vix, vix_chg, vix_err = _kite_vix()
    if vix is None:
        try:
            vix, vix_chg = _yahoo_chg("^INDIAVIX")
        except Exception as exc:  # noqa: BLE001
            out["notes"].append(f"vix_err:{vix_err or exc}")
    out["vix"] = vix
    out["vix_change_pct"] = vix_chg

    for key, sym in (
        ("crude", "CL=F"),
        ("usdinr", "USDINR=X"),
        ("gold", "GC=F"),
        ("es_fut", "ES=F"),
    ):
        try:
            last, chg = _yahoo_chg(sym)
            out[key] = last
            out[f"{key}_change_pct" if key != "es_fut" else "es_change_pct"] = chg
            if key == "crude":
                out["crude_change_pct"] = chg
            if key == "usdinr":
                out["usdinr_change_pct"] = chg
            if key == "gold":
                out["gold_change_pct"] = chg
        except Exception as exc:  # noqa: BLE001
            out["notes"].append(f"{key}_err:{exc}")

    try:
        _, nq_chg = _yahoo_chg("NQ=F")
        out["nq_change_pct"] = nq_chg
    except Exception as exc:  # noqa: BLE001
        out["notes"].append(f"nq_err:{exc}")

    regime, hits, samples = _headline_bias()
    out["headlines"] = hits
    out["headline_samples"] = samples
    out["headline_bias"] = regime
    out["regime"] = regime

    fut = _fut_oi_bias()
    out["smart_money"] = fut.get("smart_money") or "unknown"
    out["factors"]["nifty_fut"] = fut
    if fut.get("note"):
        out["notes"].append(str(fut["note"]))

    # FII / futures OI scanner (short build vs covering) — persist + actions
    fii_scan: dict[str, Any] = {}
    try:
        from foxtrade.fii_oi_scanner import get_fii_oi_state

        fii_scan = get_fii_oi_state()
        out["factors"]["fii_oi"] = {
            "regime": fii_scan.get("regime"),
            "smart_money": fii_scan.get("smart_money"),
            "ltp": fii_scan.get("ltp"),
            "oi": fii_scan.get("oi"),
            "oi_chg_vs_day_open": fii_scan.get("oi_chg_vs_day_open"),
            "price_chg_vs_day_open": fii_scan.get("price_chg_vs_day_open"),
            "fii_net": fii_scan.get("fii_net"),
            "fii_net_chg": fii_scan.get("fii_net_chg"),
            "actions": fii_scan.get("actions") or {},
            "saved": fii_scan.get("saved"),
        }
        out["fii_oi_regime"] = fii_scan.get("regime")
        out["fii_oi_actions"] = fii_scan.get("actions") or {}
        # Prefer OI-matrix label over soft fut±40 proxy when known
        sm_oi = str(fii_scan.get("smart_money") or "")
        if sm_oi and sm_oi not in ("unknown", "balanced"):
            out["smart_money"] = sm_oi
        for n in (fii_scan.get("actions") or {}).get("notes") or []:
            out["notes"].append(str(n))
    except Exception as exc:  # noqa: BLE001
        out["notes"].append(f"fii_oi_err:{exc}")

    adj = 0.0
    side_hint = None

    if out["vix"] and out["vix"] >= 18:
        adj -= 0.05
        out["notes"].append("elevated VIX")
        if regime != "risk_on":
            out["regime"] = "risk_off"
        side_hint = "PE"
    if out["vix"] and out["vix"] >= 22:
        adj -= 0.05
        out["notes"].append("high VIX stress")
        side_hint = "PE"

    if out.get("crude_change_pct") is not None and abs(out["crude_change_pct"]) >= 2:
        adj -= 0.03
        out["notes"].append("crude shock")
        if out["crude_change_pct"] > 0:
            out["regime"] = "risk_off"
            side_hint = side_hint or "PE"

    if out.get("usdinr_change_pct") is not None and out["usdinr_change_pct"] >= 0.4:
        adj -= 0.04
        out["notes"].append("USDINR spike (risk-off India)")
        out["regime"] = "risk_off"
        side_hint = "PE"

    if out.get("es_change_pct") is not None and out["es_change_pct"] <= -1.0:
        adj -= 0.04
        out["notes"].append("US ES futures weak overnight")
        out["regime"] = "risk_off"
        side_hint = "PE"
    elif out.get("es_change_pct") is not None and out["es_change_pct"] >= 1.0:
        adj += 0.03
        out["notes"].append("US ES futures firm")
        if out["regime"] != "risk_off":
            side_hint = side_hint or "CE"

    if out.get("smart_money") == "short_pressure":
        adj -= 0.03
        out["notes"].append("fut short pressure")
        side_hint = "PE"
    elif out.get("smart_money") == "long_pressure":
        adj += 0.02
        out["notes"].append("fut long pressure")
        if out["regime"] != "risk_off":
            side_hint = side_hint or "CE"
    elif out.get("smart_money") == "short_covering":
        adj -= 0.02
        out["notes"].append("short covering rally — CE caution")
    elif out.get("smart_money") == "long_unwinding":
        adj -= 0.03
        side_hint = "PE"
        out["notes"].append("long unwinding → PE")

    # Explicit scanner actions override soft hints
    acts = out.get("fii_oi_actions") or {}
    try:
        adj += float(acts.get("confidence_adj") or 0)
    except Exception:
        pass
    if acts.get("prefer_pe"):
        side_hint = "PE"
    elif acts.get("prefer_ce") and side_hint != "PE":
        side_hint = side_hint or "CE"
    if acts.get("side_hint") in ("CE", "PE"):
        # Don't upgrade to CE if covering-only caution without long buildup
        if acts.get("side_hint") == "CE" and acts.get("caution_ce"):
            pass
        else:
            side_hint = acts["side_hint"]

    if regime == "risk_off":
        adj -= 0.05
        side_hint = "PE"
    elif regime == "risk_on":
        adj += 0.03
        if side_hint != "PE":
            side_hint = side_hint or "CE"

    out["confidence_adj"] = round(adj, 3)
    out["side_hint"] = side_hint
    out["block_ce"] = bool(acts.get("block_ce"))
    out["caution_ce"] = bool(acts.get("caution_ce"))
    _CACHE["ts"] = now
    _CACHE["data"] = out
    return dict(out)
