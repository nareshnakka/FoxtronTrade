"""
Self-training from right/wrong entries.

Persists labeled trade examples and updates decision priors daily
to maximize future expectancy (paper + live tracked separately).
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from foxtrade.config_store import load_config, save_config

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
TRAIN_LOG = DATA / "training_log.jsonl"
LEARN_STATE = DATA / "learning_state.json"
LESSONS = DATA / "lessons.json"

# RLock: record_closed_example updates state while already holding this lock.
# A plain Lock deadlocks there (nested load/save) and freezes the trading loop.
_lock = threading.RLock()

_DEFAULT_STATE: dict[str, Any] = {
    "updated_at": None,
    "last_daily_learn": None,
    "prior_pe": 0.65,
    "algo1_min_confidence": 0.52,
    "signal_weights": {
        "bearish_candle": 1.0,
        "bullish_candle": 1.0,
        "rsi_high": 1.0,
        "rsi_low": 1.0,
        "ema_bear": 1.0,
        "ema_bull": 1.0,
        "near_resistance": 1.0,
        "near_support": 1.0,
        "macro_risk_off": 1.0,
        "macro_risk_on": 1.0,
        "pe_firming": 1.0,
        "ce_firming": 1.0,
        "ce_trap_filter": 1.0,
        "or_fade_high": 1.0,
        "or_expect_recover": 1.0,
        "or_breakdown": 1.0,
    },
    "stats": {
        "paper": {"right": 0, "wrong": 0, "pnl": 0.0},
        "live": {"right": 0, "wrong": 0, "pnl": 0.0},
    },
    "notes": [],
}


def _now() -> datetime:
    return datetime.now(IST)


def _ensure_files() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    if not TRAIN_LOG.exists():
        TRAIN_LOG.write_text("", encoding="utf-8")
    if not LEARN_STATE.exists():
        LEARN_STATE.write_text(json.dumps(_DEFAULT_STATE, indent=2), encoding="utf-8")
    if not LESSONS.exists():
        LESSONS.write_text("[]", encoding="utf-8")


def _load_learning_state_unlocked() -> dict[str, Any]:
    raw = json.loads(LEARN_STATE.read_text(encoding="utf-8"))
    state = json.loads(json.dumps(_DEFAULT_STATE))
    state.update(raw)
    sw = dict(_DEFAULT_STATE["signal_weights"])
    sw.update(raw.get("signal_weights") or {})
    state["signal_weights"] = sw
    stats = {
        "paper": dict(_DEFAULT_STATE["stats"]["paper"]),
        "live": dict(_DEFAULT_STATE["stats"]["live"]),
    }
    for mode in ("paper", "live"):
        stats[mode].update((raw.get("stats") or {}).get(mode) or {})
    state["stats"] = stats
    return state


def _save_learning_state_unlocked(state: dict[str, Any]) -> None:
    state["updated_at"] = _now().isoformat()
    LEARN_STATE.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")


def load_learning_state() -> dict[str, Any]:
    _ensure_files()
    with _lock:
        return _load_learning_state_unlocked()


def save_learning_state(state: dict[str, Any]) -> None:
    _ensure_files()
    with _lock:
        _save_learning_state_unlocked(state)


def build_entry_features(
    *,
    side: str,
    confidence: float,
    reason: str,
    index: dict[str, Any] | None,
    macro: dict[str, Any] | None,
    pair_regime: Any = None,
    gap: float | None = None,
) -> dict[str, Any]:
    index = index or {}
    macro = macro or {}
    patterns = index.get("patterns") or {}
    return {
        "side": side,
        "confidence": round(float(confidence), 4),
        "reason": reason,
        "gap": gap,
        "rsi14": index.get("rsi14"),
        "ema9": index.get("ema9"),
        "ema21": index.get("ema21"),
        "spot": index.get("spot"),
        "support": index.get("support"),
        "resistance": index.get("resistance"),
        "patterns": patterns,
        "regime": getattr(pair_regime, "regime", None) if pair_regime else None,
        "macro_regime": macro.get("regime"),
        "vix": macro.get("vix"),
        "tags": _reason_tags(reason, patterns, macro, pair_regime),
    }


def _reason_tags(
    reason: str,
    patterns: dict[str, Any],
    macro: dict[str, Any],
    pair_regime: Any,
) -> list[str]:
    tags: list[str] = []
    r = (reason or "").lower()
    if "bearish candle" in r or patterns.get("shooting_star") or patterns.get("bearish_engulf"):
        tags.append("bearish_candle")
    if "bullish" in r or patterns.get("hammer") or patterns.get("bullish_engulf"):
        tags.append("bullish_candle")
    if "rsi" in r and "high" in r:
        tags.append("rsi_high")
    if "rsi" in r and "low" in r:
        tags.append("rsi_low")
    if "ema9<ema21" in r.replace(" ", "").lower() or "ema9<ema21" in r.lower():
        tags.append("ema_bear")
    if "ema9>ema21" in r.lower():
        tags.append("ema_bull")
    if "resistance" in r:
        tags.append("near_resistance")
    if "support" in r:
        tags.append("near_support")
    if macro.get("regime") == "risk_off":
        tags.append("macro_risk_off")
    if macro.get("regime") == "risk_on":
        tags.append("macro_risk_on")
    if "pe premium firming" in r:
        tags.append("pe_firming")
    if "ce premium firming" in r:
        tags.append("ce_firming")
    if "ce trap" in r:
        tags.append("ce_trap_filter")
    if "or fade high" in r or "fade high" in r:
        tags.append("or_fade_high")
    if "or recover" in r:
        tags.append("or_expect_recover")
    if "or confirmed breakdown" in r or "confirmed breakdown" in r:
        tags.append("or_breakdown")
    if pair_regime is not None and getattr(pair_regime, "regime", None):
        tags.append(f"regime_{pair_regime.regime}")
    return tags


def label_outcome(pnl_rupees: float, exit_reason: str | None = None) -> str:
    if pnl_rupees > 0:
        return "RIGHT"
    if pnl_rupees < 0:
        return "WRONG"
    # flat — treat target hits as right-ish, stops as wrong-ish
    er = (exit_reason or "").lower()
    if "target" in er or "expand" in er:
        return "RIGHT"
    if "stop" in er or "decay" in er or "crush" in er:
        return "WRONG"
    return "FLAT"


def record_closed_example(trade: dict[str, Any]) -> dict[str, Any] | None:
    """Append labeled training row when a trade closes."""
    if trade.get("status") != "CLOSED":
        return None
    _ensure_files()
    mode = "live" if trade.get("mode") == "live" else "paper"
    pnl = float(trade.get("pnl_rupees") or 0)
    label = label_outcome(pnl, trade.get("exit_reason"))
    row = {
        "ts": _now().isoformat(),
        "trade_id": trade.get("id"),
        "algo": trade.get("algo"),
        "mode": mode,
        "symbol": trade.get("symbol"),
        "side": trade.get("side"),
        "entry_price": trade.get("entry_price"),
        "exit_price": trade.get("exit_price"),
        "pnl_rupees": pnl,
        "pnl_points": trade.get("pnl_points"),
        "exit_reason": trade.get("exit_reason"),
        "label": label,
        "features": trade.get("entry_features") or {},
        "opened_at": trade.get("opened_at"),
        "closed_at": trade.get("closed_at"),
    }
    with _lock:
        with TRAIN_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
        # Unlocked helpers: this block already holds _lock. Calling the public
        # load/save here deadlocks a plain Lock and stalls every later tick.
        state = _load_learning_state_unlocked()
        if label in ("RIGHT", "WRONG"):
            key = "right" if label == "RIGHT" else "wrong"
            state["stats"][mode][key] = int(state["stats"][mode].get(key) or 0) + 1
        state["stats"][mode]["pnl"] = round(
            float(state["stats"][mode].get("pnl") or 0) + pnl, 2
        )
        _save_learning_state_unlocked(state)
    return row


def read_training_rows(limit: int = 5000) -> list[dict[str, Any]]:
    _ensure_files()
    rows: list[dict[str, Any]] = []
    with _lock:
        text = TRAIN_LOG.read_text(encoding="utf-8")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[-limit:]


def run_daily_learn(force: bool = False) -> dict[str, Any]:
    """
    Once per calendar day (or force): adjust priors from labeled examples.
    Applies soft updates to config.json so future entries self-correct.
    """
    state = load_learning_state()
    today = _now().date().isoformat()
    if not force and state.get("last_daily_learn") == today:
        return {"skipped": True, "reason": "already learned today", "state": state}

    rows = read_training_rows()
    # use last 60 days
    cutoff = (_now() - timedelta(days=60)).isoformat()
    recent = [r for r in rows if str(r.get("closed_at") or r.get("ts") or "") >= cutoff]
    labeled = [r for r in recent if r.get("label") in ("RIGHT", "WRONG")]

    lesson: dict[str, Any] = {
        "date": today,
        "samples": len(labeled),
        "changes": [],
        "paper": {"right": 0, "wrong": 0, "pnl": 0.0},
        "live": {"right": 0, "wrong": 0, "pnl": 0.0},
    }

    if len(labeled) < 3:
        lesson["changes"].append("not enough samples (<3) — keep priors")
        state["last_daily_learn"] = today
        state["notes"] = (state.get("notes") or [])[-20:] + [f"{today}: need more samples"]
        save_learning_state(state)
        _append_lesson(lesson)
        return {"skipped": False, "lesson": lesson, "state": state}

    # Side accuracy
    pe_r = pe_w = ce_r = ce_w = 0
    tag_right: dict[str, int] = {}
    tag_wrong: dict[str, int] = {}
    for r in labeled:
        mode = "live" if r.get("mode") == "live" else "paper"
        if r["label"] == "RIGHT":
            lesson[mode]["right"] += 1
        else:
            lesson[mode]["wrong"] += 1
        lesson[mode]["pnl"] = round(lesson[mode]["pnl"] + float(r.get("pnl_rupees") or 0), 2)

        side = (r.get("side") or (r.get("features") or {}).get("side") or "").upper()
        if side == "PE":
            if r["label"] == "RIGHT":
                pe_r += 1
            else:
                pe_w += 1
        elif side == "CE":
            if r["label"] == "RIGHT":
                ce_r += 1
            else:
                ce_w += 1
        for tag in (r.get("features") or {}).get("tags") or []:
            if r["label"] == "RIGHT":
                tag_right[tag] = tag_right.get(tag, 0) + 1
            else:
                tag_wrong[tag] = tag_wrong.get(tag, 0) + 1

    # Adjust prior_pe toward historically better side
    pe_n, ce_n = pe_r + pe_w, ce_r + ce_w
    pe_wr = pe_r / pe_n if pe_n else 0.5
    ce_wr = ce_r / ce_n if ce_n else 0.5
    prior = float(state.get("prior_pe", 0.65))
    if pe_n + ce_n >= 5:
        # blend empirical PE preference
        empiric = pe_wr / (pe_wr + ce_wr) if (pe_wr + ce_wr) > 0 else 0.65
        # if CE winning more, lower prior_pe
        new_prior = 0.7 * prior + 0.3 * empiric
        new_prior = max(0.45, min(0.80, new_prior))
        if abs(new_prior - prior) >= 0.01:
            lesson["changes"].append(f"prior_pe {prior:.2f} → {new_prior:.2f} (PE WR {pe_wr:.0%} / CE WR {ce_wr:.0%})")
            prior = new_prior
        state["prior_pe"] = round(prior, 3)

    # Confidence floor: if overall WR poor, be pickier
    rights = sum(1 for r in labeled if r["label"] == "RIGHT")
    wr = rights / len(labeled)
    min_conf = float(state.get("algo1_min_confidence", 0.52))
    if wr < 0.45:
        new_c = min(0.65, min_conf + 0.03)
        lesson["changes"].append(f"min_confidence {min_conf:.2f} → {new_c:.2f} (WR {wr:.0%} weak)")
        min_conf = new_c
    elif wr > 0.60 and min_conf > 0.50:
        new_c = max(0.50, min_conf - 0.02)
        lesson["changes"].append(f"min_confidence {min_conf:.2f} → {new_c:.2f} (WR {wr:.0%} strong)")
        min_conf = new_c
    state["algo1_min_confidence"] = round(min_conf, 3)

    # Signal weight nudge
    weights = dict(state.get("signal_weights") or {})
    for tag in set(list(tag_right.keys()) + list(tag_wrong.keys())):
        if tag.startswith("regime_"):
            continue
        r_n = tag_right.get(tag, 0)
        w_n = tag_wrong.get(tag, 0)
        if r_n + w_n < 3:
            continue
        twr = r_n / (r_n + w_n)
        cur = float(weights.get(tag, 1.0))
        if twr >= 0.6:
            new_w = min(1.5, cur + 0.08)
        elif twr <= 0.4:
            new_w = max(0.5, cur - 0.08)
        else:
            continue
        if abs(new_w - cur) >= 0.01:
            weights[tag] = round(new_w, 3)
            lesson["changes"].append(f"weight {tag} {cur:.2f} → {new_w:.2f} (tag WR {twr:.0%})")
    state["signal_weights"] = weights

    # Apply into live config
    save_config(
        {
            "prior_pe": state["prior_pe"],
            "algo1_min_confidence": state["algo1_min_confidence"],
        }
    )
    state["last_daily_learn"] = today
    note = f"{today}: WR {wr:.0%} samples {len(labeled)}; " + (
        "; ".join(lesson["changes"]) if lesson["changes"] else "no param change"
    )
    state["notes"] = (state.get("notes") or [])[-30:] + [note]
    save_learning_state(state)
    _append_lesson(lesson)
    return {"skipped": False, "lesson": lesson, "state": state, "win_rate": wr}


def _append_lesson(lesson: dict[str, Any]) -> None:
    _ensure_files()
    with _lock:
        rows = json.loads(LESSONS.read_text(encoding="utf-8") or "[]")
        rows.append(lesson)
        LESSONS.write_text(json.dumps(rows[-90:], indent=2, default=str), encoding="utf-8")


def learning_report() -> dict[str, Any]:
    state = load_learning_state()
    rows = read_training_rows(2000)
    by_mode = {
        "paper": [r for r in rows if r.get("mode") != "live"],
        "live": [r for r in rows if r.get("mode") == "live"],
    }

    def summarize(rs: list[dict[str, Any]]) -> dict[str, Any]:
        right = [r for r in rs if r.get("label") == "RIGHT"]
        wrong = [r for r in rs if r.get("label") == "WRONG"]
        return {
            "samples": len(rs),
            "right": len(right),
            "wrong": len(wrong),
            "win_rate": round(len(right) / max(1, len(right) + len(wrong)) * 100, 1),
            "pnl": round(sum(float(r.get("pnl_rupees") or 0) for r in rs), 2),
            "recent": list(reversed(rs[-30:])),
        }

    lessons = []
    if LESSONS.exists():
        lessons = json.loads(LESSONS.read_text(encoding="utf-8") or "[]")

    # ensure daily learn attempt when report opened after session
    learn_result = run_daily_learn(force=False)

    return {
        "state": load_learning_state(),
        "paper": summarize(by_mode["paper"]),
        "live": summarize(by_mode["live"]),
        "lessons": list(reversed(lessons[-14:])),
        "learn_result": {
            "skipped": learn_result.get("skipped"),
            "reason": learn_result.get("reason"),
            "changes": (learn_result.get("lesson") or {}).get("changes"),
        },
    }


def apply_learned_weights(cfg: dict[str, Any]) -> dict[str, Any]:
    """Merge learning_state into runtime config dict (non-persistent overlay)."""
    state = load_learning_state()
    out = dict(cfg)
    out["prior_pe"] = state.get("prior_pe", cfg.get("prior_pe", 0.65))
    out["algo1_min_confidence"] = state.get(
        "algo1_min_confidence", cfg.get("algo1_min_confidence", 0.52)
    )
    out["signal_weights"] = state.get("signal_weights") or {}
    # Side-pace rule: CE slow grind / PE fast dump
    rules = state.get("rules") or {}
    side_pace = rules.get("side_pace") or {}
    if side_pace.get("enabled", True):
        out["side_pace_rule_enabled"] = True
        for k in (
            "ce_entry_min_hold_minutes",
            "pe_entry_min_hold_minutes",
            "ce_entry_min_hold_loss_points",
            "ce_entry_min_hold_loss_points_low_conf",
            "pe_entry_min_hold_loss_points",
            "pe_entry_min_hold_loss_points_low_conf",
            "ce_trail_giveback_pct",
            "pe_trail_giveback_pct",
            "ce_atm_trail_giveback_pct",
            "pe_atm_trail_giveback_pct",
            "ce_signal_persist_minutes",
            "pe_signal_persist_minutes",
        ):
            if k in side_pace and side_pace[k] is not None:
                out[k] = side_pace[k]
    elif "enabled" in side_pace:
        out["side_pace_rule_enabled"] = False
    return out
