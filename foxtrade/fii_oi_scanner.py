"""
FII / futures OI scanner — short build-up vs short covering.

What it does
------------
1. Snapshot Nifty futures LTP + OI (Kite) intraday.
2. Classify vs prior snapshot / day open:
     price↑ + OI↑  → long_buildup
     price↑ + OI↓  → short_covering   (shorts shrinking)
     price↓ + OI↑  → short_buildup    (fresh shorts)
     price↓ + OI↓  → long_unwinding
3. Best-effort NSE participant-wise OI (EOD FII net) when available.
4. Persist every scan to data/fii_oi/ and emit trade actions for the algo.

Never blocks the process on network failure — returns unknown + no action.
"""
from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data" / "fii_oi"
LATEST = DATA_DIR / "latest.json"
HISTORY = DATA_DIR / "history.jsonl"
PARTICIPANT = DATA_DIR / "participant_latest.json"

_FUT_SYM_CACHE: dict[str, Any] = {"day": "", "symbol": None}
_LAST_SCAN_TS = 0.0
_LAST_SCAN: dict[str, Any] = {}


def _now() -> datetime:
    return datetime.now(IST)


def _day() -> str:
    return _now().date().isoformat()


def _ensure_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _append_history(row: dict[str, Any]) -> None:
    _ensure_dir()
    with HISTORY.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _save_latest(row: dict[str, Any]) -> None:
    _ensure_dir()
    LATEST.write_text(json.dumps(row, indent=2), encoding="utf-8")


def _nifty_fut_quote() -> dict[str, Any]:
    """Live Nifty current-month futures LTP + OI via Kite."""
    out: dict[str, Any] = {
        "symbol": None,
        "ltp": None,
        "prev_close": None,
        "oi": None,
        "oi_day_high": None,
        "oi_day_low": None,
        "volume": None,
        "error": None,
    }
    try:
        from kite_client import get_authenticated_kite

        kite = get_authenticated_kite()
        today = _day()
        sym = _FUT_SYM_CACHE.get("symbol") if _FUT_SYM_CACHE.get("day") == today else None
        if not sym:
            instruments = kite.instruments("NFO")
            futs = [
                i
                for i in instruments
                if i.get("name") == "NIFTY"
                and i.get("instrument_type") == "FUT"
                and str(i.get("expiry") or "")[:10] >= today
            ]
            if not futs:
                out["error"] = "no_nifty_fut"
                return out
            futs.sort(key=lambda x: str(x.get("expiry")))
            sym = futs[0].get("tradingsymbol")
            _FUT_SYM_CACHE["day"] = today
            _FUT_SYM_CACHE["symbol"] = sym
        if not sym:
            out["error"] = "no_symbol"
            return out
        key = f"NFO:{sym}"
        q = kite.quote([key]).get(key) or {}
        ohlc = q.get("ohlc") or {}
        out["symbol"] = sym
        out["ltp"] = float(q["last_price"]) if q.get("last_price") is not None else None
        out["prev_close"] = float(ohlc["close"]) if ohlc.get("close") is not None else None
        out["oi"] = int(q["oi"]) if q.get("oi") is not None else None
        out["oi_day_high"] = int(q["oi_day_high"]) if q.get("oi_day_high") is not None else None
        out["oi_day_low"] = int(q["oi_day_low"]) if q.get("oi_day_low") is not None else None
        out["volume"] = int(q["volume"]) if q.get("volume") is not None else None
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
    return out


def classify_oi_price(
    *,
    price_chg: float | None,
    oi_chg: float | None,
    price_eps: float = 15.0,
    oi_eps: float = 0.0,
) -> str:
    """
    Classic OI × price matrix.
    Returns: long_buildup | short_covering | short_buildup | long_unwinding | chop | unknown
    """
    if price_chg is None or oi_chg is None:
        return "unknown"
    up = price_chg >= price_eps
    dn = price_chg <= -price_eps
    oi_up = oi_chg > oi_eps
    oi_dn = oi_chg < -oi_eps
    if up and oi_up:
        return "long_buildup"
    if up and oi_dn:
        return "short_covering"
    if dn and oi_up:
        return "short_buildup"
    if dn and oi_dn:
        return "long_unwinding"
    return "chop"


