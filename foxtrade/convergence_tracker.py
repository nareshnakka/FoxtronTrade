"""
Dynamic multi-strike convergence tracking.

At/after open: track ALL CE/PE premiums in band (current + next week).
Until a pair converges at least once, keep ranking/switching candidates.
If the active set decays, rotate to the next closest converging set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from foxtrade.atm_diag_merge_lock import (
    chain_merge_ok,
    cross_atm_x_ok,
    pick_highest_band_pair,
)
from foxtrade.late_start import dig_day_history, is_late_start
from foxtrade.scanner import (
    expiry_allowed_for_entry,
    is_converged,
    resolve_atm_wing_instruments,
    scan_premium_band,
)

IST = ZoneInfo("Asia/Kolkata")


def _pair_id(pair: dict[str, Any]) -> str:
    ce = pair["ce"]["tradingsymbol"]
    pe = pair["pe"]["tradingsymbol"]
    return f"{ce}|{pe}"


def score_pair_merge(
    pair: dict[str, Any],
    *,
    low: float,
    high: float,
    index: dict[str, Any] | None = None,
) -> float:
    """
    Rank converging CE/PE sets when several merge in-band.

    Prefer:
      1) tight gap (real merge)
      2) highest merge mid inside the premium band
      3) strike near index support / resistance (lock that set)
      4) same-strike over cross-strike
    """
    gap = float(pair.get("gap") or 99)
    mid = float(pair.get("mid") or 0)
    score = 0.0

    if pair.get("converged"):
        score += 100.0
    elif gap <= 5:
        score += 40.0
    elif gap <= 10:
        score += 20.0

    # Highest merge value within band (e.g. ~140 beats ~126)
    band = max(high - low, 1.0)
    if low <= mid <= high:
        score += 35.0 * ((mid - low) / band)
    elif mid > high:
        score += 10.0  # slightly above band — still interesting
    else:
        score -= 15.0

    score += max(0.0, 25.0 - gap)  # tighter gap → higher

    if pair.get("same_strike"):
        score += 10.0  # mild tie-break only; high-mid convergence must win

    idx = index or {}
    strike = pair.get("strike")
    support = idx.get("support")
    resistance = idx.get("resistance")
    spot = idx.get("spot")
    if strike is not None and (support is not None or resistance is not None):
        dists: list[float] = []
        if support is not None:
            dists.append(abs(float(strike) - float(support)))
        if resistance is not None:
            dists.append(abs(float(strike) - float(resistance)))
        near = min(dists) if dists else 1e9
        # Within ~150 pts of S or R → strong lock preference
        if near <= 150:
            score += 30.0 * (1.0 - near / 150.0)
        # Prefer S/R that spot is pressing
        if spot is not None and resistance is not None and spot >= float(resistance) * 0.998:
            if abs(float(strike) - float(resistance)) <= 100:
                score += 12.0
        if spot is not None and support is not None and spot <= float(support) * 1.002:
            if abs(float(strike) - float(support)) <= 100:
                score += 12.0

    atm = idx.get("atm") or (round(float(spot) / 50) * 50 if spot else None)
    if strike is not None and atm is not None:
        score += max(0.0, 8.0 - abs(float(strike) - float(atm)) / 50.0)

    return round(score, 2)


def _build_all_pairs(
    candidates: list[dict[str, Any]],
    low: float,
    high: float,
) -> list[dict[str, Any]]:
    """All same-expiry CE/PE combos with mid in/near band; prefer same strike."""
    by_exp: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for c in candidates:
        by_exp.setdefault(c["expiry"], {"CE": [], "PE": []})[c["instrument_type"]].append(c)

    pairs: list[dict[str, Any]] = []
    seen: set[str] = set()

    # 1) same-strike pairs
    for exp, sides in by_exp.items():
        by_strike: dict[float, dict[str, dict[str, Any]]] = {}
        for side in ("CE", "PE"):
            for c in sides[side]:
                by_strike.setdefault(float(c["strike"]), {})[side] = c
        for strike, legs in by_strike.items():
            if "CE" not in legs or "PE" not in legs:
                continue
            ce, pe = legs["CE"], legs["PE"]
            gap = abs(ce["ltp"] - pe["ltp"])
            mid = (ce["ltp"] + pe["ltp"]) / 2
            # BOTH legs must sit in/near the premium band (no 135 PE + 325 CE junk)
            pad = 10.0
            if not (
                (low - pad) <= ce["ltp"] <= (high + pad)
                and (low - pad) <= pe["ltp"] <= (high + pad)
            ):
                continue
            if not (low - 15 <= mid <= high + 15):
                continue
            p = {
                "expiry": exp,
                "strike": strike,
                "ce": dict(ce),
                "pe": dict(pe),
                "gap": round(gap, 2),
                "mid": round(mid, 2),
                "same_strike": True,
            }
            pid = _pair_id(p)
            if pid not in seen:
                seen.add(pid)
                pairs.append(p)

    # 2) cross-strike same expiry (premium-matched) — always included.
    # The 125–145 band is usually hit by OTM legs at different strikes, so this
    # is the realistic convergence set, not a fallback.
    for exp, sides in by_exp.items():
        for ce in sides["CE"]:
            for pe in sides["PE"]:
                if float(ce["strike"]) == float(pe["strike"]):
                    continue
                gap = abs(ce["ltp"] - pe["ltp"])
                mid = (ce["ltp"] + pe["ltp"]) / 2
                if not (low <= mid <= high):
                    continue
                if gap > 25:
                    continue
                p = {
                    "expiry": exp,
                    "strike": None,
                    "ce": dict(ce),
                    "pe": dict(pe),
                    "gap": round(gap, 2),
                    "mid": round(mid, 2),
                    "same_strike": False,
                    "ce_strike": float(ce["strike"]),
                    "pe_strike": float(pe["strike"]),
                }
                pid = _pair_id(p)
                if pid not in seen:
                    seen.add(pid)
                    pairs.append(p)

    # Highest merge first so downstream ranking sees the best pair first.
    pairs.sort(
        key=lambda x: (
            -(x.get("mid") or 0) if x.get("gap", 99) <= 5 else 0,
            x["gap"],
            0 if x.get("same_strike") else 1,
        )
    )
    return pairs


@dataclass
class ConvergenceTracker:
    universe: list[dict[str, Any]] = field(default_factory=list)  # instruments
    pairs: list[dict[str, Any]] = field(default_factory=list)
    active: dict[str, Any] | None = None
    active_id: str | None = None
    converged_once: dict[str, bool] = field(default_factory=dict)  # pair_id -> True
    decayed_ids: set[str] = field(default_factory=set)
    switch_reason: str = ""
    last_scan_meta: dict[str, Any] = field(default_factory=dict)
    locked_for_trade: bool = False  # don't rotate while algos holding this pair
    locked_merge: bool = False  # hold best merge/S-R pick until decay or mid leaves band
    late_report: dict[str, Any] | None = None
    _late_dig_done: bool = False
    wing_instruments: list[dict[str, Any]] = field(default_factory=list)
    merge_gate: dict[str, Any] = field(default_factory=dict)
    _wing_atm: int | None = None

    def _refresh_wing(self, atm: int | float | None, cfg: dict[str, Any]) -> None:
        if atm is None:
            return
        atm_i = int(round(float(atm) / 50) * 50)
        if self._wing_atm == atm_i and self.wing_instruments:
            return
        depth = int(cfg.get("atm_diag_wing_depth", 2))
        exp = self.last_scan_meta.get("current_expiry")
        try:
            self.wing_instruments = resolve_atm_wing_instruments(
                atm_i, expiry=exp, depth=depth
            )
            self._wing_atm = atm_i
        except Exception:
            self.wing_instruments = []

    def _eval_merge_gate(
        self, quotes: dict[str, float], cfg: dict[str, Any], atm: int | float | None
    ) -> dict[str, Any]:
        if not cfg.get("atm_diag_merge_gate_enabled", True):
            gate = {"ok": True, "reason": "gate disabled", "enabled": False}
            self.merge_gate = gate
            return gate
        if atm is None:
            # derive from spot quote if present
            spot = quotes.get("NSE:NIFTY 50") or quotes.get("NIFTY 50")
            if spot is None and self.last_scan_meta.get("spot") is not None:
                spot = self.last_scan_meta.get("spot")
            if spot is not None:
                atm = round(float(spot) / 50) * 50
        if atm is None:
            gate = {"ok": False, "reason": "no ATM/spot yet", "enabled": True}
            self.merge_gate = gate
            return gate
        atm_i = int(round(float(atm) / 50) * 50)
        self._refresh_wing(atm_i, cfg)
        legs: dict[str, Any] = {}
        for w in self.wing_instruments:
            sym = w["tradingsymbol"]
            px = quotes.get(sym)
            if px is None and w.get("ltp") is not None:
                px = w.get("ltp")
            if px is None:
                continue
            w["ltp"] = float(px)
            strike = int(float(w["strike"]))
            legs[f"{w['instrument_type']}:{strike}"] = {
                "c": float(px),
                "ltp": float(px),
                "sym": sym,
            }
        # also fold universe premiums for band pick
        for u in self.universe:
            sym = u.get("tradingsymbol")
            px = quotes.get(sym) if sym else None
            if px is None:
                px = u.get("ltp")
            if px is None or u.get("strike") is None:
                continue
            legs[f"{u['instrument_type']}:{int(float(u['strike']))}"] = {
                "c": float(px),
                "ltp": float(px),
                "sym": sym,
            }
        gate = chain_merge_ok(
            atm=atm_i,
            legs=legs,
            atm_tol=float(cfg.get("atm_diag_atm_tol", 3.0)),
            diag_tol=float(cfg.get("atm_diag_diag_tol", 5.0)),
            diag_depths=tuple(range(1, int(cfg.get("atm_diag_wing_depth", 2)) + 1)),
            require_itm=bool(cfg.get("atm_diag_require_itm", False)),
        )
        gate["enabled"] = True
        gate["atm"] = atm_i
        gate["legs_n"] = len(legs)
        # Always expose ATM premiums for lean side-confirm (even if diagonals not merged)
        if gate.get("ce_atm") is None:
            ce_atm_leg = legs.get(f"CE:{atm_i}")
            pe_atm_leg = legs.get(f"PE:{atm_i}")
            if ce_atm_leg and pe_atm_leg:
                gate["ce_atm"] = float(ce_atm_leg.get("c") or ce_atm_leg.get("ltp") or 0) or None
                gate["pe_atm"] = float(pe_atm_leg.get("c") or pe_atm_leg.get("ltp") or 0) or None
                if gate["ce_atm"] == 0:
                    gate["ce_atm"] = None
                if gate["pe_atm"] == 0:
                    gate["pe_atm"] = None
        band_lo = float(cfg.get("premium_low", 125))
        band_hi = float(cfg.get("premium_high", 145))
        equal_tol = float(cfg.get("atm_diag_equal_tol", 3.0))
        if gate.get("ok"):
            pair = pick_highest_band_pair(
                legs=legs,
                band_lo=band_lo,
                band_hi=band_hi,
                equal_tol=equal_tol,
            )
            gate["pick"] = pair
        else:
            gate["pick"] = None
            # Expiry→next-week: if ATM same-strike not merged, still try cross-ATM X
            if cfg.get("atm_diag_cross_x_enabled", True):
                x_gate = cross_atm_x_ok(
                    atm=atm_i,
                    legs=legs,
                    step=50,
                    tol=float(cfg.get("atm_diag_cross_x_tol", equal_tol)),
                    depths=tuple(
                        int(x)
                        for x in (cfg.get("atm_diag_cross_x_depths") or [1, 2, 3, 4])
                    ),
                    band_lo=band_lo,
                    band_hi=band_hi,
                )
                gate["cross_x"] = x_gate
                if x_gate.get("ok") and x_gate.get("pair"):
                    gate["ok"] = True
                    gate["reason"] = x_gate.get("reason")
                    gate["pick"] = x_gate["pair"]
                    gate["via"] = "cross_atm_x"
        self.merge_gate = gate
        return gate

    def bootstrap(self, cfg: dict[str, Any], *, dig_history: bool = True) -> dict[str, Any]:
        """Full rescan — load strikes; if late login, dig day history from 09:15."""
        low = float(cfg["premium_low"])
        high = float(cfg["premium_high"])
        late = is_late_start(cfg)
        # Late: also keep expanded/crushed legs so history dig can see the morning path
        # Late dig may LOOK at a wider premium window for history, but the live
        # trade universe / active pair must stay in premium_low–premium_high.
        wide_low = float(cfg.get("late_wide_low", 45)) if late else low
        wide_high = float(cfg.get("late_wide_high", 220)) if late else high
        scan = scan_premium_band(
            premium_low=low,
            premium_high=high,
            wide_low=wide_low,
            wide_high=wide_high,
        )
        candidates = list(scan.get("candidates") or [])
        by_sym = {u["tradingsymbol"]: u for u in self.universe}
        for c in candidates:
            by_sym[c["tradingsymbol"]] = c
        allowed = [
            u
            for u in by_sym.values()
            if expiry_allowed_for_entry(u.get("expiry"), symbol=u.get("tradingsymbol"))
        ]

        def _core_legs(legs: list[dict[str, Any]]) -> list[dict[str, Any]]:
            core = [c for c in legs if c.get("in_core_band")]
            if not core:
                core = [
                    c
                    for c in legs
                    if low - 5 <= float(c.get("ltp") or 0) <= high + 5
                ]
            return core or [
                c for c in legs if low - 10 <= float(c.get("ltp") or 0) <= high + 10
            ]

        # Dig history on wide set (sees morning 125–145 that later expanded)
        if dig_history and late and not self._late_dig_done:
            self.universe = allowed
            self.pairs = _build_all_pairs(_core_legs(allowed), low, high)
            self._apply_late_dig(cfg)
            self._late_dig_done = True

        # Live trade universe: CORE 125–145 only
        self.universe = _core_legs(allowed)
        self.pairs = _build_all_pairs(self.universe, low, high)
        trade_week = scan.get("current_expiry")  # single allowed week for today
        expiry_day = bool(scan.get("expiry_day"))
        # Always stick to today's single trade week (current week, or next week on expiry day)
        if trade_week:
            tw = str(trade_week)[:10]
            self.universe = [
                u for u in self.universe if str(u.get("expiry") or "")[:10] == tw
            ]
            self.pairs = [
                p
                for p in self.pairs
                if str(p.get("expiry") or (p.get("ce") or {}).get("expiry") or "")[:10] == tw
            ]
        # Clear active if wrong week / 0DTE / left band
        if self.active and not expiry_allowed_for_entry(
            self.active.get("expiry"),
            symbol=(self.active.get("ce") or {}).get("tradingsymbol")
            or (self.active.get("pe") or {}).get("tradingsymbol"),
        ):
            act_exp = str(
                self.active.get("expiry")
                or (self.active.get("ce") or {}).get("expiry")
                or ""
            )[:10]
            self.active = None
            self.active_id = None
            self.locked_merge = False
            self.switch_reason = (
                f"cleared wrong-week {act_exp} — trade week is {trade_week}"
            )
        if self.active:
            ce_lp = float((self.active.get("ce") or {}).get("ltp") or 0)
            pe_lp = float((self.active.get("pe") or {}).get("ltp") or 0)
            mid = (ce_lp + pe_lp) / 2 if ce_lp and pe_lp else 0
            if not (
                (low - 10) <= ce_lp <= (high + 10)
                and (low - 10) <= pe_lp <= (high + 10)
                and (low - 15) <= mid <= (high + 15)
            ):
                self.active = None
                self.active_id = None
                self.locked_merge = False
                self.switch_reason = "cleared out-of-band pair — stay in 125–145"
        self.last_scan_meta = {
            "spot": scan.get("spot"),
            "atm": scan.get("atm"),
            "current_expiry": scan.get("current_expiry"),
            "next_expiry": scan.get("next_expiry"),
            "expiry_day": expiry_day,
            "candidate_count": len(self.universe),
            "pair_count": len(self.pairs),
            "late_start": late,
        }
        self._refresh_wing(scan.get("atm"), cfg)

        if not self.active and self.pairs:
            # Gate checked on first quotes tick; bootstrap may preselect tentatively
            self._select_best(low, high, reason="bootstrap best merge/S-R")
        elif self.active:
            self._refresh_active_quotes()
        return self.snapshot(cfg)

    def _apply_late_dig(self, cfg: dict[str, Any]) -> None:
        low = float(cfg["premium_low"])
        high = float(cfg["premium_high"])
        report = dig_day_history(self.universe, cfg)
        self.late_report = {
            "late": report.late,
            "minutes_since_open": report.minutes_since_open,
            "message": report.message,
            "recommended_id": report.recommended_id,
            "paths": [
                {
                    "id": p.pair_id,
                    "strike": p.strike,
                    "expiry": p.expiry,
                    "min_gap": p.min_gap,
                    "last_gap": p.last_gap,
                    "converged": p.converged,
                    "first_converge_ts": p.first_converge_ts,
                    "decayed": p.decayed,
                    "expanded_side": p.expanded_side,
                    "score": round(p.score, 1),
                    "reasons": p.reasons,
                    "ce": p.ce["tradingsymbol"],
                    "pe": p.pe["tradingsymbol"],
                    "ce_open": p.ce_open,
                    "pe_open": p.pe_open,
                    "ce_last": p.ce_last,
                    "pe_last": p.pe_last,
                }
                for p in report.paths[:12]
            ],
        }
        for p in report.paths:
            if p.converged:
                self.converged_once[p.pair_id] = True
            if p.decayed:
                self.decayed_ids.add(p.pair_id)

        # Choose recommended pair if present in live pairs / rebuild from path
        # Never adopt today's expiry; never adopt legs that left the 125–145 band
        def _ltps_in_band(ce_lp: Any, pe_lp: Any) -> bool:
            try:
                c, p = float(ce_lp), float(pe_lp)
            except (TypeError, ValueError):
                return False
            pad = 10.0
            mid = (c + p) / 2
            return (
                (low - pad) <= c <= (high + pad)
                and (low - pad) <= p <= (high + pad)
                and (low - 15) <= mid <= (high + 15)
            )

        def _path_ok(path: Any) -> bool:
            if path.decayed or path.pair_id in self.decayed_ids:
                return False
            if getattr(path, "expanded_side", None):
                return False  # already blew out of premium range
            if not expiry_allowed_for_entry(
                path.expiry or (path.ce or {}).get("expiry"),
                symbol=(path.ce or {}).get("tradingsymbol"),
            ):
                return False
            # Prefer live LTP on instruments; fall back to last dig prints
            ce_lp = (path.ce or {}).get("ltp", path.ce_last)
            pe_lp = (path.pe or {}).get("ltp", path.pe_last)
            return _ltps_in_band(ce_lp, pe_lp)

        def _pair_in_band(p: dict[str, Any] | None) -> bool:
            if not p:
                return False
            return _ltps_in_band(
                (p.get("ce") or {}).get("ltp"),
                (p.get("pe") or {}).get("ltp"),
            )

        chosen = None
        if report.recommended_id:
            chosen = next((p for p in self.pairs if _pair_id(p) == report.recommended_id), None)
            if chosen is not None and not _pair_in_band(chosen):
                chosen = None
            if chosen is None:
                path = next((p for p in report.paths if p.pair_id == report.recommended_id), None)
                if path and _path_ok(path):
                    chosen = {
                        "expiry": path.expiry,
                        "strike": path.strike,
                        "ce": dict(path.ce),
                        "pe": dict(path.pe),
                        "gap": path.last_gap,
                        "mid": path.last_mid,
                        "same_strike": path.strike is not None,
                        "id": path.pair_id,
                    }

        if (
            chosen
            and chosen.get("id", _pair_id(chosen)) not in self.decayed_ids
            and expiry_allowed_for_entry(
                chosen.get("expiry"),
                symbol=(chosen.get("ce") or {}).get("tradingsymbol"),
            )
            and _pair_in_band(chosen)
        ):
            self.active = chosen
            self.active_id = chosen.get("id") or _pair_id(chosen)
            self.switch_reason = report.message
        else:
            # skip decayed / expanded / out-of-band; else live in-band merge
            picked = False
            for path in report.paths:
                if not _path_ok(path):
                    continue
                self.active = {
                    "expiry": path.expiry,
                    "strike": path.strike,
                    "ce": dict(path.ce),
                    "pe": dict(path.pe),
                    "gap": path.last_gap,
                    "mid": path.last_mid,
                    "same_strike": path.strike is not None,
                    "id": path.pair_id,
                }
                self.active_id = path.pair_id
                self.switch_reason = f"late dig in-band: {report.message}"
                picked = True
                break
            if not picked or not _pair_in_band(self.active):
                self.active = None
                self.active_id = None
                self._select_best(low, high, reason="late dig out-of-band — live 125–145 merge")

    def instruments_to_watch(self) -> list[dict[str, Any]]:
        by_sym = {u["tradingsymbol"]: u for u in self.universe}
        for w in self.wing_instruments:
            by_sym.setdefault(w["tradingsymbol"], w)
        if self.active:
            for leg in ("ce", "pe"):
                x = self.active.get(leg) or {}
                sym = x.get("tradingsymbol")
                if sym and sym not in by_sym:
                    by_sym[sym] = x
        return list(by_sym.values())

    def on_quotes(
        self,
        quotes: dict[str, float],
        cfg: dict[str, Any],
        *,
        decay_active_pair: bool = False,
        trade_flat: bool = True,
        index: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Update LTPs, re-rank pairs, lock best merge / S-R set."""
        low = float(cfg["premium_low"])
        high = float(cfg["premium_high"])
        tol = float(cfg.get("convergence_tol", 2.0))
        idx = index or {}
        if idx.get("atm") is None and self.last_scan_meta.get("atm") is not None:
            idx = {**idx, "atm": self.last_scan_meta.get("atm")}

        # Update universe LTPs; drop legs that left the trade band hard
        # Core screen is premium_low/high; allow small drift only (not 300+ ITM junk)
        still: list[dict[str, Any]] = []
        band_lo = low - 20
        band_hi = high + 25
        for inst in self.universe:
            sym = inst["tradingsymbol"]
            if sym in quotes:
                inst["ltp"] = float(quotes[sym])
            lp = inst.get("ltp")
            if lp is None:
                continue
            if band_lo <= lp <= band_hi:
                inst["in_core_band"] = low <= lp <= high
                still.append(inst)
        if still:
            self.universe = still

        trade_week = str(self.last_scan_meta.get("current_expiry") or "")[:10]
        self.pairs = [
            p
            for p in _build_all_pairs(self.universe, low, high)
            if expiry_allowed_for_entry(
                p.get("expiry") or (p.get("ce") or {}).get("expiry"),
                symbol=(p.get("ce") or {}).get("tradingsymbol"),
            )
            and (
                not trade_week
                or str(p.get("expiry") or (p.get("ce") or {}).get("expiry") or "")[:10]
                == trade_week
            )
        ]
        # annotate live status + merge score
        for p in self.pairs:
            ce_lp = quotes.get(p["ce"]["tradingsymbol"], p["ce"].get("ltp"))
            pe_lp = quotes.get(p["pe"]["tradingsymbol"], p["pe"].get("ltp"))
            if ce_lp is not None:
                p["ce"]["ltp"] = float(ce_lp)
            if pe_lp is not None:
                p["pe"]["ltp"] = float(pe_lp)
            p["gap"] = round(abs(p["ce"]["ltp"] - p["pe"]["ltp"]), 2)
            p["mid"] = round((p["ce"]["ltp"] + p["pe"]["ltp"]) / 2, 2)
            p["converged"] = is_converged(p["ce"]["ltp"], p["pe"]["ltp"], tol, low, high)
            pid = _pair_id(p)
            p["id"] = pid
            if p["converged"]:
                self.converged_once[pid] = True
            p["converged_once"] = bool(self.converged_once.get(pid))
            p["decayed"] = pid in self.decayed_ids
            p["merge_score"] = score_pair_merge(p, low=low, high=high, index=idx)

        # Best merge first (highest score), then gap as tie-break
        self.pairs.sort(
            key=lambda x: (
                0 if x["id"] not in self.decayed_ids else 1,
                -(x.get("merge_score") or 0),
                x["gap"],
            )
        )

        self.locked_for_trade = not trade_flat

        # Always evaluate ATM+diagonal gate (UI + selection)
        atm_now = idx.get("atm") or self.last_scan_meta.get("atm") or self._wing_atm
        gate = self._eval_merge_gate(quotes, cfg, atm_now)
        equal_tol = float(cfg.get("atm_diag_equal_tol", max(tol, 3.0)))

        # Never keep / trade today's expiry as active (expiry-day → next week)
        if self.active and not expiry_allowed_for_entry(
            self.active.get("expiry") or (self.active.get("ce") or {}).get("expiry"),
            symbol=(self.active.get("ce") or {}).get("tradingsymbol")
            or (self.active.get("pe") or {}).get("tradingsymbol"),
        ):
            self.active = None
            self.active_id = None
            self.locked_merge = False
            self.switch_reason = "dropped expiry-day active — select next week"

        # No merge lock before algo2_merge_after (default 09:20 IST)
        merge_after = str(cfg.get("algo2_merge_after") or "09:20")
        now_hm = datetime.now(IST).strftime("%H:%M")
        if now_hm < merge_after and not self.locked_for_trade:
            self.active = None
            self.active_id = None
            self.locked_merge = False
            self.switch_reason = f"wait merge after {merge_after} (now {now_hm})"
            return self.snapshot(cfg)

        if self.locked_for_trade:
            self._refresh_active_from_list()
            # If locked pair left the premium band (e.g. same-strike CE blew to 300+),
            # stick to best in-band pair that still contains an open leg when possible.
            if self.active and not (
                (low - 10) <= float((self.active.get("ce") or {}).get("ltp") or 0) <= (high + 10)
                and (low - 10) <= float((self.active.get("pe") or {}).get("ltp") or 0) <= (high + 10)
            ):
                better = next(
                    (
                        p
                        for p in self.pairs
                        if p["id"] not in self.decayed_ids
                        and (low - 10) <= p.get("mid", 0) <= (high + 10)
                    ),
                    None,
                )
                if better:
                    self.active = better
                    self.active_id = better["id"]
                    self.switch_reason = (
                        f"locked trade — show in-band pair K{better.get('strike')} "
                        f"mid {better.get('mid')}"
                    )
                    return self.snapshot(cfg)
            self.switch_reason = (
                "locked — trade open on active pair"
                + (f" · gate {'OK' if gate.get('ok') else 'WAIT'}" if gate.get("enabled") is not False else "")
            )
            return self.snapshot(cfg)

        # Mark active as decayed if requested
        if decay_active_pair and self.active_id:
            self.decayed_ids.add(self.active_id)
            self.switch_reason = "active pair decay — rotating"
            self.active = None
            self.active_id = None
            self.locked_merge = False

        alive = [p for p in self.pairs if p["id"] not in self.decayed_ids]
        in_band = [p for p in alive if low <= p.get("mid", 0) <= high]
        near_equal = [p for p in in_band if p.get("gap", 99) <= equal_tol]
        # Prefer gate pick if it maps to a known pair
        gate_pick = gate.get("pick") if gate.get("ok") else None
        top = None
        if gate.get("ok") and near_equal:
            top = max(near_equal, key=lambda p: (p.get("mid") or 0, -(p.get("gap") or 99)))
            if gate_pick:
                # Prefer exact CE/PE strikes from pick when present in pairs
                matched = next(
                    (
                        p
                        for p in near_equal
                        if int(float(p.get("ce_strike") or p.get("strike") or 0))
                        == int(gate_pick.get("ce_strike") or -1)
                        and int(float(p.get("pe_strike") or p.get("strike") or 0))
                        == int(gate_pick.get("pe_strike") or -1)
                    ),
                    None,
                )
                if matched:
                    top = matched
                elif abs(float(top.get("mid") or 0) - float(gate_pick.get("mid") or 0)) > 1.0:
                    # Build synthetic cross-strike pair from universe if needed
                    ce_u = next(
                        (
                            u
                            for u in self.universe
                            if u.get("instrument_type") == "CE"
                            and int(float(u.get("strike") or 0)) == int(gate_pick["ce_strike"])
                        ),
                        None,
                    )
                    pe_u = next(
                        (
                            u
                            for u in self.universe
                            if u.get("instrument_type") == "PE"
                            and int(float(u.get("strike") or 0)) == int(gate_pick["pe_strike"])
                        ),
                        None,
                    )
                    if ce_u and pe_u:
                        syn = {
                            "expiry": ce_u.get("expiry"),
                            "strike": None,
                            "ce": dict(ce_u),
                            "pe": dict(pe_u),
                            "gap": gate_pick["gap"],
                            "mid": gate_pick["mid"],
                            "same_strike": False,
                            "ce_strike": gate_pick["ce_strike"],
                            "pe_strike": gate_pick["pe_strike"],
                            "converged": True,
                            "merge_score": 200.0,
                        }
                        syn["id"] = _pair_id(syn)
                        top = syn

        cur = next((p for p in self.pairs if p["id"] == self.active_id), None) if self.active_id else None
        if cur is None and self.active:
            cur = self.active

        gate_required = bool(cfg.get("atm_diag_merge_gate_required", False))
        if not gate.get("ok") and gate_required:
            # Legacy: wait for ATM+diagonal merge before any band lock
            if self.locked_merge and cur and (low - 10) <= cur.get("mid", 0) <= (high + 10):
                self.active = cur
                self.switch_reason = (
                    f"holding lock — waiting re-merge ({gate.get('reason')})"
                )
            else:
                self.locked_merge = False
                self.switch_reason = f"scan wait: {gate.get('reason') or 'ATM+diag not merged'}"
            return self.snapshot(cfg)

        # Soft / trained path: lock clear 125–145 band merge even if ATM still wide
        if not gate.get("ok") and top is None and near_equal:
            top = max(near_equal, key=lambda p: (p.get("mid") or 0, -(p.get("gap") or 99)))
        if not gate.get("ok") and top is None:
            band_pick = pick_highest_band_pair(
                legs={
                    f"{u.get('instrument_type')}:{int(float(u.get('strike') or 0))}": {
                        "c": float(u.get("ltp") or 0),
                        "sym": u.get("tradingsymbol") or "",
                    }
                    for u in self.universe
                    if u.get("instrument_type") in ("CE", "PE") and u.get("ltp") is not None
                },
                band_lo=low,
                band_hi=high,
                equal_tol=equal_tol,
            )
            if band_pick:
                ce_u = next(
                    (
                        u
                        for u in self.universe
                        if u.get("instrument_type") == "CE"
                        and int(float(u.get("strike") or 0)) == int(band_pick["ce_strike"])
                    ),
                    None,
                )
                pe_u = next(
                    (
                        u
                        for u in self.universe
                        if u.get("instrument_type") == "PE"
                        and int(float(u.get("strike") or 0)) == int(band_pick["pe_strike"])
                    ),
                    None,
                )
                if ce_u and pe_u:
                    top = {
                        "expiry": ce_u.get("expiry"),
                        "strike": None,
                        "ce": dict(ce_u),
                        "pe": dict(pe_u),
                        "gap": band_pick["gap"],
                        "mid": band_pick["mid"],
                        "same_strike": False,
                        "ce_strike": band_pick["ce_strike"],
                        "pe_strike": band_pick["pe_strike"],
                        "converged": True,
                        "merge_score": 180.0,
                    }
                    top["id"] = _pair_id(top)

        if top is not None:
            hold = bool(
                self.locked_merge
                and cur
                and cur.get("id") not in self.decayed_ids
                and (low - 10) <= cur.get("mid", 0) <= (high + 10)
                and abs(float(cur.get("mid") or 0) - float(top.get("mid") or 0)) < 8
            )
            how = "ATM+diag" if gate.get("ok") else "band"
            if hold:
                self.active = cur
                self.switch_reason = (
                    f"{how} LOCK mid {cur.get('mid')} gap {cur.get('gap')} "
                    f"CE{cur.get('ce_strike') or cur.get('strike')} / "
                    f"PE{cur.get('pe_strike') or cur.get('strike')}"
                )
                return self.snapshot(cfg)

            self.active = top
            self.active_id = top.get("id") or _pair_id(top)
            top["id"] = self.active_id
            self.locked_merge = True
            self.switch_reason = (
                f"{how} LOCK mid {top.get('mid')} gap {top.get('gap')} "
                f"CE{top.get('ce_strike') or top.get('strike')}@"
                f"{(top.get('ce') or {}).get('ltp')} "
                f"PE{top.get('pe_strike') or top.get('strike')}@"
                f"{(top.get('pe') or {}).get('ltp')}"
            )
            return self.snapshot(cfg)

        # Gate OK but no near-equal in-band pair yet — try synthetic from pick
        if gate.get("ok") and gate_pick and top is None:
            ce_u = next(
                (
                    u
                    for u in self.universe
                    if u.get("instrument_type") == "CE"
                    and int(float(u.get("strike") or 0)) == int(gate_pick["ce_strike"])
                ),
                None,
            )
            pe_u = next(
                (
                    u
                    for u in self.universe
                    if u.get("instrument_type") == "PE"
                    and int(float(u.get("strike") or 0)) == int(gate_pick["pe_strike"])
                ),
                None,
            )
            if ce_u and pe_u:
                top = {
                    "expiry": ce_u.get("expiry"),
                    "strike": None,
                    "ce": dict(ce_u),
                    "pe": dict(pe_u),
                    "gap": gate_pick["gap"],
                    "mid": gate_pick["mid"],
                    "same_strike": False,
                    "ce_strike": gate_pick["ce_strike"],
                    "pe_strike": gate_pick["pe_strike"],
                    "converged": True,
                    "merge_score": 200.0,
                }
                top["id"] = _pair_id(top)
                self.active = top
                self.active_id = top["id"]
                self.locked_merge = True
                self.switch_reason = (
                    f"ATM+diag LOCK mid {top['mid']} gap {top['gap']} "
                    f"CE{top['ce_strike']} PE{top['pe_strike']}"
                )
                return self.snapshot(cfg)

        self.switch_reason = (
            f"ATM+diag merge OK but no near-equal pair in {low:g}–{high:g} "
            f"(tol {equal_tol:g})"
        )
        return self.snapshot(cfg)

    def _select_best(
        self,
        low: float,
        high: float,
        reason: str,
        index: dict[str, Any] | None = None,
    ) -> None:
        pool = [
            p
            for p in self.pairs
            if p.get("id") not in self.decayed_ids
            and (low - 15) <= p.get("mid", 0) <= (high + 15)
        ]
        if not pool:
            self.active = self.pairs[0] if self.pairs else None
            self.active_id = _pair_id(self.active) if self.active else None
            self.locked_merge = False
            self.switch_reason = reason + " (fallback)"
            return
        for p in pool:
            p["merge_score"] = score_pair_merge(p, low=low, high=high, index=index)
        top = max(pool, key=lambda p: (p.get("merge_score") or 0, -p.get("gap", 99)))
        self.active = top
        self.active_id = top.get("id") or _pair_id(top)
        self.locked_merge = False
        self.switch_reason = (
            f"{reason}: K{top.get('strike')} mid {top.get('mid')} score {top.get('merge_score')}"
        )

    def _refresh_active_from_list(self) -> None:
        if not self.active_id:
            return
        cur = next((p for p in self.pairs if p["id"] == self.active_id), None)
        if cur:
            self.active = cur

    def _refresh_active_quotes(self) -> None:
        if not self.active:
            return
        pid = _pair_id(self.active)
        self.active_id = pid
        self.active["id"] = pid

    def mark_decay_active(self) -> None:
        if self.active_id:
            self.decayed_ids.add(self.active_id)

    def snapshot(self, cfg: dict[str, Any]) -> dict[str, Any]:
        low = float(cfg["premium_low"])
        high = float(cfg["premium_high"])
        return {
            "spot": self.last_scan_meta.get("spot"),
            "atm": self.last_scan_meta.get("atm"),
            "current_expiry": self.last_scan_meta.get("current_expiry"),
            "next_expiry": self.last_scan_meta.get("next_expiry"),
            "expiry_day": self.last_scan_meta.get("expiry_day"),
            "candidates": self.universe,
            "pairs": self.pairs[:20],
            "pair": self.active,
            "active_id": self.active_id,
            "switch_reason": self.switch_reason,
            "converged_once_ids": list(self.converged_once.keys()),
            "decayed_ids": list(self.decayed_ids),
            "tracked_legs": len(self.universe),
            "tracked_pairs": len(self.pairs),
            "locked_merge": self.locked_merge,
            "merge_gate": self.merge_gate,
            "band": {"low": low, "high": high},
            "late_start": self.last_scan_meta.get("late_start"),
            "late_report": self.late_report,
            "ts": datetime.now(IST).isoformat(),
        }
