"""Telegram bot 長輪詢 listener — 從手機發指令給 run_bot.py 內的同個 process。

設計：
- 同一個 Python process 內跑 worker（不會開新 session、不堆疊）
- 只接受授權 chat_id（你的）的訊息，其他自動忽略
- 支援固定指令清單（不接自由對話避免 token 浪費）
- 命令處理結果即時推回 Telegram

指令清單見 commands.py。
"""
from __future__ import annotations

import asyncio
import os
from typing import Callable

import httpx


class TelegramListener:
    def __init__(self, token: str | None = None,
                 authorized_chat_id: str | None = None,
                 long_poll_timeout: int = 30):
        self.token = token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.authorized = str(authorized_chat_id
                              or os.getenv("TELEGRAM_CHAT_ID", ""))
        self.base_url = f"https://api.telegram.org/bot{self.token}"
        self.timeout = long_poll_timeout
        self.last_update_id = 0
        self._client: httpx.AsyncClient | None = None
        self._handlers: dict[str, Callable] = {}

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout + 5)
        return self._client

    def register(self, command: str, handler: Callable):
        """註冊指令處理函式。handler(args: list[str]) -> str (回覆訊息)。"""
        self._handlers[command.lower()] = handler

    async def get_updates(self) -> list[dict]:
        """長輪詢 getUpdates。"""
        params = {
            "offset": self.last_update_id + 1,
            "timeout": self.timeout,
            "allowed_updates": '["message"]',
        }
        try:
            r = await self.client.get(f"{self.base_url}/getUpdates", params=params)
            body = r.json()
            if not body.get("ok"):
                return []
            return body.get("result", [])
        except Exception as e:
            print(f"[listener] getUpdates error: {e}")
            return []

    async def send_reply(self, chat_id: str, text: str) -> None:
        try:
            await self.client.post(
                f"{self.base_url}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            )
        except Exception as e:
            print(f"[listener] send error: {e}")

    async def handle_message(self, msg: dict) -> None:
        text = (msg.get("text") or "").strip()
        chat = msg.get("chat", {})
        chat_id = str(chat.get("id", ""))

        # === 授權檢查 ===
        if chat_id != self.authorized:
            print(f"[listener] ignored from unauthorized chat_id={chat_id}")
            return

        if not text.startswith("/"):
            await self.send_reply(chat_id,
                "ℹ️ 指令需以 <code>/</code> 開頭。輸入 <code>/help</code> 看清單。")
            return

        parts = text.split()
        cmd = parts[0].lower().lstrip("/")
        # 去除 bot 名稱後綴（@MyBot）
        if "@" in cmd:
            cmd = cmd.split("@")[0]
        args = parts[1:]

        handler = self._handlers.get(cmd)
        if not handler:
            await self.send_reply(chat_id,
                f"❓ 未知指令 <code>/{cmd}</code>。輸入 <code>/help</code> 看清單。")
            return

        try:
            reply = await handler(args) if asyncio.iscoroutinefunction(handler) \
                    else handler(args)
            if reply:
                await self.send_reply(chat_id, reply[:4000])  # Telegram 上限 4096
            print(f"[listener] handled /{cmd}")
        except Exception as e:
            await self.send_reply(chat_id,
                f"❌ 指令 <code>/{cmd}</code> 執行失敗：<code>{type(e).__name__}: {str(e)[:200]}</code>")
            print(f"[listener] handler error /{cmd}: {e}")

    async def run_forever(self) -> None:
        print(f"[listener] online, listening for commands from chat_id={self.authorized}")
        print(f"[listener] registered commands: {sorted(self._handlers.keys())}")
        while True:
            updates = await self.get_updates()
            for u in updates:
                self.last_update_id = max(self.last_update_id, u.get("update_id", 0))
                msg = u.get("message")
                if msg:
                    await self.handle_message(msg)
