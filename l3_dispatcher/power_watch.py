# -*- coding: utf-8 -*-
"""v177: 電力哨兵——機器「即將斷電」時事前告警，而不是事後才發現全停。

背景（2026-08-01 r76 實測）：筆電拔了電源在跑電池，ATKLiveConsumer 排程設了
DisallowStartIfOnBatteries ⇒ 真錢消費器整個不啟動（NumberOfMissedRuns 一直累加），
再過約半小時電池耗盡、機器關機 ⇒ daemon 全停、watchdog 也救不回（機器是關的）。

而這件事當時**只以代理值存在**：ceo_oversight.ac_power_online() 量得到 AC=Offline，
但它只被拿去裝飾帳本裡 live_stall 的那句字串（且字面是「接上電源即會自行恢復」，
把「機器快沒電了」講成一件樂觀的事）。沒有任何一條路徑會主動告訴人。
本哨兵補的就是這條路徑：與 v176 IP 哨兵同型——量得到、判得出、就要推出去。

鐵則：
  ⛔ 量不到＝未知，未知一律靜默，永不誤報（不把 None 折成 False 也不折成 True）。
  ⛔ 每種告警一次電池週期只發一次，接回電源才重新武裝（不洗版）。
  ⛔ 只推私人 TG，不對外發布任何內容（紅線②）。
"""
from __future__ import annotations

import asyncio
import json
import time

from botpaths import data_dir

_STATE = data_dir() / "power_watch_state.json"
_POLL_S = 120
_LOW_PCT = 25          # 低電量：還有時間插電
_CRITICAL_PCT = 10     # 危急：隨時會關機


def power_status() -> dict:
    """量目前電力（Windows GetSystemPowerStatus，純唯讀、零副作用）。

    回 {"ac": True/False/None, "pct": int|None, "secs_left": int|None}。
    ⛔ 量不到一律 None＝未知，不是「有接電」也不是「沒接電」。
    """
    out = {"ac": None, "pct": None, "secs_left": None}
    try:
        import ctypes
        from ctypes import wintypes

        class _SPS(ctypes.Structure):
            _fields_ = [("ACLineStatus", wintypes.BYTE),
                        ("BatteryFlag", wintypes.BYTE),
                        ("BatteryLifePercent", wintypes.BYTE),
                        ("SystemStatusFlag", wintypes.BYTE),
                        ("BatteryLifeTime", wintypes.DWORD),
                        ("BatteryFullLifeTime", wintypes.DWORD)]

        st = _SPS()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st)):
            return out
        v = int(st.ACLineStatus) & 0xFF
        out["ac"] = False if v == 0 else (True if v == 1 else None)
        pct = int(st.BatteryLifePercent) & 0xFF
        if 0 <= pct <= 100:                       # 255＝unknown
            out["pct"] = pct
        secs = int(st.BatteryLifeTime)
        if 0 <= secs < 0xFFFFFFFF:                # -1/0xFFFFFFFF＝unknown
            out["secs_left"] = secs
    except Exception:  # noqa: BLE001
        return {"ac": None, "pct": None, "secs_left": None}
    return out


def next_alerts(prev: dict, st: dict) -> tuple[list[str], dict]:
    """純函式：依上一輪狀態與本輪量測，決定這輪要發哪些告警。

    回 (kinds, new_state)。kinds ⊆ {"unplugged","low","critical","restored"}。
    ⛔ ac 未知⇒不發也不改狀態（未知不得推翻既有判斷）。
    """
    ac = st.get("ac")
    if ac is None:
        return [], prev
    fired = list(prev.get("fired") or [])
    if ac is True:
        kinds = ["restored"] if fired else []
        return kinds, {"ac": True, "fired": []}

    kinds = []
    if "unplugged" not in fired:
        kinds.append("unplugged")
        fired.append("unplugged")
    pct = st.get("pct")
    if isinstance(pct, int):
        if pct <= _CRITICAL_PCT:
            if "critical" not in fired:
                kinds.append("critical")
                fired.append("critical")
            if "low" not in fired:
                fired.append("low")        # 已更嚴重，低電量那級不必再補發
        elif pct <= _LOW_PCT and "low" not in fired:
            kinds.append("low")
            fired.append("low")
    return kinds, {"ac": False, "fired": fired}


