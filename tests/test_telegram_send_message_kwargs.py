# -*- coding: utf-8 -*-
"""v253：送訊息的參數名打錯，不得在執行期才炸、更不得被靜默吞掉。

2026-08-03 從新 daemon 開機日誌逮到：
    [coindesk] 推送失敗（下輪重試）：TypeError: TelegramClient.send_message()
               got an unexpected keyword argument 'disable_web_page_preview'

`TelegramClient.send_message` 的參數自 v20（2026-06-12 初始提交）起就叫 `disable_preview`，
而 v175（coindesk）與 v185（wlfi）兩個新呼叫點都寫成 Telegram HTTP API 的欄位名
`disable_web_page_preview`。⇒ **兩個功能從出生就沒推出過任何一則訊息。**

為什麼靜態檢查抓不到、要靠這支測試：
  `telegram_bot/topics.py` 的 TopicClient 用 `send_message(self, text, **kwargs)` 轉發，
  型別檢查器看到的是 `**kwargs`（吃任何名字），錯誤只有在**真的送出那一刻**才成立。
  wlfi 那個呼叫點外面還包著 `except Exception: pass` ⇒ 連那一刻都不會出聲：
  實測 14 筆鯨魚交易被標記 seen、7 筆在 24h 內（最大 $409K），卡片全數蒸發。
  同一物種（把量到的故障折成「沒發生」）。

所以守門必須是**全庫掃呼叫點**，而不是替個別功能補一個 case。
"""
from __future__ import annotations

import ast
import inspect
import pathlib

from telegram_bot.client import TelegramClient

REPO = pathlib.Path(__file__).resolve().parent.parent
SKIP_PARTS = {".git", "__pycache__", "node_modules", ".venv", "venv", "platform"}


def _allowed_kwargs() -> set[str]:
    """以生產簽名為唯一真相，簽名改了測試自動跟著改（不維護第二份清單）。"""
    sig = inspect.signature(TelegramClient.send_message)
    return {
        name for name, p in sig.parameters.items()
        if p.kind in (p.KEYWORD_ONLY, p.POSITIONAL_OR_KEYWORD)
    } - {"self"}


def _bad_call_sites() -> list[tuple[str, int, str]]:
    allowed = _allowed_kwargs()
    bad: list[tuple[str, int, str]] = []
    for path in REPO.rglob("*.py"):
        if SKIP_PARTS & set(path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "send_message"):
                continue
            for kw in node.keywords:
                if kw.arg is None:          # **kwargs 轉發：靜態上無從判斷，跳過
                    continue
                if kw.arg not in allowed:
                    bad.append((str(path.relative_to(REPO)), node.lineno, kw.arg))
    return bad


def test_no_send_message_call_uses_an_unknown_kwarg():
    """任何 `.send_message(...)` 都只准用簽名裡有的參數名。

    ⚠️ 若日後有**別的類別**也叫 send_message 而參數不同，這裡會誤報一筆——
    那正是要的：逼人回來看一眼，而不是讓一個打錯的名字混進去等執行期。
    """
    bad = _bad_call_sites()
    assert not bad, (
        "以下呼叫點用了 TelegramClient.send_message 沒有的參數名"
        f"（合法：{sorted(_allowed_kwargs())}）：\n"
        + "\n".join(f"  {f}:{ln} → {kw}" for f, ln, kw in bad)
    )


def test_scanner_actually_detects_a_planted_typo(tmp_path):
    """反向側：確認掃描器不是恆真。

    上面那條若因掃描邏輯壞掉而永遠綠，等於白裝一道閘（虛設檢定）。
    """
    src = "await tg.send_message(msg, parse_mode='HTML', disable_web_page_preview=True)\n"
    tree = ast.parse(src)
    allowed = _allowed_kwargs()
    hits = [
        kw.arg for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "send_message"
        for kw in node.keywords
        if kw.arg and kw.arg not in allowed
    ]
    assert hits == ["disable_web_page_preview"]
    assert "disable_preview" in allowed      # 正確的名字確實存在


def test_wlfi_whale_card_failure_is_not_swallowed():
    """WLFI 鯨魚卡的送出失敗必須留痕。

    `except Exception: pass` 讓「推不出去」與「沒有鯨魚」在觀測上完全一樣，
    這正是這個 bug 兩天沒被發現的原因。
    """
    src = (REPO / "l3_dispatcher" / "wlfi_watch.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "send_message"):
            continue
        # 找出包住這個呼叫的 try，檢查它的 handler 不是純 pass
        for anc in ast.walk(tree):
            if not isinstance(anc, ast.Try):
                continue
            if not any(node is n for n in ast.walk(anc)):
                continue
            for h in anc.handlers:
                assert not all(isinstance(s, ast.Pass) for s in h.body), (
                    f"wlfi_watch.py:{node.lineno} 的 send_message 失敗被 "
                    "`except: pass` 吞掉 —— 推不出去必須出聲"
                )
