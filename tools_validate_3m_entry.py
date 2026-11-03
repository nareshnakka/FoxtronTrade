"""Validate new entry gates from 3m loss autopsy on 90d / 30d / full."""
from tools_validate_pa_gates import run

# Current locked base (from prior PE lessons)
CUR = {
    "daily_pa_pe_min_vs_open": 35,
    "daily_pa_pe_entry_after_minutes": 75,
    "daily_pa_ce_max_giveback": 55,
    "daily_pa_min_path_efficiency": 0.0,
    "daily_pa_max_orb_width_for_pb": 999,
    "daily_pa_pb_min_giveback": 0,
    "daily_pa_pb_max_giveback": 999,
    "daily_pa_ce_min_vs_open": 20,
    "daily_pa_pe_min_giveback": 0,
}

profiles = [
    ("current", {}),
    ("eff20", {"daily_pa_min_path_efficiency": 0.20}),
    ("eff25", {"daily_pa_min_path_efficiency": 0.25}),
    ("ce_vs28", {"daily_pa_ce_min_vs_open": 28}),
    ("ce_vs30", {"daily_pa_ce_min_vs_open": 30}),
    ("pe_bounce12", {"daily_pa_pe_min_giveback": 12}),
    ("pe_bounce15", {"daily_pa_pe_min_giveback": 15}),
    (
        "lean_right_entry",
        {
            "daily_pa_min_path_efficiency": 0.20,
            "daily_pa_ce_min_vs_open": 28,
            "daily_pa_pe_min_giveback": 12,
        },
    ),
    (
        "lean_plus",
        {
            "daily_pa_min_path_efficiency": 0.25,
            "daily_pa_ce_min_vs_open": 30,
            "daily_pa_pe_min_giveback": 15,
        },
    ),
    (
        "knife_ce",
        {
            "daily_pa_ce_min_vs_open": 28,
            "daily_pa_pe_min_giveback": 12,
        },
    ),
]


def main() -> int:
    for name, over in profiles:
        print("---", name)
        cfg = {**CUR, **over}
        run("90d", days_n=90, **cfg)
        run("30d", days_n=30, **cfg)
        run("full", all_avail=True, **cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