def _fetch_nse_participant_oi() -> dict[str, Any]:
    """
    Best-effort NSE participant OI (index futures).
    NSE often needs cookies; failures are soft.
    """
    out: dict[str, Any] = {
        "ok": False,
        "fii_long": None,
        "fii_short": None,
        "fii_net": None,
        "dii_net": None,
        "pro_net": None,
        "client_net": None,
        "as_of": None,
        "error": None,
        "source": None,
    }
    # Prefer locally dropped/cached participant file
    cached = _load_json(PARTICIPANT)
    if cached.get("fii_net") is not None and cached.get("as_of") == _day():
        cached["ok"] = True
        cached["source"] = cached.get("source") or "cache_today"
        return cached

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) FoxtronTrade/1.0",
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://www.nseindia.com/",
    }
    urls = [
        "https://www.nseindia.com/api/participant-oi",
        "https://www.nseindia.com/api/fiidiiTradeReact",
    ]
    # Warm-up cookie jar
    try:
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
        req0 = urllib.request.Request("https://www.nseindia.com", headers=headers)
        opener.open(req0, timeout=6).read()
        for url in urls:
            try:
                req = urllib.request.Request(url, headers=headers)
                raw = opener.open(req, timeout=8).read().decode("utf-8", errors="ignore")
                payload = json.loads(raw)
                parsed = _parse_participant_payload(payload)
                if parsed.get("fii_net") is not None or parsed.get("ok"):
                    parsed["source"] = url
                    parsed["as_of"] = parsed.get("as_of") or _day()
                    _ensure_dir()
                    PARTICIPANT.write_text(json.dumps(parsed, indent=2), encoding="utf-8")
                    return parsed
            except Exception as exc:  # noqa: BLE001
                out["error"] = f"{url}:{exc}"
                continue
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)

    # Fall back to last known participant file (may be prior day)
    if cached.get("fii_net") is not None:
        cached["ok"] = True
        cached["source"] = "cache_stale"
        cached["stale"] = True
        return cached
    return out


