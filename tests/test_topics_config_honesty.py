# -*- coding: utf-8 -*-
"""v197（監督員 r91）：Telegram 主題路由設定檔「壞掉／讀不到」不再折成「從來沒設過」。

破口（telegram_bot/topics.py）：
    def load_topics_config() -> dict | None:
        if not TOPICS_FILE.exists():
            return None                      # ← ① 真的沒設定過
        try:
            cfg = json.loads(TOPICS_FILE.read_text(...))
            if cfg.get("group_chat_id") and isinstance(cfg.get("topics"), dict):
                return cfg
        except Exception as e:
            print(f"[topics] config parse error: {e}")
        return None                          # ← ② 壞檔 ③ 讀不到 ④ 形狀不對，全折成 ①

後果分三層，一層比一層貴：

  第一層（路由塌成單一聊天室）：TopicRouter 拿到 None 就整包 fallback 到 base client，
  9 個已 provision 的主題（🎯交易訊號／📈持倉與績效／📊市場情報／📰新聞快訊／🛠系統狀態／
  💡意見箱／🌊週期／🦅WLFI／💎山寨抄底）全部併回同一條流。使用者看不懂程式碼、也開不了
  本機檔案，Telegram 就是他**唯一**的介面。

  第二層（❹形狀不對是 100% 靜默）：JSON 解得開但少了 group_chat_id（例如被別的工具整包
  覆寫過）時，舊碼連 parse error 那行都不會印——一個字都沒有。

  第三層（真正不可逆的那層）：舊碼的 TopicRouter 在這種情形印的是
      "[topics] no forum config, single-chat fallback (run setup_telegram_group.py to enable)"
  ——它**主動指示**使用者去跑 setup_telegram_group.py。而該腳本第 97 行
      existing = load_topics_config()
      if existing: 「已有設定」→ return 0
  同樣吃這個 None ⇒ 判定「還沒設定過」⇒ 在同一個群組把 TOPIC_DEFS 全部**再建一次**，
  然後 save_topics_config 用新的 thread_id 整包覆寫設定檔。原本 9 個 thread_id 永久滅失，
  歷史訊息留在成為孤兒的舊主題裡，Telegram 也沒有「合併主題」這種操作。
  ＝一個讀取端的靜默降級，最後由一行善意的指示把它兌現成不可逆的破壞。

同一個 read-modify-write 陷阱也在 add_pulse_topic.py / add_us_topics.py：讀出整包 →
改一把 → 整包寫回。讀不到既有內容時寫回去＝把其餘主題抹掉（與 v196 的 botconfig
_OVERRIDES 同型）。故 fail-closed 放在**寫入端**：不知道原本有什麼，就不准整包覆寫。
讀取端**故意不** fail-closed——在 load 裡拋會讓每個 import topics 的行程當場死掉。

壞檔還是自己製造的：舊 save_topics_config 用非原子 write_text，斷電寫到一半就是半截
JSON（本機有斷電事件史，v177 才補電力哨兵），下次啟動再自己誤讀＝自產自誤的閉環
（與 v162-v166、v193-v196 同一根因家族，第 17 次）。

本檔鎖住的語意（含反向護欄，避免把偵測改成「一律當沒設定」或「一律不准寫」）：
  1. missing／corrupt／unreadable／invalid 四態必須分得出來。
  2. 壞檔要留鑑識副本（原檔隨時可能被下一次 save 蓋掉）。
  3. 形狀不對不得靜默——一定要出聲。
  4. 檔在但讀不出來時，⛔ 不得再叫使用者去跑 setup_telegram_group.py。
  5. 寫入端 fail-closed：既有檔讀不出來就拒絕整包覆寫，且原檔一個 byte 都不許動。
  6. 反向護欄：真·沒有檔仍回 None、仍走單一聊天室 fallback、仍給 setup 指示（行為不變）。
  7. 反向護欄：設定正常時照常回 cfg、照常進 forum 模式、照常允許覆寫（不得因噎廢食）。
  8. save 必須原子寫，失敗要出聲並回報失敗。
全離線：monkeypatch 到暫存目錄；零網路、零 Telegram API、零交易所、零真錢。
"""
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

