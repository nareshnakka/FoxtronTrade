from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parent
STATE = ROOT / "data" / "learning_state.json"
LESSONS = ROOT / "data" / "lessons.jsonl"


def main() -> None:
    state = {}
    if STATE.exists():
        state = json.loads(STATE.read_text(encoding="utf-8"))
    notes = list(state.get("notes") or [])[-40:]
    notes.append(
        "2026-10-26: weak-est (+75%) CE post-decay partial reclaim (-14 vs open) "
        "stabbed -35k in 1m. Block shallow under-open reclaim when est<200; "
        "weak-est confirm 5m; true spot OHLC for decay."
    )
    rules = dict(state.get("rules") or {})
    rules["post_decay_weak_est_partial_reclaim"] = {
        "enabled": True,
        "strong_est_pct": 200.0,
        "align_pts": 35.0,
        "weak_confirm_minutes": 5,
        "ce_max_under_open": 150.0,
        "learned_from": "2026-10-26",
        "rule": (
            "WAIT CE post-decay if est<strong and 0>vs_open>-align (dead-cat); "
            "fix spot_open to session open"
        ),
    }
    state["notes"] = notes
    state["rules"] = rules
    state["updated_at"] = datetime.now(IST).isoformat()
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")

    lesson = {
        "date": "2026-10-26",
        "ts": datetime.now(IST).isoformat(),
        "source": "post_decay_knife_train",
        "samples": 1,
        "changes": [
            "post_decay_strong_est_pct=200",
            "post_decay_weak_est_confirm_minutes=5",
            "post_decay_ce_max_under_open=150",
            "decay update_live true spot OHLC",
            "WAIT weak-est partial reclaim under open",
        ],
        "paper": {"right": 0, "wrong": 1, "pnl": -35833.5},
        "insight": (
            "ce_half_day_crush est +75% entered 09:16 on partial reclaim "
            "(-14 vs open); 09:17 continuation knife. Require open reclaim "
            "for weak-est shallow under-open; longer confirm."
        ),
    }
    with LESSONS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(lesson) + "\n")
    print("lesson saved", STATE, LESSONS)


if __name__ == "__main__":
    main()