def _live_positions_line() -> str:
    """真錢在場部位（讀不到就說未知——⛔ 不得把讀不到折成 0 筆）。"""
    try:
        raw = json.loads((data_dir() / "atk_positions_live.json").read_text(encoding="utf-8"))
        open_pos = raw.get("open")
        if isinstance(open_pos, dict):
            return f"{len(open_pos)} 筆"
    except Exception:  # noqa: BLE001
        pass
    return "未知（讀不到部位檔，不等於沒有）"


def _fmt_left(st: dict) -> str:
    secs = st.get("secs_left")
    if isinstance(secs, int) and secs > 0:
        return f"約剩 {secs // 60} 分鐘"
    return "剩餘時間未知"


def build_message(kind: str, st: dict) -> str:
    """組告警文字（繁中、給人看的，不是給程式解析的）。"""
    pct = st.get("pct")
    pct_s = f"{pct}%" if isinstance(pct, int) else "未知"
    if kind == "restored":
        return ("🔌 <b>已接回外部電源</b>\n"
                f"目前電量：{pct_s}\n"
                "真錢消費器排程（電池模式不啟動）會自行恢復每分鐘一輪——"
                "⚠️ 但送不送得出去仍取決於 OKX API 白名單是否含目前對外 IP。")
    head = {
        "unplugged": "🔋 <b>已改用電池</b>（未接外部電源）",
        "low": f"⚠️ <b>電量偏低 {pct_s}</b>（{_fmt_left(st)}）",
        "critical": f"🚨 <b>電量危急 {pct_s}</b>（{_fmt_left(st)}，隨時會關機）",
    }.get(kind, "🔋 <b>電力狀態</b>")
    body = ("・真錢消費器排程設為電池模式不啟動 ⇒ <b>現在零送單</b>，"
            "trade-intent 一到期就永久丟棄、修好也不補送。\n"
            f"・真錢在場部位：{_live_positions_line()}"
            "（若有倉：交易所端止損仍在，但本地部位帳在斷電期間會停止更新）。\n"
            "・一旦電池耗盡關機，主 daemon 全停，watchdog 也救不回（機器是關的）。\n"
            "👉 插上電源即可；插電後仍需確認 OKX 白名單含目前對外 IP 才會真的送得出單。")
    return f"{head}\n{body}"


def _load() -> dict:
    try:
        return json.loads(_STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save(st: dict) -> None:
    try:
        _STATE.write_text(json.dumps(st), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


async def run_power_watch_loop(tg=None, poll_seconds: int = _POLL_S):
    """worker：拔電／低電量／危急／復電 → 私人 TG，各一次，接回電源重新武裝。"""
    print("[power_watch] loop online（2min 哨兵,斷電事前告警）")
    while True:
        try:
            st = power_status()
            prev = _load()
            kinds, new_state = next_alerts(prev, st)
            for kind in kinds:
                print(f"[power_watch] 告警：{kind} pct={st.get('pct')} ac={st.get('ac')}")
                if tg:
                    try:
                        await tg.send_message(build_message(kind, st), parse_mode="HTML")
                    except Exception as e:  # noqa: BLE001
                        print(f"[power_watch] TG 告警失敗：{type(e).__name__}: {e}")
            if new_state != prev:
                new_state["updated_at"] = time.time()
                _save(new_state)
        except Exception as e:  # noqa: BLE001
            print(f"[power_watch] loop 例外（不致命）：{type(e).__name__}: {e}")
        await asyncio.sleep(max(30, int(poll_seconds)))