from telegram_bot import topics as T  # noqa: E402

GOOD = {"group_chat_id": "-1001234567890",
        "topics": {"trade": 2, "intel": 3, "wlfi": 11775}}


@pytest.fixture()
def cfgfile(tmp_path, monkeypatch):
    """把設定檔指到暫存目錄——⛔ 絕不碰真實資料目錄裡那份（9 個真 thread_id）。"""
    p = tmp_path / "telegram_topics.json"
    monkeypatch.setattr(T, "TOPICS_FILE", p)
    return p


# ---------------------------------------------------------------- 四態可分辨
def test_missing_and_corrupt_are_not_the_same_answer(cfgfile):
    """① 沒有檔 vs ② 壞檔：舊碼兩者都只回 None，分不出來。"""
    assert T.load_topics_config_status()[1] == "missing"

    cfgfile.write_text('{"group_chat_id": "-100123", "topi', encoding="utf-8")
    cfg, status = T.load_topics_config_status()
    assert cfg is None
    assert status == "corrupt", "壞檔被折成『從來沒設過』"


def test_shape_invalid_is_its_own_state(cfgfile):
    """④ 解得開但缺 group_chat_id——舊碼這條路徑一個字都不印。"""
    cfgfile.write_text(json.dumps({"topics": {"trade": 2}}), encoding="utf-8")
    cfg, status = T.load_topics_config_status()
    assert cfg is None
    assert status == "invalid"


def test_shape_invalid_is_not_silent(cfgfile, capsys):
    """⛔ 靜默是本物種最貴的部分：形狀不對必須出聲。"""
    cfgfile.write_text(json.dumps({"topics": {"trade": 2}}), encoding="utf-8")
    T.load_topics_config()
    out = capsys.readouterr().out
    assert out.strip(), "形狀不對時完全沒有輸出——使用者無從得知路由已塌"
    assert "topics" in out


def test_corrupt_keeps_forensic_copy(cfgfile, capsys):
    """壞檔要留鑑識副本，且 ⛔ 不刪不改原檔。"""
    raw = '{"group_chat_id": "-100123", "topi'
    cfgfile.write_text(raw, encoding="utf-8")
    T.load_topics_config()
    bad = cfgfile.with_suffix(".bad")
    assert bad.exists(), "壞檔沒留證——下一次 save 會把第一現場沖掉"
    assert bad.read_text(encoding="utf-8") == raw
    assert cfgfile.read_text(encoding="utf-8") == raw, "原檔被動過"
    assert "🚨" in capsys.readouterr().out


# ------------------------------------------- 第三層：不得指示使用者去重建主題
def test_router_does_not_tell_user_to_rerun_setup_when_file_is_broken(cfgfile, capsys):
    """舊碼在壞檔時印 'run setup_telegram_group.py to enable'。

    照做的下場：setup 判定「還沒設定過」→ 在同群組把 9 個主題全部再建一次 →
    整包覆寫設定檔 → 原 thread_id 永久滅失、歷史訊息成孤兒。⛔ 這行指示只能在
    『真的沒有檔』時出現。"""
    cfgfile.write_text('{"group_chat_id": "-100123", "topi', encoding="utf-8")
    T.TopicRouter(base=object())
    out = capsys.readouterr().out
    # ⚠️ 不是「不准出現這個字串」——明講「⛔ 不要跑它」比隻字不提更有用（使用者不看程式碼，
    # 只會照著螢幕上的指示做）。這裡鎖的是**指示形式**：那句邀請去跑 setup 的話不得出現。
    assert "to enable" not in out, "壞檔時仍叫使用者去跑 setup＝把靜默降級兌現成不可逆破壞"
    assert "⛔ 不要跑 setup_telegram_group.py" in out, "沒有明確擋下那條會造成不可逆破壞的路"
    assert "🚨" in out


def test_router_exposes_degraded_status(cfgfile):
    cfgfile.write_text('{"group_chat_id": "-100123", "topi', encoding="utf-8")
    r = T.TopicRouter(base=object())
    assert r.config_status == "corrupt"
    assert r.forum_enabled is False


