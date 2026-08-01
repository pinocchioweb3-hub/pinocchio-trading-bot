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

# v36: 群組精簡 12→6（按「功能」分；資產別在訊息內以 🪙加密/🇺🇸美股/🥇商品 標記區分）。
# 保留這 6 個 key（已有 thread_id）；其餘 6 個（usstock/econ/alerts/pulse/us_signals/
# us_positions）由 migrate_topics_v36.py 改名收尾並關閉(close)封存——不刪除、不丟歷史。
TOPIC_DEFS = [
    ("trade",     "🎯 交易訊號"),     # 加密+美股 FIRE 進場 + 熔斷警報（高價值低頻）
    ("positions", "📈 持倉與績效"),   # 加密+美股 持倉快照(含回連)/TP·SL/績效/風控阻擋
    ("intel",     "📊 市場情報"),     # Daily Macro / 每小時 Pulse / Deep Dive / 全市場掃描警報 / 經濟數據 / 解鎖日曆
    ("news",      "📰 新聞快訊"),     # Trump·X 推文 / 美股新聞 / 美股行情總覽
    ("system",    "🛠 系統狀態"),     # 開關機 / worker 警報 / supervisor / 版本通知 / 帳本錨定報告
    ("ideas",     "💡 意見箱"),       # 社群建議 + 貢獻積分（開源共建互動入口）
    ("cycle",     "🌊 週期·熊底→牛頂"),  # 週期/部位層 shadow：每日深度價值帶觀察（純定位非進場；
    #                                     未 provision thread_id 前 router.client 優雅落 General/主聊）
    ("wlfi",      "🦅 WLFI 專屬追蹤"),   # v179：鏈上大額轉帳/行情劇變/每日日報（display_only,
    #                                     使用者 2026-08-01 指定;thread_id 已 provision=11775）
    ("alt20",     "💎 山寨抄底·現貨Top20"),  # v180：20檔賽道名單每日深度卡+大跌雷達
    #                                     （display_only;thread_id=11807）
]

# v36 已退役（migrate 後關閉封存）的舊 key → 內容去向：
RETIRED_TOPIC_KEYS_V36 = {
    "usstock":      "news",       # 美股行情總覽 → 📰新聞快訊
    "econ":         "intel",      # 經濟數據 → 📊市場情報
    "alerts":       "intel",      # 全市場掃描警報 → 📊市場情報
    "pulse":        "intel",      # 每小時 pulse → 📊市場情報
    "us_signals":   "trade",      # 美股交易訊號 → 🎯交易訊號
    "us_positions": "positions",  # 美股持倉追蹤 → 📈持倉與績效
}


_NO_SETUP = ("⛔ 不要跑 setup_telegram_group.py：它同樣把這種情形讀成「還沒設定過」，"
             "會在同一個群組把主題**全部再建一次**、再用新的 thread_id 整包覆寫設定檔——"
             "原 thread_id 永久滅失、歷史訊息留在孤兒主題裡（Telegram 無法合併主題）。"
             "請人工檢視 telegram_topics.bad 後把原檔修回來。")


def _preserve_bad_config(text: str) -> str:
    """壞掉的設定檔留一份鑑識副本——原檔隨時可能被下一次 save 蓋掉。⛔ 不刪不改原檔。"""
    bad = TOPICS_FILE.with_suffix(".bad")
    try:
        if not bad.exists():        # 只留最早那一份（後續覆蓋會沖掉第一現場）
            bad.write_text(text, encoding="utf-8")
        return f"；壞檔已留證於 {bad.name}"
    except Exception:  # noqa: BLE001
        return "；（留證失敗）"      # 留證是 best-effort，⛔ 不可反過來壓掉主訊息


