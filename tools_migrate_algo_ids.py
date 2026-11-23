"""Migrate historical trades + patch leftover UI labels after Algo-CW/NW rename."""
from __future__ import annotations

import json
import re
from pathlib import Path

from foxtrade.algo_ids import canon

ROOT = Path(__file__).resolve().parent


def migrate_trades() -> int:
    p = ROOT / "data" / "trades.json"
    rows = json.loads(p.read_text(encoding="utf-8"))
    n = 0
    for t in rows:
        a = t.get("algo")
        c = canon(a)
        if a and c != a:
            t["algo"] = c
            n += 1
    p.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return n


def patch_ui() -> None:
    dash = ROOT / "static" / "dashboard.html"
    t = dash.read_text(encoding="utf-8")
    t = t.replace("Convergence CW —", "Algo-CW —")
    t = t.replace("Convergence NW —", "Algo-NW —")
    t = t.replace("Next-Week Convergence capital ₹", "Algo-NW capital ₹")
    t = t.replace(
        "Convergence books buy CE+PE on merge; NW is a separate 46–54 @ 1.70× book.",
        "Algo-CW / Algo-NW buy CE+PE on merge; Algo-NW is a separate 46–54 @ 1.70× book.",
    )
    t = t.replace('`<strong>CW</strong>', '`<strong>Algo-CW</strong>')
    t = t.replace('`<strong>NW</strong>', '`<strong>Algo-NW</strong>')
    t = t.replace(
        '`CW ₹${a2cap.toLocaleString("en-IN")} · NW ₹${nwcap.toLocaleString("en-IN")}`',
        '`Algo-CW ₹${a2cap.toLocaleString("en-IN")} · Algo-NW ₹${nwcap.toLocaleString("en-IN")}`',
    )
    t = t.replace(" · NW ₹", " · Algo-NW ₹")
    dash.write_text(t, encoding="utf-8")

    rep = ROOT / "static" / "reports.html"
    t = rep.read_text(encoding="utf-8")
    t = t.replace(">Algo-CW CW<", ">Algo-CW<")
    t = t.replace(">Algo-CW NW<", ">Algo-NW<")
    t = t.replace('algo2: "Algo-CW CW"', 'algo_cw: "Algo-CW"')
    t = t.replace('algo_nw: "Algo-CW NW"', 'algo_nw: "Algo-NW"')
    t = t.replace("all | algo1 | algo2 | algo_nw", "all | algo1 | algo_cw | algo_nw")
    t = t.replace("algo=algo2|algo_nw", "algo=algo_cw|algo_nw")
    rep.write_text(t, encoding="utf-8")


def patch_dual_match() -> None:
    """Make AlgoCwDual treat legacy algo2 / algo2nw trade rows as own."""
    p = ROOT / "foxtrade" / "algo_cw_dual.py"
    text = p.read_text(encoding="utf-8")
    if "from foxtrade.algo_ids import matches" not in text:
        text = text.replace(
            "from foxtrade import broker, trade_store",
            "from foxtrade import broker, trade_store\nfrom foxtrade.algo_ids import matches",
            1,
        )
        # common import style variants
        if "from foxtrade.algo_ids import matches" not in text:
            # insert after first import block line with trade_store
            text = text.replace(
                "import trade_store\n",
                "import trade_store\nfrom foxtrade.algo_ids import matches\n",
                1,
            )
    text = text.replace(
        'if t.get("algo") == self.algo_id',
        "if matches(t.get(\"algo\"), self.algo_id)",
    )
    text = text.replace(
        'if t.get("status") != "OPEN" or t.get("algo") != self.algo_id:',
        'if t.get("status") != "OPEN" or not matches(t.get("algo"), self.algo_id):',
    )
    text = text.replace(
        'opens = [t for t in trade_store.open_trades() if t.get("algo") == self.algo_id]',
        'opens = [t for t in trade_store.open_trades() if matches(t.get("algo"), self.algo_id)]',
    )
    text = text.replace(
        'open_ids = {t["id"] for t in trade_store.open_trades() if t.get("algo") == self.algo_id}',
        'open_ids = {t["id"] for t in trade_store.open_trades() if matches(t.get("algo"), self.algo_id)}',
    )
    text = text.replace("algo2", "algo_cw")  # leftover message strings — careful?
    # That last replace is too broad if any remain as substrings of algo_cw... algo2 shouldn't remain.
    # But would break "algo_cw" if we had algo2 inside? no.
    # Would break comments. OK.
    # Actually "algo2" already mostly gone; only messages. Re-read — we already renamed algo2_ keys.
    # Don't blanket replace again — may damage nothing. Skip blanket.
    p.write_text(text, encoding="utf-8")


