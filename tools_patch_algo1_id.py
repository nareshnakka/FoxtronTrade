from pathlib import Path

p = Path("foxtrade/algo1_breakout.py")
t = p.read_text(encoding="utf-8")
old = '''class Algo1Breakout:
    """Directional one-side trader — market analysis picks CE or PE (no convergence)."""

    def __init__(self) -> None:
        self.state = Algo1State()
'''
new = '''class Algo1Breakout:
    """Directional one-side trader — market analysis picks CE or PE (no convergence)."""

    def __init__(self, algo_id: str = "algo1", underlying: str = "NIFTY") -> None:
        self.algo_id = str(algo_id or "algo1")
        self.underlying = str(underlying or "NIFTY").upper()
        self.state = Algo1State()
'''
if old not in t:
    raise SystemExit("init block not found")
t = t.replace(old, new, 1)
repls = [
    ('open_for_algo("algo1")', "open_for_algo(self.algo_id)"),
    ('runtime or {}).get("algo1")', "runtime or {}).get(self.algo_id)"),
    ('release("algo1")', "release(self.algo_id)"),
    ('can_open_new("algo1",', "can_open_new(self.algo_id,"),
    ('seconds_since_last_close("algo1",', "seconds_since_last_close(self.algo_id,"),
    ('day_pnl_stats("algo1",', "day_pnl_stats(self.algo_id,"),
    ('period_pnl("algo1",', "period_pnl(self.algo_id,"),
    ('try_acquire("algo1")', "try_acquire(self.algo_id)"),
    ('"algo": "algo1",', '"algo": self.algo_id,'),
    ('algo="algo1",', "algo=self.algo_id,"),
]
for a, b in repls:
    print(a, "->", t.count(a))
    t = t.replace(a, b)
p.write_text(t, encoding="utf-8")
print("patched ok")