# ------------------------------------------------------ 寫入端 fail-closed
def test_save_refuses_to_overwrite_unreadable_existing(cfgfile, capsys):
    """讀不出既有內容就不准整包覆寫——這是 9 個 thread_id 的最後一道保險。"""
    raw = '{"group_chat_id": "-100123", "topi'
    cfgfile.write_text(raw, encoding="utf-8")
    ok = T.save_topics_config("-100999", {"trade": 1})
    assert ok is False
    assert cfgfile.read_text(encoding="utf-8") == raw, "壞檔被整包覆寫＝其餘主題永久滅失"
    assert "🚨" in capsys.readouterr().out


def test_save_force_escape_hatch_still_exists(cfgfile):
    """人工確認過（看過 .bad）就該能修——fail-closed 不是死鎖。"""
    cfgfile.write_text('{"group_chat_id": "-100123", "topi', encoding="utf-8")
    assert T.save_topics_config("-100999", {"trade": 1}, force=True) is True
    assert json.loads(cfgfile.read_text(encoding="utf-8"))["group_chat_id"] == "-100999"


def test_save_is_atomic_and_reports_success(cfgfile):
    assert T.save_topics_config(GOOD["group_chat_id"], GOOD["topics"]) is True
    assert not cfgfile.with_suffix(".tmp").exists(), "殘留 .tmp"
    assert json.loads(cfgfile.read_text(encoding="utf-8")) == GOOD


def test_save_failure_is_loud_and_returns_false(cfgfile, monkeypatch, capsys):
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(Path, "write_text", boom)
    assert T.save_topics_config("-100123", {"trade": 1}) is False
    assert "🚨" in capsys.readouterr().out


# ------------------------------------------------------------ 反向護欄
# ⚠️ 以下三個反向護欄刻意只用**改動前就存在**的 API，且在舊碼上就是綠的——
# 這樣才證明它們是忠實的回歸鎖（鎖住既有行為不准被這次改動弄壞），
# 而不是我自己編出來、只有新碼才滿足得了的期望值。
def test_reverse_guard_missing_file_behaviour_unchanged(cfgfile, capsys):
    """真·沒設定過：仍回 None、仍走單一聊天室、仍給 setup 指示。⛔ 不得一併變兇。"""
    assert T.load_topics_config() is None
    r = T.TopicRouter(base=object())
    assert r.forum_enabled is False
    out = capsys.readouterr().out
    assert "setup_telegram_group" in out, "沒有檔時反而不給 setup 指示＝矯枉過正"
    assert "🚨" not in out
    assert not cfgfile.with_suffix(".bad").exists()


def test_reverse_guard_missing_file_still_writable(cfgfile):
    """沒有檔＝沒有東西會被抹掉 ⇒ 首次寫入不得被 fail-closed 擋住。"""
    T.save_topics_config(GOOD["group_chat_id"], GOOD["topics"])
    assert json.loads(cfgfile.read_text(encoding="utf-8")) == GOOD


def test_reverse_guard_good_config_unchanged(cfgfile, capsys):
    """設定正常時一切照舊——⛔ 不得因噎廢食把正常的 read-modify-write 也擋掉。"""
    cfgfile.write_text(json.dumps(GOOD), encoding="utf-8")
    cfg = T.load_topics_config()
    assert cfg is not None and cfg["topics"]["wlfi"] == 11775
    r = T.TopicRouter(base=object())
    assert r.forum_enabled is True
    assert "🚨" not in capsys.readouterr().out
    merged = dict(GOOD["topics"], alt20=11807)
    T.save_topics_config(GOOD["group_chat_id"], merged)
    assert json.loads(cfgfile.read_text(encoding="utf-8"))["topics"]["alt20"] == 11807


def test_reverse_guard_status_api_agrees_with_legacy_api(cfgfile):
    """新舊兩個入口不得各說各話（load_topics_config 必須just是 status 版的投影）。"""
    for content in (None, '{"broken', json.dumps({"topics": {}}), json.dumps(GOOD)):
        if content is None:
            cfgfile.unlink(missing_ok=True)
        else:
            cfgfile.write_text(content, encoding="utf-8")
        cfg, status = T.load_topics_config_status()
        assert cfg == T.load_topics_config()
        assert (status == "ok") is (cfg is not None)