def main() -> None:
    n = migrate_trades()
    print(f"trades.json migrated {n} rows")
    patch_ui()
    print("patched dashboard + reports labels")

    # Fix dual matching carefully without blanket algo2 replace
    p = ROOT / "foxtrade" / "algo_cw_dual.py"
    text = p.read_text(encoding="utf-8")
    if "from foxtrade.algo_ids import matches" not in text:
        # find a good insert point
        needle = "from foxtrade import"
        idx = text.find(needle)
        if idx >= 0:
            end = text.find("\n", idx)
            text = text[: end + 1] + "from foxtrade.algo_ids import matches\n" + text[end + 1 :]
        else:
            text = "from foxtrade.algo_ids import matches\n" + text
    text = text.replace(
        "t.get(\"algo\") == self.algo_id",
        "matches(t.get(\"algo\"), self.algo_id)",
    )
    text = text.replace(
        "t.get(\"algo\") != self.algo_id",
        "not matches(t.get(\"algo\"), self.algo_id)",
    )
    text = text.replace(
        '"""Re-attach newest OPEN algo2 setup after process restart."""',
        '"""Re-attach newest OPEN Algo-CW/Algo-NW setup after process restart."""',
    )
    text = text.replace("restored algo2 ", "restored ")
    text = text.replace("no OPEN algo2 legs", "no OPEN legs")
    text = text.replace("while algo2 OPEN", "while OPEN")
    p.write_text(text, encoding="utf-8")
    print("patched algo_cw_dual matching")

    # engine ghost message
    eng = ROOT / "foxtrade" / "engine.py"
    et = eng.read_text(encoding="utf-8")
    et = et.replace("cleared ghost algo2 —", "cleared ghost Algo-CW —")
    # restore accepts legacy runtime key
    if 'or (runtime or {}).get("algo2")' not in et:
        et = et.replace(
            'if self.algo_cw.restore_from_store(runtime):',
            'if self.algo_cw.restore_from_store(runtime) or self.algo_cw.restore_from_store(\n'
            '                {**runtime, "algo_cw": (runtime or {}).get("algo_cw") or (runtime or {}).get("algo2")}\n'
            '                if runtime else runtime\n'
            '            ):',
            1,
        )
    # Actually that restore hack is ugly — fix inside restore_from_store instead
    eng.write_text(et.replace(
        'if self.algo_cw.restore_from_store(runtime) or self.algo_cw.restore_from_store(\n'
        '                {**runtime, "algo_cw": (runtime or {}).get("algo_cw") or (runtime or {}).get("algo2")}\n'
        '                if runtime else runtime\n'
        '            ):',
        'if self.algo_cw.restore_from_store(runtime):',
    ), encoding="utf-8")
    print("patched engine messages")

    # restore_from_store: also read legacy runtime keys
    text = p.read_text(encoding="utf-8")
    old = 'saved = (runtime or {}).get(self.algo_id) or {}'
    new = (
        'saved = (runtime or {}).get(self.algo_id) or {}\n'
        '        if not saved and self.algo_id == "algo_cw":\n'
        '            saved = (runtime or {}).get("algo2") or {}\n'
        '        if not saved and self.algo_id == "algo_nw":\n'
        '            saved = (runtime or {}).get("algo2nw") or {}'
    )
    if old in text and "get(\"algo2\")" not in text:
        text = text.replace(old, new, 1)
        p.write_text(text, encoding="utf-8")
        print("restore_from_store accepts legacy runtime keys")

    # leftover scan
    bad = []
    for f in list((ROOT / "foxtrade").glob("*.py")) + list((ROOT / "static").glob("*.html")):
        if f.name in {"algo2_dual.py", "algo_ids.py", "tools_rename_algo_cw_nw.py"}:
            continue
        body = f.read_text(encoding="utf-8", errors="ignore")
        if re.search(r"\balgo2\b|Algo2(?!State|Dual)|algo2nw|Algo-CW NW|Algo-CW CW", body):
            bad.append(str(f.relative_to(ROOT)))
    print("leftover mentions:", bad or "none")


if __name__ == "__main__":
    main()
