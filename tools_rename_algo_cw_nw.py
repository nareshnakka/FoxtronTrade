"""One-shot rename: Algo2→Algo-CW (algo_cw), Algo2NW→Algo-NW (algo_nw)."""
from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def transform(text: str) -> str:
    out = text
    # Longer / specific first
    out = out.replace("algo2nw", "algo_nw")
    out = out.replace("Algo2NW", "Algo-NW")
    out = out.replace("Algo2Dual", "AlgoCwDual")
    out = out.replace("Algo2State", "AlgoCwState")
    out = out.replace("algo2_", "algo_cw_")
    out = out.replace('"algo2"', '"algo_cw"')
    out = out.replace("'algo2'", "'algo_cw'")
    out = out.replace(".algo2", ".algo_cw")
    out = out.replace("Algo2", "Algo-CW")
    out = out.replace("Convergence Next-Week", "Algo-NW")
    out = out.replace("Convergence Algo", "Algo-CW")
    # UI / label cleanups after bulk replace
    out = out.replace("Algo-CW — Next week", "Algo-NW")
    out = out.replace("Algo-CW (Next week)", "Algo-NW")
    out = out.replace("Algo-CW (Convergence CW)", "Algo-CW")
    out = out.replace(
        "Entries / Live P&L — Algo-CW (Next week)",
        "Entries / Live P&L — Algo-NW",
    )
    out = out.replace('return "Algo1 CW"', 'return "Algo-CW"')
    out = out.replace('return "Algo-CW NW"', 'return "Algo-NW"')
    out = out.replace("algo2 today", "algo_cw today")
    out = out.replace("algo_nw today", "algo_nw today")  # noop if already
    return out


def main() -> None:
    targets: list[Path] = [
        ROOT / "config.json",
        ROOT / "README.md",
        ROOT / "data" / "runtime_state.json",
    ]
    targets += list((ROOT / "foxtrade").rglob("*.py"))
    targets += list((ROOT / "static").rglob("*"))
    targets += list(ROOT.glob("tools_bt_*.py"))
    targets += list(ROOT.glob("tools_patch_*.py"))

    files = sorted({p for p in targets if p.is_file() and p.suffix.lower() in {
        ".py", ".html", ".js", ".json", ".md"
    }})

    changed: list[str] = []
    for p in files:
        raw = p.read_text(encoding="utf-8")
        if "algo2" not in raw.lower() and "Algo2" not in raw:
            continue
        new = transform(raw)
        if new != raw:
            p.write_text(new, encoding="utf-8")
            changed.append(str(p.relative_to(ROOT)))

    print(f"updated {len(changed)} files")
    for c in changed:
        print(" ", c)

    src = ROOT / "foxtrade" / "algo2_dual.py"
    dst = ROOT / "foxtrade" / "algo_cw_dual.py"
    if src.exists():
        if dst.exists():
            dst.unlink()
        shutil.move(str(src), str(dst))
        print("renamed foxtrade/algo2_dual.py -> algo_cw_dual.py")

    # Compatibility shim for old import path
    shim = ROOT / "foxtrade" / "algo2_dual.py"
    shim.write_text(
        '"""Compat shim — use foxtrade.algo_cw_dual."""\n'
        "from foxtrade.algo_cw_dual import *  # noqa: F403\n"
        "from foxtrade.algo_cw_dual import AlgoCwDual as Algo2Dual  # noqa: F401\n"
        "from foxtrade.algo_cw_dual import AlgoCwState as Algo2State  # noqa: F401\n",
        encoding="utf-8",
    )
    print("wrote compat shim foxtrade/algo2_dual.py")

    renames = [
        ("tools_bt_algo2_last5.py", "tools_bt_algo_cw_last5.py"),
        ("tools_bt_algo2_fallen_retrace_last5.py", "tools_bt_algo_cw_fallen_retrace_last5.py"),
        ("tools_bt_algo2_exit_pct_sweep.py", "tools_bt_algo_cw_exit_pct_sweep.py"),
        ("tools_patch_algo2_id.py", "tools_patch_algo_cw_id.py"),
    ]
    for a, b in renames:
        sa, sb = ROOT / a, ROOT / b
        if sa.exists():
            if sb.exists():
                sb.unlink()
            shutil.move(str(sa), str(sb))
            print(f"renamed {a} -> {b}")


if __name__ == "__main__":
    main()
