"""watchdog 的 daemon 行程探測「查不到」不可無聲——同物種第 10 次的預防性封堵。

背景（2026-08-01 r82 盤點 → r83 治本）：
    `daemon_process_alive()` 查詢失敗時回 None，方向是安全的（不確定就不重啟，
    永不誤殺 daemon）。但它是 `except Exception: pass` ＋ 落到函式尾端的 `return None`
    ——**完全無聲**：沒有 log、沒有告警、退出碼還是 0。

    後果不是誤殺，是**靜默退化**：這支探測是 2026-06-18 事故（Claude App 更新把
    daemon 當子行程一起殺掉、但最後一筆心跳還很新）之後補的「第二訊號」，用來把
    30 分鐘的心跳盲點縮到 3 分鐘。探測若永久壞掉（powershell 政策改變、cmdlet 行為
    變更、locale 解碼不一致），watchdog 會靜靜退回只看心跳的舊行為——**盲點復活，
    而沒有任何人會發現**。

本測試釘死的邊界：
    - 回傳語意不變：ALIVE→True、DEAD→False、查不到→None（⛔ 查不到永不折成 False，
      那會變成「確定死了」→ 誤殺重啟）。
    - 查不到時**必須**在本機 watchdog.log 留痕（fail-loud）；措辭要能區分
      「例外」與「輸出無法判讀」。
    - 正常兩條路徑（ALIVE／DEAD）**不得**留痕——每 3 分鐘一輪，吵了等於沒留痕。
    - 解碼不依賴 locale：cp950／UTF-8 的輸出都要讀得出來（同 _decode_console 的教訓，
      見 test_watchdog_runner_probe_honesty.py）。
"""
from __future__ import annotations

import pytest

import watchdog as wd


@pytest.fixture()
def wlog(tmp_path, monkeypatch):
    """把 watchdog.log 導到暫存檔，回傳讀取器。"""
    p = tmp_path / "watchdog.log"
    monkeypatch.setattr(wd, "WLOG", p)
    return lambda: (p.read_text(encoding="utf-8") if p.exists() else "")


def _fake_run(returncode=0, stdout=b"", stderr=b""):
    def _run(cmd, *a, **kw):
        class _R:
            pass

        _R.returncode = returncode
        _R.stdout = stdout
        _R.stderr = stderr
        return _R()

    return _run


# --- ① 正常兩條路徑：語意不變、且安靜 --------------------------------------


def test_alive_is_true_and_quiet(wlog, monkeypatch):
    monkeypatch.setattr(wd.subprocess, "run", _fake_run(stdout=b"ALIVE\r\n"))
    assert wd.daemon_process_alive() is True
    assert wlog() == "", "正常路徑每 3 分鐘吵一次＝把真正的異常淹掉"


def test_dead_is_false_and_quiet(wlog, monkeypatch):
    monkeypatch.setattr(wd.subprocess, "run", _fake_run(stdout=b"DEAD\r\n"))
    assert wd.daemon_process_alive() is False
    assert wlog() == ""


# --- ② 查不到：回 None 且必須留痕 ------------------------------------------


def test_exception_returns_none_and_logs(wlog, monkeypatch):
    """逾時／powershell 不存在：舊碼 `except Exception: pass` 無聲（本測試會紅）。"""

    def _boom(cmd, *a, **kw):
        raise wd.subprocess.TimeoutExpired(cmd="powershell", timeout=30)

    monkeypatch.setattr(wd.subprocess, "run", _boom)

    assert wd.daemon_process_alive() is None, "查不到必須是『不確定』，⛔ 不可折成 False"
    assert "探測失敗" in wlog(), f"探測失敗卻無聲：{wlog()!r}"


def test_unreadable_output_returns_none_and_logs(wlog, monkeypatch):
    """輸出既不是 ALIVE 也不是 DEAD（被 profile 汙染／政策擋下）：舊碼靜靜回 None。"""
    monkeypatch.setattr(
        wd.subprocess, "run",
        _fake_run(returncode=1, stdout=b"", stderr=b"execution policy blocked"))

    assert wd.daemon_process_alive() is None
    txt = wlog()
    assert "無法判讀" in txt, f"輸出讀不懂卻無聲：{txt!r}"
    assert "1" in txt, "沒把退出碼寫進去＝下次還是查不出為什麼"


def test_two_unknown_kinds_have_different_wording(wlog, monkeypatch):
    """『例外』與『輸出無法判讀』是兩件不同的事，寫成同一句話等於沒區分。"""

    def _boom(cmd, *a, **kw):
        raise OSError("powershell not found")

    monkeypatch.setattr(wd.subprocess, "run", _boom)
    wd.daemon_process_alive()
    monkeypatch.setattr(wd.subprocess, "run", _fake_run(stdout=b"???"))
    wd.daemon_process_alive()

    lines = [ln for ln in wlog().splitlines() if ln.strip()]
    assert len(lines) == 2, f"兩次未知只留了 {len(lines)} 行"
    assert lines[0] != lines[1]


# --- ③ 解碼不依賴 locale ---------------------------------------------------


def test_cp950_bytes_output_is_still_read(wlog, monkeypatch):
    """輸出以 bytes 回來（不靠 text=True 的 locale 解碼）仍要判得出 ALIVE。

    舊碼 `(out.stdout or "").strip()` 對 bytes 做 `"ALIVE" in b"..."` → TypeError
    → 被 `except Exception: pass` 吞掉 → 回 None（本測試會紅）。
    真實情境：locale 與主控台輸出編碼不一致時，text=True 會直接拋 UnicodeDecodeError，
    結果同樣是「活著的 daemon 被讀成不確定」。
    """
    monkeypatch.setattr(
        wd.subprocess, "run",
        _fake_run(stdout="ALIVE\r\n".encode("cp950")))

    assert wd.daemon_process_alive() is True
    assert wlog() == ""
