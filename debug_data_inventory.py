"""數據盤點：盤點所有可用 SQLite + 文字檔，給 explore-data 用。"""
import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent

print("=" * 70)
print("  數據源盤點")
print("=" * 70)

# 1. SQLite
print("\n## SQLite databases")
for db in ROOT.glob("*.db"):
    size_kb = db.stat().st_size / 1024
    print(f"  {db.name}: {size_kb:.1f} KB")
    try:
        conn = sqlite3.connect(db)
        c = conn.cursor()
        tables = c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        for (tn,) in tables:
            cnt = c.execute(f"SELECT COUNT(*) FROM {tn}").fetchone()[0]
            cols = c.execute(f"PRAGMA table_info({tn})").fetchall()
            col_str = ", ".join(f"{c[1]} {c[2]}" for c in cols)
            print(f"    [{tn}] {cnt} rows")
            print(f"      cols: {col_str}")
        conn.close()
    except Exception as e:
        print(f"    error: {e}")

# 2. Backtest outputs
print("\n## Backtest output files")
patterns = ["*backtest*.txt", "*sweep*.txt", "*deepdive*.txt", "*output*.txt"]
seen = set()
for pat in patterns:
    for f in ROOT.glob(pat):
        if f.name in seen: continue
        seen.add(f.name)
        size_kb = f.stat().st_size / 1024
        print(f"  {f.name}: {size_kb:.1f} KB")

# 3. Bot logs
print("\n## Bot logs（含歷史備份）")
for log in sorted(ROOT.glob("bot.log*"), key=lambda f: f.stat().st_mtime):
    size_kb = log.stat().st_size / 1024
    print(f"  {log.name}: {size_kb:.1f} KB")
