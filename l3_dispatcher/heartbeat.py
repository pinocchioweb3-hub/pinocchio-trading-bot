"""Worker 3: 心跳/狀態彙報。

每 N 秒/分推一則 scan summary 到 Telegram。即使整段沒 FIRE，你也知道機器人活著。

預設：每 1 小時一次，避免洗版。可調 HEARTBEAT_INTERVAL_SEC env。
"""
from __future__ import annotations

import asyncio
from typing import Callable

from telegram_bot.client import TelegramClient
from telegram_bot.message_format import render_heartbeat

from .scheduler import ScanSummary


class HeartbeatState:
    """從 scheduler 收集最近 summary 給 heartbeat 用。"""
    def __init__(self):
        self.latest: ScanSummary | None = None
        self.cycles_since_last_heartbeat = 0
        self.fires_since_last_heartbeat = 0

    def update(self, summary: ScanSummary):
        self.latest = summary
        self.cycles_since_last_heartbeat += 1
        self.fires_since_last_heartbeat += summary.fires_enqueued

    def reset(self):
        self.cycles_since_last_heartbeat = 0
        self.fires_since_last_heartbeat = 0


async def run_heartbeat(
    tg: TelegramClient,
    state: HeartbeatState,
    interval_seconds: int = 3600,
):
    """每 N 秒推 heartbeat。"""
    # 第一次延後一個間隔，避免和 startup 訊息搶
    await asyncio.sleep(interval_seconds)
    while True:
        if state.latest is not None:
            text = render_heartbeat(
                state.latest.snapshots,
                fires_this_cycle=state.fires_since_last_heartbeat,
            )
            try:
                await tg.send_message(text, parse_mode="HTML")
                print(f"[heartbeat] sent (cycles={state.cycles_since_last_heartbeat}, "
                      f"fires={state.fires_since_last_heartbeat})")
                state.reset()
            except Exception as e:
                print(f"[heartbeat] send error: {e}")
        await asyncio.sleep(interval_seconds)
