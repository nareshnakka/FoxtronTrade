"""
Verify / prepare option-chain DB after migrate extract or GitHub clone.

Runs automatically from FoxtronTrade.bat on first start.
If chain.db is missing but chain.db.zip is present (from git), extracts it.

Safe to re-run anytime:
  .venv\\Scripts\\python.exe tools_migrate_restore.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LIVE = ROOT / "data" / "option_chain_live"
DB = LIVE / "chain.db"
ZIP = LIVE / "chain.db.zip"
ARM = LIVE / "capture_arm.json"
FLAG = ROOT / "data" / ".migrate_restore_ok"
CHAIN_1M = ROOT / "data" / "option_chain_1m"


def _ok(msg: str) -> None:
    print(f"  OK  {msg}")


def _warn(msg: str) -> None:
    print(f"  WARN {msg}")


def _fail(msg: str) -> None:
    print(f"  FAIL {msg}")


def extract_seed_zip() -> bool:
    """Create chain.db from tracked chain.db.zip when DB is missing."""
    if DB.exists() and DB.stat().st_size > 0:
        return True
    if not ZIP.exists():
        return False
    LIVE.mkdir(parents=True, exist_ok=True)
    size_mb = ZIP.stat().st_size / (1024 * 1024)
    print(f"  Extracting options seed {ZIP.name} ({size_mb:.1f} MB zip)...")
    try:
        with zipfile.ZipFile(ZIP, "r") as zf:
            names = zf.namelist()
            member = next((n for n in names if n.replace("\\", "/").endswith("chain.db")), None)
            if not member:
                _fail(f"chain.db not found inside zip (members={names[:8]})")
                return False
            with zf.open(member) as src, DB.open("wb") as dst:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    dst.write(chunk)
        out_mb = DB.stat().st_size / (1024 * 1024)
        _ok(f"extracted chain.db ({out_mb:.1f} MB)")
        return True
    except Exception as exc:  # noqa: BLE001
        _fail(f"extract zip: {exc}")
        if DB.exists():
            try:
                DB.unlink()
            except OSError:
                pass
        return False


def checkpoint() -> None:
    if not DB.exists():
        return
    con = sqlite3.connect(str(DB), timeout=60)
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.commit()
    finally:
        con.close()


def main() -> int:
    print("=" * 60)
    print("FoxtronTrade — option DB migrate restore / verify")
    print("=" * 60)

    if not (DB.exists() and DB.stat().st_size > 0):
        if ZIP.exists():
            if not extract_seed_zip():
                return 1
        else:
            _fail(f"missing {DB.relative_to(ROOT)}")
            _fail("and no seed zip at data/option_chain_live/chain.db.zip")
            _fail("Run AutoUpdate.bat or re-pack from the source laptop.")
            return 1

    size_mb = DB.stat().st_size / (1024 * 1024)
    _ok(f"chain.db present ({size_mb:.1f} MB)")
    if ZIP.exists():
        z_mb = ZIP.stat().st_size / (1024 * 1024)
        _ok(f"seed zip present ({z_mb:.1f} MB) for future machines")

    # Flush any leftover WAL into main DB (portable restore)
    try:
        checkpoint()
        _ok("wal_checkpoint(TRUNCATE)")
    except Exception as exc:  # noqa: BLE001
        _warn(f"checkpoint: {exc}")

    try:
        con = sqlite3.connect(str(DB), timeout=60)
        try:
            row = con.execute("PRAGMA integrity_check").fetchone()
            if not row or str(row[0]).lower() != "ok":
                _fail(f"integrity_check: {row}")
                return 2
            _ok("integrity_check ok")

            tables = {
                r[0]
                for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            needed = {"ltp_ticks", "ltp_legs", "oi_snaps"}
            if not needed.issubset(tables):
                _warn(f"unexpected tables: {sorted(tables)}")
            else:
                _ok("capture schema present (ltp_ticks / ltp_legs / oi_snaps)")

            day_count = None
            tick_count = None
            try:
                day_count, tick_count = con.execute(
                    "SELECT COUNT(DISTINCT day), COUNT(*) FROM ltp_ticks"
                ).fetchone()
            except sqlite3.Error:
                pass
            leg_count = None
            try:
                leg_count = con.execute("SELECT COUNT(*) FROM ltp_legs").fetchone()[0]
            except sqlite3.Error:
                pass
            oi_count = None
            try:
                oi_count = con.execute("SELECT COUNT(*) FROM oi_snaps").fetchone()[0]
            except sqlite3.Error:
                pass

            if tick_count is not None:
                _ok(f"ltp_ticks={tick_count:,}  days={day_count}")
            if leg_count is not None:
                _ok(f"ltp_legs={leg_count:,}")
            if oi_count is not None:
                _ok(f"oi_snaps={oi_count:,}")
            if not tick_count and not leg_count:
                for t in sorted(tables):
                    try:
                        n = con.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
                        if n:
                            _ok(f"table {t}: {n:,} rows")
                    except sqlite3.Error:
                        pass
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001
        _fail(f"open/verify: {exc}")
        return 3

    if ARM.exists():
        try:
            arm = json.loads(ARM.read_text(encoding="utf-8"))
            _ok(f"capture_arm day={arm.get('day')} capturing={arm.get('capturing')}")
        except Exception as exc:  # noqa: BLE001
            _warn(f"capture_arm: {exc}")

    if CHAIN_1M.exists():
        days = [p.name for p in CHAIN_1M.iterdir() if p.is_dir()]
        _ok(f"option_chain_1m folders: {len(days)}")
    else:
        _warn("option_chain_1m folder missing (live DB still usable)")

    FLAG.parent.mkdir(parents=True, exist_ok=True)
    FLAG.write_text(
        json.dumps(
            {
                "ok": True,
                "db": str(DB.relative_to(ROOT)).replace("\\", "/"),
                "size_mb": round(size_mb, 2),
                "seed_zip": ZIP.exists(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    _ok("restore flag written — app will use this DB as-is")
    print("=" * 60)
    print("READY — captured options history will load on this machine.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
