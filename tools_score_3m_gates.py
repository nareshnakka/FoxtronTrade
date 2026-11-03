"""Score candidate entry gates on 3m PA loss lessons JSON."""
from __future__ import annotations

import json
from pathlib import Path

j = json.loads(Path("data/backtests/pa_loss_lessons_3m.json").read_text(encoding="utf-8"))
L, W = j["losses"], j["wins"]
ALL = L + W


def score(name, fn):
    bl = sum(1 for r in L if not fn(r))
    bw = sum(1 for r in W if not fn(r))
    kn = sum(r["net"] for r in ALL if fn(r))
    print(f"{name:40} block L {bl}/{len(L)} W {bw}/{len(W)} kept_net {kn:+}")


print("PE knife (give_pe):")
for thr in (8, 10, 12, 15, 18, 20):
    score(
        f"PE give_pe>={thr}",
        lambda r, t=thr: not (r["side"] == "PE" and r["give_pe"] < t),
    )

print("\nCE vs_open:")
for thr in (25, 28, 30, 35, 40):
    score(
        f"CE vs>={thr}",
        lambda r, t=thr: not (r["side"] == "CE" and r["vs"] < t),
    )

print("\neff:")
for thr in (0.15, 0.20, 0.25, 0.30):
    score(f"eff>={thr}", lambda r, t=thr: r["eff40"] >= t)

print("\nmom10 align:")
for thr in (6, 8, 10, 12):
    score(
        f"|mom|>={thr}",
        lambda r, t=thr: (r["side"] == "CE" and r["mom10"] >= t)
        or (r["side"] == "PE" and r["mom10"] <= -t),
    )

print("\nPE not below OR:")
score("PE not below_or", lambda r: not (r["side"] == "PE" and r["below_or"]))

print("\nCombos:")
score(
    "PE_g>=12 + CE_vs>=30 + eff>=0.15",
    lambda r: (r["side"] != "PE" or r["give_pe"] >= 12)
    and (r["side"] != "CE" or r["vs"] >= 30)
    and r["eff40"] >= 0.15,
)
score(
    "PE_g>=15 + CE_vs>=30",
    lambda r: (r["side"] != "PE" or r["give_pe"] >= 15)
    and (r["side"] != "CE" or r["vs"] >= 30),
)
score(
    "PE_g>=12 + CE_vs>=30",
    lambda r: (r["side"] != "PE" or r["give_pe"] >= 12)
    and (r["side"] != "CE" or r["vs"] >= 30),
)
score(
    "PE_g>=12 + CE_vs>=28 + |mom|>=8",
    lambda r: (r["side"] != "PE" or r["give_pe"] >= 12)
    and (r["side"] != "CE" or r["vs"] >= 28)
    and abs(r["mom10"]) >= 8,
)
score(
    "PE_g>=15 + CE_vs>=30 + eff>=0.20",
    lambda r: (r["side"] != "PE" or r["give_pe"] >= 15)
    and (r["side"] != "CE" or r["vs"] >= 30)
    and r["eff40"] >= 0.20,
)
score(
    "PE_g>=12 + CE_vs>=30 + no_PE_belowOR",
    lambda r: (r["side"] != "PE" or (r["give_pe"] >= 12 and not r["below_or"]))
    and (r["side"] != "CE" or r["vs"] >= 30),
)

print("\nOpposed-day PE losses (day finished UP) / CE (day DOWN):")
for r in L:
    if (r["side"] == "CE" and r["day_oc"] is not None and r["day_oc"] < -20) or (
        r["side"] == "PE" and r["day_oc"] is not None and r["day_oc"] > 20
    ):
        print(
            f"  {r['date']} {r['side']} net {r['net']:+} day_oc {r['day_oc']} "
            f"vs {r['vs']} givePE {r['give_pe']} giveCE {r['give_ce']} belowOR {r['below_or']}"
        )
