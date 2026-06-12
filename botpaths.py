"""集中管理資料檔路徑（v14：SQLite 遷出 OneDrive 同步資料夾）。

問題背景：
    專案在 OneDrive 同步資料夾內。SQLite WAL 模式會產生 -wal/-shm sidecar，
    OneDrive 同步時搶檔案鎖 → database is locked / 資料庫損毀風險。

解法：
    所有 .db 改放 %LOCALAPPDATA%\\TradingBot\\（不被 OneDrive 同步）。
    可用環境變數 BOT_DATA_DIR 覆寫。
    首次切換時自動把舊位置的 .db（含 -wal/-shm）搬過去。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent


def data_dir() -> Path:
    """資料目錄：BOT_DATA_DIR env > %LOCALAPPDATA%/TradingBot > 專案根（fallback）"""
    env = os.getenv("BOT_DATA_DIR", "").strip()
    if env:
        d = Path(env)
    else:
        localapp = os.getenv("LOCALAPPDATA", "").strip()
        if localapp:
            d = Path(localapp) / "TradingBot"
        else:
            # v14.1: 這個 fallback 會讓 DB 回到 OneDrive — 大聲警告
            print("[botpaths] WARNING: LOCALAPPDATA not set, DB falls back to "
                  "OneDrive project root — set BOT_DATA_DIR to fix!")
            d = PROJECT_ROOT
    d.mkdir(parents=True, exist_ok=True)
    return d


def _checkpoint_wal(db_file: Path) -> None:
    """搬移前先把 WAL 收斂回主檔（v14.1: 防主檔/-wal 分離搬移遺失已 commit 交易）"""
    import sqlite3
    conn = sqlite3.connect(db_file, timeout=5)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


def db_path(name: str) -> Path:
    """回傳 db 完整路徑；若新位置沒有但專案根有舊檔，自動搬移。

    v14.1 遷移順序：先 checkpoint WAL → 主檔+sidecar 一起搬 → 失敗才 fallback。
    注意：搬移只應在 daemon 停止時發生。
    """
    target = data_dir() / name
    legacy = PROJECT_ROOT / name

    if target.exists():
        # v14.1: 新舊並存偵測 — 舊 daemon 死前可能在舊位置重建了殘留檔
        if legacy.exists():
            print(f"[botpaths] WARNING: stale legacy {name} exists at project root "
                  f"(active DB is {target}) — safe to delete the legacy file")
        return target

    if legacy.exists():
        try:
            _checkpoint_wal(legacy)  # WAL 收斂，sidecar 內容歸零
            shutil.move(str(legacy), str(target))
            for suffix in ("-wal", "-shm"):
                side = PROJECT_ROOT / f"{name}{suffix}"
                if side.exists():
                    shutil.move(str(side), str(target.parent / f"{name}{suffix}"))
            print(f"[botpaths] migrated {name} -> {target}")
        except Exception as e:
            # 搬移失敗（多半 = daemon 還在跑握著鎖）→ 本進程繼續用舊位置
            print(f"[botpaths] WARNING: migrate {name} FAILED ({e}); this process "
                  f"uses LEGACY path {legacy} — stop all bot processes and restart "
                  f"to complete migration (split-brain risk until then)")
            return legacy
    return target


if __name__ == "__main__":
    print(f"data_dir: {data_dir()}")
    for n in ("fire_queue.db", "trade_journal.db", "news_feed.db"):
        print(f"  {n} -> {db_path(n)}")
