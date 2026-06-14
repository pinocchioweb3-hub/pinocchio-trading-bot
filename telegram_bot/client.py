"""Telegram Bot API client（thin async wrapper）。

不用 python-telegram-bot 套件 — 用 httpx 直接打 HTTP，省依賴 + 易部署。

v14:
    - message_thread_id 支援（Forum Topics：一個社群多個主題頻道）
    - default_thread_id：綁定預設主題的 client 實例（給 worker 注入用）
    - reply_to_message_id 支援（修 trade_monitor TP/SL 通知 TypeError bug）
    - create_forum_topic / get_updates（社群初始化用）
"""
from __future__ import annotations

import os
from typing import Any

import httpx


class TelegramClient:
    def __init__(self, token: str | None = None, chat_id: str | None = None,
                 default_thread_id: int | None = None):
        self.token = token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
        self.default_thread_id = default_thread_id
        self.base_url = f"https://api.telegram.org/bot{self.token}"

    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def for_topic(self, chat_id: str, thread_id: int | None) -> "TelegramClient":
        """產生綁定（群組, 主題）的新 client 實例"""
        return TelegramClient(token=self.token, chat_id=chat_id,
                              default_thread_id=thread_id)

    async def send_message(
        self,
        text: str,
        *,
        parse_mode: str | None = "HTML",
        disable_preview: bool = True,
        inline_buttons: list[list[dict[str, str]]] | None = None,
        message_thread_id: int | None = None,
        reply_to_message_id: int | None = None,
    ) -> dict[str, Any]:
        """送訊息。parse_mode=None 代表純文字（不送該參數，Telegram 預設）。"""
        body: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": disable_preview,
        }
        # parse_mode=None / "" → 純文字模式，省略該欄位（Telegram 預設）
        if parse_mode:
            body["parse_mode"] = parse_mode
        if inline_buttons:
            body["reply_markup"] = {"inline_keyboard": inline_buttons}
        thread_id = message_thread_id if message_thread_id is not None else self.default_thread_id
        if thread_id is not None:
            body["message_thread_id"] = thread_id
        if reply_to_message_id is not None:
            body["reply_to_message_id"] = reply_to_message_id

        resp = await self._post("sendMessage", body)
        # reply 目標被刪 / thread 失效 / topic 被刪 → 降級重送一次（不帶 reply）
        if not resp.get("ok") and reply_to_message_id is not None:
            desc = str(resp.get("description", "")).lower()
            if any(m in desc for m in ("repl", "thread", "topic", "not found")):
                body.pop("reply_to_message_id", None)
                resp = await self._post("sendMessage", body)
        return resp

    async def create_forum_topic(self, name: str,
                                 icon_color: int | None = None) -> dict[str, Any]:
        """在 forum 超級群組建主題。成功時 result.message_thread_id 為主題 ID。"""
        body: dict[str, Any] = {"chat_id": self.chat_id, "name": name}
        if icon_color:
            body["icon_color"] = icon_color
        return await self._post("createForumTopic", body)

    async def get_updates(self, offset: int | None = None,
                          timeout: int = 0) -> dict[str, Any]:
        """getUpdates。timeout=0 = 短輪詢（立即返回，避開不穩網路的長連線超時）。"""
        body: dict[str, Any] = {"timeout": timeout,
                                "allowed_updates": ["message", "callback_query",
                                                    "my_chat_member", "chat_member",
                                                    "chat_join_request"]}
        if offset is not None:
            body["offset"] = offset
        # 短輪詢時用固定 20s HTTP 超時就夠（請求本身瞬間返回）
        http_timeout = (timeout + 10) if timeout > 0 else 20
        async with httpx.AsyncClient(timeout=http_timeout) as client:
            r = await client.post(f"{self.base_url}/getUpdates", json=body)
            try:
                return r.json()
            except Exception:
                return {"ok": False, "error": "non-json response"}

    async def send_photo(self, photo_path, caption: str = "",
                         parse_mode: str | None = "HTML",
                         message_thread_id: int | None = None) -> dict[str, Any]:
        """傳圖片（multipart upload）。caption 上限 1024 字。"""
        data: dict[str, Any] = {"chat_id": self.chat_id}
        if caption:
            data["caption"] = caption[:1024]
            if parse_mode:
                data["parse_mode"] = parse_mode
        thread_id = (message_thread_id if message_thread_id is not None
                     else self.default_thread_id)
        if thread_id is not None:
            data["message_thread_id"] = str(thread_id)
        try:
            with open(photo_path, "rb") as f:
                files = {"photo": (str(photo_path).split("\\")[-1], f, "image/png")}
                async with httpx.AsyncClient(timeout=60) as client:
                    r = await client.post(f"{self.base_url}/sendPhoto",
                                          data=data, files=files)
                    return r.json()
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    async def get_me(self) -> dict[str, Any]:
        return await self._get("getMe")

    async def get_chat(self, chat_id: str | int) -> dict[str, Any]:
        """查群組現況（is_forum 等）— 不需要新訊息就能查"""
        return await self._post("getChat", {"chat_id": chat_id})

    async def answer_callback_query(self, callback_query_id: str,
                                    text: str = "",
                                    show_alert: bool = False) -> dict[str, Any]:
        """回應 inline 按鈕點擊（讓按鈕停止轉圈圈 + 彈小提示）"""
        body: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            body["text"] = text[:200]
        if show_alert:
            body["show_alert"] = True
        return await self._post("answerCallbackQuery", body)

    async def edit_message_reply_markup(self, chat_id: str | int, message_id: int,
                                        inline_buttons: list[list[dict]] | None = None
                                        ) -> dict[str, Any]:
        """改掉訊息的按鈕（None = 移除全部按鈕）"""
        body: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id}
        if inline_buttons:
            body["reply_markup"] = {"inline_keyboard": inline_buttons}
        else:
            body["reply_markup"] = {"inline_keyboard": []}
        return await self._post("editMessageReplyMarkup", body)

    async def pin_chat_message(self, message_id: int,
                               disable_notification: bool = True) -> dict[str, Any]:
        """置頂訊息（forum topic 內會顯示於該主題）。"""
        return await self._post("pinChatMessage", {
            "chat_id": self.chat_id, "message_id": message_id,
            "disable_notification": disable_notification})

    async def unpin_chat_message(self, message_id: int | None = None) -> dict[str, Any]:
        """取消置頂；message_id=None 取消最近一則置頂。"""
        body: dict[str, Any] = {"chat_id": self.chat_id}
        if message_id is not None:
            body["message_id"] = message_id
        return await self._post("unpinChatMessage", body)

    async def _post(self, method: str, body: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(f"{self.base_url}/{method}", json=body)
            try:
                return r.json()
            except Exception:
                return {"ok": False, "error": "non-json response", "status": r.status_code,
                        "body": r.text[:300]}

    async def _get(self, method: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(f"{self.base_url}/{method}")
            try:
                return r.json()
            except Exception:
                return {"ok": False, "error": "non-json response"}