def _parse_participant_payload(payload: Any) -> dict[str, Any]:
    """Normalize various NSE participant OI JSON shapes."""
    out: dict[str, Any] = {
        "ok": False,
        "fii_long": None,
        "fii_short": None,
        "fii_net": None,
        "dii_net": None,
        "pro_net": None,
        "client_net": None,
        "as_of": None,
        "error": None,
        "source": None,
    }
    rows = []
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        for key in ("data", "dataFO", "participantOI", "result"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
        if not rows and "fii" in {str(k).lower() for k in payload.keys()}:
            rows = [payload]

    def _num(v: Any) -> float | None:
        try:
            if v is None or v == "":
                return None
            return float(str(v).replace(",", ""))
        except Exception:
            return None

    for r in rows:
        if not isinstance(r, dict):
            continue
        # Heuristic field names
        client = str(
            r.get("client_type")
            or r.get("Client Type")
            or r.get("participant")
            or r.get("category")
            or ""
        ).upper()
        # Index futures long/short
        fl = _num(
            r.get("future_index_long")
            or r.get("Fut Index Long")
            or r.get("indexFuturesLong")
            or r.get("long")
        )
        fs = _num(
            r.get("future_index_short")
            or r.get("Fut Index Short")
            or r.get("indexFuturesShort")
            or r.get("short")
        )
        net = None
        if fl is not None and fs is not None:
            net = fl - fs
        else:
            net = _num(r.get("net") or r.get("net_oi") or r.get("Net"))

        if "FII" in client or "FPI" in client:
            out["fii_long"] = fl
            out["fii_short"] = fs
            out["fii_net"] = net
        elif "DII" in client:
            out["dii_net"] = net
        elif "PRO" in client:
            out["pro_net"] = net
        elif "CLIENT" in client:
            out["client_net"] = net

    if out["fii_net"] is not None:
        out["ok"] = True
    return out


def _actions_for(
    regime: str,
    *,
    fii_net: float | None,
    fii_net_chg: float | None,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Map scan → algo actions.
    """
    cfg = cfg or {}
    block_ce = False
    prefer_pe = False
    prefer_ce = False
    caution_ce = False
    conf_adj = 0.0
    notes: list[str] = []

    if regime == "short_buildup":
        # Brief short_buildup alone is a haircut/prefer-PE signal — hard CE
        # block only after sustained minutes (applied by scan_fii_oi) or when
        # fii_oi_block_ce_on_short_build is forced true for legacy.
        prefer_pe = True
        conf_adj -= 0.05
        if bool(cfg.get("fii_oi_block_ce_on_short_build", False)):
            block_ce = True
            notes.append("OI short_buildup → block CE / prefer PE")
        else:
            notes.append("OI short_buildup → prefer PE (CE haircut, no hard block yet)")
    elif regime == "short_covering":
        # Covering rally = fuel not foundation — allow CE only cautiously
        caution_ce = True
        conf_adj -= 0.02
        notes.append("OI short_covering → CE caution (not fresh longs)")
        # If FII net short is shrinking hard, covering is real
        if fii_net_chg is not None and fii_net_chg > 0:
            notes.append("FII net short shrinking (covering)")
            conf_adj += 0.01  # slight offset — covering confirmed institutional
    elif regime == "long_buildup":
        prefer_ce = True
        conf_adj += 0.04
        notes.append("OI long_buildup → prefer CE")
    elif regime == "long_unwinding":
        prefer_pe = True
        conf_adj -= 0.03
        notes.append("OI long_unwinding → prefer PE")

    # Absolute FII net is often structurally short (hedge). Don't block CE on
    # level alone — only act on *fresh* shorting / covering Δ, or short_buildup.
    if fii_net is not None and fii_net <= float(cfg.get("fii_deep_short_net", -150_000)):
        notes.append(f"FII deep net short ({fii_net:,.0f})")
        if regime == "short_buildup":
            prefer_pe = True
            block_ce = block_ce or bool(
                cfg.get("fii_oi_block_ce_when_fii_deep_short", True)
            )
        elif regime in ("long_unwinding",):
            prefer_pe = True
        elif regime == "short_covering":
            caution_ce = True
        # chop / unknown / long_buildup: informational only
    if fii_net_chg is not None and fii_net_chg <= float(cfg.get("fii_fresh_short_chg", -20_000)):
        prefer_pe = True
        prefer_ce = False
        block_ce = True
        conf_adj -= 0.04
        notes.append(f"FII fresh shorting Δnet={fii_net_chg:,.0f}")
    if fii_net_chg is not None and fii_net_chg >= float(cfg.get("fii_cover_chg", 20_000)):
        notes.append(f"FII covering Δnet=+{fii_net_chg:,.0f}")
        if regime == "short_covering":
            caution_ce = True

    if prefer_ce and prefer_pe:
        # Resolve conflict: structure OI wins unless fresh FII shorts
        if block_ce:
            prefer_ce = False
        else:
            prefer_pe = False

    if prefer_pe and not prefer_ce:
        hint = "PE"
    elif prefer_ce:
        hint = "CE"
    else:
        hint = None

    return {
        "block_ce": block_ce,
        "prefer_pe": prefer_pe,
        "prefer_ce": prefer_ce,
        "caution_ce": caution_ce,
        "confidence_adj": round(conf_adj, 3),
        "notes": notes,
        "side_hint": hint,
    }


def scan_fii_oi(
    *,
    force: bool = False,
    cfg: dict[str, Any] | None = None,
    min_interval_sec: float = 60.0,
) -> dict[str, Any]:
    """
    Run one scan, persist, return full state + actions.
    Cached briefly unless force=True.
    """
    global _LAST_SCAN_TS, _LAST_SCAN
    cfg = cfg or {}
    now = time.time()
    if (
        not force
        and _LAST_SCAN
        and now - _LAST_SCAN_TS < min_interval_sec
    ):
        return dict(_LAST_SCAN)

    prev = _load_json(LATEST)
    quote = _nifty_fut_quote()
    ltp = quote.get("ltp")
    prev_close = quote.get("prev_close")
    oi = quote.get("oi")

    # Day-open baseline stored once
    day = _day()
    day_open_px = prev.get("day_open_px") if prev.get("day") == day else None
    day_open_oi = prev.get("day_open_oi") if prev.get("day") == day else None
    if day_open_px is None and ltp is not None:
        day_open_px = float(ltp)
    if day_open_oi is None and oi is not None:
        day_open_oi = int(oi)

    # Prefer compare vs last snapshot same day; else vs day open / prev close
    ref_px = prev.get("ltp") if prev.get("day") == day else day_open_px
    ref_oi = prev.get("oi") if prev.get("day") == day else day_open_oi
    if ref_px is None and prev_close is not None:
        ref_px = float(prev_close)

    price_chg = (float(ltp) - float(ref_px)) if ltp is not None and ref_px is not None else None
    oi_chg = (int(oi) - int(ref_oi)) if oi is not None and ref_oi is not None else None
    price_chg_day = (
        (float(ltp) - float(day_open_px)) if ltp is not None and day_open_px is not None else None
    )
    oi_chg_day = (
        (int(oi) - int(day_open_oi)) if oi is not None and day_open_oi is not None else None
    )
    price_chg_vs_prev = (
        (float(ltp) - float(prev_close)) if ltp is not None and prev_close is not None else None
    )

    price_eps = float(cfg.get("fii_oi_price_eps", 20.0))
    # OI eps in contracts — ignore tiny noise
    oi_eps = float(cfg.get("fii_oi_chg_eps", 5000))

    regime = classify_oi_price(
        price_chg=price_chg_day if price_chg_day is not None else price_chg,
        oi_chg=oi_chg_day if oi_chg_day is not None else oi_chg,
        price_eps=price_eps,
        oi_eps=oi_eps,
    )

    # Participant (EOD) — soft
    part = _fetch_nse_participant_oi() if cfg.get("fii_oi_fetch_participant", True) else {}
    fii_net = part.get("fii_net")
    prev_fii = prev.get("fii_net")
    fii_net_chg = None
    if fii_net is not None and prev_fii is not None:
        try:
            fii_net_chg = float(fii_net) - float(prev_fii)
        except Exception:
            fii_net_chg = None
    # If same-day participant unchanged, try stored prior history last different
    if fii_net_chg is None and fii_net is not None and HISTORY.exists():
        try:
            lines = HISTORY.read_text(encoding="utf-8").strip().splitlines()[-40:]
            for line in reversed(lines):
                row = json.loads(line)
                if row.get("fii_net") is None:
                    continue
                if row.get("day") != day and float(row["fii_net"]) != float(fii_net):
                    fii_net_chg = float(fii_net) - float(row["fii_net"])
                    break
        except Exception:
            pass

    actions = _actions_for(
        regime, fii_net=fii_net, fii_net_chg=fii_net_chg, cfg=cfg
    )

    # Sustained short_buildup → escalate to hard CE block (cuts brief false tags)
    sustained_need = int(cfg.get("fii_oi_sustained_short_min", 45))
    sb_since = prev.get("short_buildup_since") if prev.get("day") == day else None
    if regime == "short_buildup":
        if not sb_since:
            sb_since = _now().isoformat()
        try:
            started = datetime.fromisoformat(str(sb_since))
            if started.tzinfo is None:
                started = started.replace(tzinfo=_now().tzinfo)
            held_min = (_now() - started).total_seconds() / 60.0
        except Exception:
            held_min = 0.0
        # Only escalate when price is still firmly down (not an SR reclaim bounce).
        firmly_down = (
            price_chg_day is not None
            and float(price_chg_day) <= -(2.0 * price_eps)
        )
        if (
            held_min >= sustained_need
            and firmly_down
            and not actions.get("block_ce")
        ):
            actions = dict(actions)
            actions["block_ce"] = True
            actions["prefer_pe"] = True
            notes = list(actions.get("notes") or [])
            notes.append(
                f"OI short_buildup sustained {held_min:.0f}m + dpx "
                f"{float(price_chg_day):+.0f} → block CE"
            )
            actions["notes"] = notes
        elif held_min >= sustained_need and not firmly_down:
            notes = list(actions.get("notes") or [])
            notes.append(
                f"OI short_buildup {held_min:.0f}m but reclaiming "
                f"(dpx {float(price_chg_day or 0):+.0f}) → CE haircut only"
            )
            actions = dict(actions)
            actions["notes"] = notes
    else:
        sb_since = None

    # Map to legacy smart_money labels used by algo1
    if regime == "short_buildup":
        smart = "short_pressure"
    elif regime == "long_buildup":
        smart = "long_pressure"
    elif regime == "short_covering":
        smart = "short_covering"
    elif regime == "long_unwinding":
        smart = "long_unwinding"
    else:
        smart = "balanced" if regime == "chop" else "unknown"

    row: dict[str, Any] = {
        "ts": _now().isoformat(),
        "day": day,
        "symbol": quote.get("symbol"),
        "ltp": ltp,
        "prev_close": prev_close,
        "oi": oi,
        "day_open_px": day_open_px,
        "day_open_oi": day_open_oi,
        "price_chg_vs_snapshot": round(price_chg, 2) if price_chg is not None else None,
        "oi_chg_vs_snapshot": oi_chg,
        "price_chg_vs_day_open": round(price_chg_day, 2) if price_chg_day is not None else None,
        "oi_chg_vs_day_open": oi_chg_day,
        "price_chg_vs_prev_close": round(price_chg_vs_prev, 2)
        if price_chg_vs_prev is not None
        else None,
        "regime": regime,
        "short_buildup_since": sb_since,
        "smart_money": smart,
        "fii_net": fii_net,
        "fii_long": part.get("fii_long"),
        "fii_short": part.get("fii_short"),
        "fii_net_chg": fii_net_chg,
        "participant_ok": bool(part.get("ok")),
        "participant_source": part.get("source"),
        "participant_error": part.get("error"),
        "quote_error": quote.get("error"),
        "actions": actions,
        "saved": True,
    }

    # Persist (skip duplicate identical regime spam more than every ~60s already)
    try:
        _save_latest(row)
        _append_history(
            {
                "ts": row["ts"],
                "day": day,
                "ltp": ltp,
                "oi": oi,
                "regime": regime,
                "smart_money": smart,
                "fii_net": fii_net,
                "fii_net_chg": fii_net_chg,
                "actions": actions,
            }
        )
    except Exception as exc:  # noqa: BLE001
        row["saved"] = False
        row["save_error"] = str(exc)

    _LAST_SCAN_TS = now
    _LAST_SCAN = row
    return dict(row)


def get_fii_oi_state(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Public helper for macro / engine — always returns a dict."""
    if cfg is not None and cfg.get("fii_oi_scan_enabled", True) is False:
        return {"regime": "disabled", "actions": {}, "smart_money": "unknown"}
    try:
        return scan_fii_oi(cfg=cfg or {})
    except Exception as exc:  # noqa: BLE001
        return {
            "regime": "unknown",
            "smart_money": "unknown",
            "actions": {},
            "error": str(exc),
        }


def ingest_participant_manual(
    *,
    fii_long: float,
    fii_short: float,
    as_of: str | None = None,
    dii_net: float | None = None,
) -> dict[str, Any]:
    """Allow manual / scripted drop of NSE participant numbers."""
    as_of = as_of or _day()
    row = {
        "ok": True,
        "fii_long": float(fii_long),
        "fii_short": float(fii_short),
        "fii_net": float(fii_long) - float(fii_short),
        "dii_net": dii_net,
        "as_of": as_of,
        "source": "manual",
        "stale": False,
    }
    _ensure_dir()
    PARTICIPANT.write_text(json.dumps(row, indent=2), encoding="utf-8")
    return row
