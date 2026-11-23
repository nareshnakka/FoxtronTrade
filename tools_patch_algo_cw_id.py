"""One-shot: parameterize AlgoCwDual with algo_id."""
from pathlib import Path

p = Path("foxtrade/algo_cw_dual.py")
text = p.read_text(encoding="utf-8")

old_init = '''class AlgoCwDual:
    """Watch CE/PE merge → buy BREAKOUT side only → book profit (no fallen leg)."""

    def __init__(self) -> None:
        self.state = AlgoCwState()
'''

new_init = '''class AlgoCwDual:
    """Watch CE/PE merge → buy both (or breakout) → book at pair-exit mult."""

    def __init__(
        self,
        algo_id: str = "algo_cw",
        *,
        display_name: str | None = None,
        week_mode: str = "current",
    ) -> None:
        self.algo_id = algo_id
        self.display_name = display_name or (
            "Algo-NW" if algo_id == "algo_nw" else "Algo-CW"
        )
        self.week_mode = week_mode if week_mode in ("current", "next") else "current"
        self.state = AlgoCwState()

    def resolve_cfg(self, cfg: dict[str, Any]) -> dict[str, Any]:
        """Overlay next-week profile (band 46–54 · 1.70× · max 1 setup)."""
        if self.algo_id != "algo_nw":
            return cfg
        out = dict(cfg)
        out["premium_low"] = float(cfg.get("algo_nw_premium_low", 46) or 46)
        out["premium_high"] = float(cfg.get("algo_nw_premium_high", 54) or 54)
        out["algo_cw_pair_exit_mult"] = float(cfg.get("algo_nw_pair_exit_mult", 1.70) or 1.70)
        out["algo_cw_max_setups_per_day"] = int(cfg.get("algo_nw_max_setups_per_day", 1) or 1)
        out["algo_cw_enabled"] = bool(cfg.get("algo_nw_enabled", True))
        out["algo_cw_second_entry_after"] = "23:59"
        out["algo_cw_last_entry_before"] = "00:00"
        out["algo_cw_skip_second_if_day_pnl_ge"] = 0.0
        out["algo_cw_index_channel_hold"] = bool(cfg.get("algo_nw_index_channel_hold", False))
        out["algo_cw_pair_exit_wait_retrace"] = False
        if cfg.get("algo_nw_lots") is not None:
            out["algo_cw_lots"] = int(cfg.get("algo_nw_lots") or out.get("algo_cw_lots") or 1)
        if cfg.get("algo_nw_dual_size_from_capital") is not None:
            out["algo_nw_dual_size_from_capital"] = bool(
                cfg.get("algo_nw_dual_size_from_capital")
            )
            out["algo_cw_dual_size_from_capital"] = out["algo_nw_dual_size_from_capital"]
        return out
'''

if old_init not in text:
    raise SystemExit("init block not found")
text = text.replace(old_init, new_init, 1)

text = text.replace('"algo_cw"', "self.algo_id")
text = text.replace('algo_id: str = self.algo_id', 'algo_id: str = "algo_cw"')
text = text.replace('if algo_id == self.algo_id', 'if algo_id == "algo_nw"')
text = text.replace('if self.algo_id != self.algo_id:', 'if self.algo_id != "algo_nw":')

text = text.replace(
    'cfg = ctx["config"]',
    'cfg = self.resolve_cfg(ctx["config"])',
    1,
)

old_week = '''        scan = ctx.get("scan") or {}
        trade_week = str(scan.get("current_expiry") or "")[:10]
        if trade_week and str(pair_exp or "")[:10] != trade_week:
            self.state.last_message = (
                f"skip wrong week {pair_exp} — trade week is {trade_week}"
            )
            return
'''
new_week = '''        scan = ctx.get("scan") or {}
        if self.week_mode == "next":
            trade_week = str(scan.get("next_expiry") or scan.get("expiry") or "")[:10]
        else:
            trade_week = str(scan.get("current_expiry") or "")[:10]
        if trade_week and str(pair_exp or "")[:10] != trade_week:
            self.state.last_message = (
                f"skip wrong week {pair_exp} — want {self.week_mode}={trade_week}"
            )
            return
'''
if old_week not in text:
    raise SystemExit("week gate not found — check if already patched")
text = text.replace(old_week, new_week, 1)

text = text.replace(
    '"name": "Algo-CW",',
    '"name": self.display_name,',
    1,
)

# restore runtime key — may already be self.algo_id from replace
text = text.replace(
    'saved = (runtime or {}).get(self.algo_id) or {}',
    'saved = (runtime or {}).get(self.algo_id) or {}',
)

p.write_text(text, encoding="utf-8")
print("OK patched", p)
assert 'algo_id: str = "algo_cw"' in text
assert 'if self.algo_id != "algo_nw"' in text
assert 'cfg = self.resolve_cfg(ctx["config"])' in text
assert "want {self.week_mode}" in text
print("checks passed")
