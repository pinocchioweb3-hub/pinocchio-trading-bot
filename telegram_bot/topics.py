"""Topic Router：把不同類型的訊息路由到社群（Forum 超級群組）的不同主題頻道。

設定檔 telegram_topics.json（由 setup_telegram_group.py 產生，放 BOT 資料目錄）：
    {
      "group_chat_id": "-1001234567890",
      "topics": {"trade": 2, "intel": 3, "news": 4, "usstock": 5}
    }

四個主題：
    trade    🎯 交易訊號（FIRE / TP/SL 事件 / 持倉快照 / 績效 / 風控警報）
    intel    📊 市場情報（Daily Macro / Hourly Pulse / Deep Dive / watchlist）
    news     📰 新聞快訊（Trump / X 推文，已過濾+繁中翻譯）
    usstock  🇺🇸 美股（未來美股報單；先建好佔位）

未設定（json 不存在）→ 全部 fallback 到原本的單一 chat（行為不變）。
"""
from __future__ import annotations

import json
from pathlib import Path

from botpaths import data_dir

from .client import TelegramClient

TOPICS_FILE = data_dir() / "telegram_topics.json"

TOPIC_DEFS = [
    ("trade",     "🎯 交易訊號"),     # v15: 只放 FIRE + 熔斷警報（高價值低頻）
    ("positions", "📈 持倉與績效"),   # v15 新增：TP/SL 事件、持倉快照、績效、風控阻擋
    ("intel",     "📊 市場情報"),
    ("news",      "📰 新聞快訊"),
    ("usstock",   "🇺🇸 美股"),
    ("system",    "🛠 系統狀態"),     # v15 新增：開關機、worker 警報、supervisor
    ("econ",      "📅 經濟數據"),     # v16 新增：CPI/PPI/FOMC 預告與即時判讀
    ("alerts",    "⚡ 異常警報"),     # v19：全市場掃描器警報（非交易訊號，獨立出來）
    ("ideas",     "💡 意見箱"),       # v19：社群建議 + 貢獻積分（累積制）
    ("pulse",     "📡 即時動態"),     # v23：每小時 pulse 獨立（市場情報只留 Daily Macro）
]


def load_topics_config() -> dict | None:
    if not TOPICS_FILE.exists():
        return None
    try:
        cfg = json.loads(TOPICS_FILE.read_text(encoding="utf-8"))
        if cfg.get("group_chat_id") and isinstance(cfg.get("topics"), dict):
            return cfg
    except Exception as e:
        print(f"[topics] config parse error: {e}")
    return None


def save_topics_config(group_chat_id: str, topics: dict[str, int]) -> None:
    TOPICS_FILE.write_text(
        json.dumps({"group_chat_id": str(group_chat_id), "topics": topics},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


class _AuditedClient:
    """v24: 包裝 TelegramClient，send_message 前跑稽核 Session。
    逐字重複直接擋；其餘放行並記錄。其他方法/屬性透明代理。"""

    def __init__(self, inner: TelegramClient, topic_key: str | None):
        self._inner = inner
        self._topic_key = topic_key

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def send_message(self, text: str, **kwargs):
        try:
            from .message_auditor import audit_message, log_send
            v = audit_message(text, topic_key=self._topic_key)
            if v.should_block:
                log_send(text, v, topic_key=self._topic_key, blocked=True)
                print(f"[auditor] BLOCKED 逐字重複 ({v.msg_kind}/{self._topic_key}): {v.dup}")
                return {"ok": False, "blocked_by_auditor": True, "reason": v.dup}
            resp = await self._inner.send_message(text, **kwargs)
            log_send(text, v, topic_key=self._topic_key, blocked=False)
            return resp
        except Exception:
            # 稽核器絕不可阻斷正常發送
            return await self._inner.send_message(text, **kwargs)


class TopicRouter:
    """為每類訊息產生綁定正確（群組, 主題）的 TelegramClient。"""

    def __init__(self, base: TelegramClient | None = None):
        self.base = base or TelegramClient()
        self.cfg = load_topics_config()
        if self.cfg:
            print(f"[topics] forum mode: group={self.cfg['group_chat_id']} "
                  f"topics={self.cfg['topics']}")
        else:
            print("[topics] no forum config, single-chat fallback "
                  "(run setup_telegram_group.py to enable)")

    @property
    def forum_enabled(self) -> bool:
        return self.cfg is not None

    def client(self, topic_key: str):
        """取得綁定主題的 client（v24 起包稽核層）；未設定 forum 時回傳原始 client。"""
        if not self.cfg:
            return self.base
        thread_id = self.cfg["topics"].get(topic_key)
        if thread_id is None:
            return self.base
        inner = self.base.for_topic(self.cfg["group_chat_id"], int(thread_id))
        return _AuditedClient(inner, topic_key)

    def general(self) -> TelegramClient:
        """General 主題（系統訊息：開關機、supervisor 警報）— forum 群組不帶 thread_id 即落 General"""
        if not self.cfg:
            return self.base
        return self.base.for_topic(self.cfg["group_chat_id"], None)