def load_topics_config_status() -> tuple[dict | None, str]:
    """讀主題路由設定。回 (cfg, status)。

    status ∈ {"ok", "missing", "unreadable", "corrupt", "invalid"}。

    v197（監督員 r91）：同物種第 17 次——**未知被折成確認沒有**。舊版把「沒有檔」「讀不到」
    「壞檔」「形狀不對」四種情形折成同一個 None，而 TopicRouter 把 None 讀成「還沒設定過
    forum」→ 9 個已 provision 的主題全部塌回單一聊天室。使用者看不懂程式碼、也開不了本機
    檔案，Telegram 就是他唯一的介面。

    ⛔ 最貴的不是塌成單一聊天室，是舊碼接著印的那句「run setup_telegram_group.py to
    enable」：該腳本第 97 行同樣吃這個 None ⇒ 判定「還沒設定過」⇒ 在同群組把 TOPIC_DEFS
    全部重建、並用新 thread_id 整包覆寫設定檔。一個讀取端的靜默降級，被一行善意的指示
    兌現成不可逆的破壞。

    ⛔ 讀取端**故意不** fail-closed（在這裡拋，會讓每個 import topics 的行程當場死掉）；
    fail-closed 放在寫入端 save_topics_config。⛔ 勿改回單一回傳值。"""
    try:
        text = TOPICS_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing"          # 唯一該走單一聊天室 fallback 的情形
    except Exception as e:  # noqa: BLE001 權限／被鎖住／IO 錯——檔可能在，只是讀不到
        print(f"🚨 [topics] 設定檔讀不到（{type(e).__name__}: {e}）——"
              f"⛔ 不當成「從來沒設過」。{_NO_SETUP}")
        return None, "unreadable"

    try:
        cfg = json.loads(text)
        if not isinstance(cfg, dict):
            raise ValueError(f"頂層是 {type(cfg).__name__} 不是物件")
    except Exception as e:  # noqa: BLE001
        print(f"🚨 [topics] 設定檔壞了（{type(e).__name__}: {e}）{_preserve_bad_config(text)}"
              f"——⛔ 不當成「從來沒設過」。{_NO_SETUP}")
        return None, "corrupt"

    if not (cfg.get("group_chat_id") and isinstance(cfg.get("topics"), dict)):
        # 舊碼這條路徑 100% 靜默：一個字都不印，路由已塌而畫面上毫無跡象。
        print(f"🚨 [topics] 設定檔解得開、但少了 group_chat_id 或 topics 不是物件"
              f"{_preserve_bad_config(text)}——⛔ 不當成「從來沒設過」。{_NO_SETUP}")
        return None, "invalid"
    return cfg, "ok"


def load_topics_config() -> dict | None:
    """相容入口（callbacks／invite_gate／setup／add_* 共 6 處）：只回 cfg／None。

    ⚠️ 需要分辨「沒設過 vs 讀不出來」一律改用 load_topics_config_status()——
    把這兩者折在一起正是 v197 治的病。"""
    return load_topics_config_status()[0]


def save_topics_config(group_chat_id: str, topics: dict[str, int],
                       *, force: bool = False) -> bool:
    """原子寫設定。回 True/False——⛔ 勿改回直接 write_text，也勿改回回傳 None。

    fail-closed：既有檔存在卻讀不出來時**拒絕覆寫**。呼叫端（add_pulse_topic.py／
    add_us_topics.py）都是「讀出整包 → 改一把 → 整包寫回」，讀不到既有內容還寫回去，
    等於把其餘主題永久抹掉（與 v196 botconfig _OVERRIDES 同型）。人工看過 .bad 之後
    要強制修復，用 force=True。

    非原子寫正是上面那些壞檔的來源：本機有實際斷電事件史（v177 才補電力哨兵），
    寫到一半就是半截 JSON，下次啟動再自己誤讀＝自產自誤的閉環。"""
    _, status = load_topics_config_status()
    if status not in ("ok", "missing") and not force:
        print(f"🚨 [topics] 拒絕覆寫設定檔：既有內容讀不出來（status={status}）。"
              "呼叫端多是「讀出整包→改一把→整包寫回」，此時寫回去會把其餘主題永久抹掉。"
              "請先人工檢視 telegram_topics.bad；確認要覆寫請帶 force=True")
        return False

    tmp = TOPICS_FILE.with_suffix(".tmp")
    try:
        tmp.write_text(
            json.dumps({"group_chat_id": str(group_chat_id), "topics": topics},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(TOPICS_FILE)        # 原子改名：讀者永不讀到半寫檔
        return True
    except Exception as e:  # noqa: BLE001
        print(f"🚨 [topics] 設定存不進去（{type(e).__name__}: {e}）——"
              "新建的主題不會被記住，下次啟動仍走舊設定（或塌回單一聊天室）；"
              "請查磁碟空間／資料夾權限／同步軟體是否鎖檔")
        return False


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
        self.cfg, self.config_status = load_topics_config_status()
        if self.cfg:
            print(f"[topics] forum mode: group={self.cfg['group_chat_id']} "
                  f"topics={self.cfg['topics']}")
        elif self.config_status == "missing":
            print("[topics] no forum config, single-chat fallback "
                  "(run setup_telegram_group.py to enable)")
        else:
            # v197：檔在、只是讀不出來。⛔ 這裡絕不能再印上面那句邀請——
            # 照做的下場是主題被重建一次、原 thread_id 永久滅失。
            print(f"🚨 [topics] 設定檔存在但讀不出來（{self.config_status}）——"
                  "所有主題頻道本輪塌回單一聊天室（訊息仍會送達，但不再分流）。"
                  f"{_NO_SETUP}")

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
