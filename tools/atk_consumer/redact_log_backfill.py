# -*- coding: utf-8 -*-
"""把**已經落檔**的明文 API key-id 遮蔽掉（v172，監督員 r70）。

為什麼還需要這支——v160/v161 已經修過了，為什麼日誌裡還有 1436 行明文？
------------------------------------------------------------------------
這件事有三段路徑，先前只補了兩段：

  ① 寫進日誌      → v160（r48）把 print 改成「先遮蔽再截斷」。**止住了新的**。
  ② 抄進 repo     → v161 的 tools/secret_leak_scan.py 掃 git 追蹤檔與 commit 訊息。
  ③ 已經在檔案裡的→ **沒人處理**。v160 之前累積的行原封不動躺在資料目錄。

實測（2026-07-31 23:4x，r70）：atk_live.log 共 7253 行，其中 1436 行帶明文 key-id，
`<key-id-redacted>` 標記 0 個——證明 v160 只擋住了後來的，一行舊的都沒清。而且它
**已經自己傳播了一跳**：overseer_l2_log.jsonl 裡也有 1 個（某輪引述錯誤原文時抄進去的）。

這正是 ② 那道閘擋不住的路徑：閘守的是「進 repo 的那一刻」，可是每一輪監督員／CEO
Session 都在 grep 這支日誌、把片段貼進報告與 commit 訊息。留著明文，就是留著一個
每輪都要靠人記得繞開的地雷。這個 repo 是 PUBLIC，踩到一次就不可逆。

⛔ IP 不遮（與 consume_intents.redact_secrets 同一口徑，刻意的）
----------------------------------------------------------------
出口 IP 是使用者拿去補白名單的**唯一**有用資訊，遮掉等於把診斷價值一起丟了；
key-id 對他則零診斷價值。repo 那側由 secret_leak_scan 擋，不靠這裡遮。
下面有回歸鎖把這條釘住——哪天有人「順手」把 IP 也遮了，測試會紅。

安全設計（這支會就地改寫檔案，所以每一條都不是裝飾）
----------------------------------------------------
• **位元組層**：正則跑在 bytes 上，不 decode 也不 encode。BOM、換行慣例、任何壞碼
  片段都原樣保留。（PS 5.1 的 Out-File -Encoding utf8 是**帶 BOM** 的 UTF-8，
  decode→encode 一趟就可能把 BOM 弄丟／把壞碼「修」成別的東西。）
• **尾巴安全**：這支跑的時候，schtasks 每分鐘還在 append。天真的 read→write 會把
  中間新寫進來的那一輪吃掉。故：先讀 N 位元組 → 遮蔽 → 寫暫存 → **回頭把 N 之後
  新長出來的尾巴補進暫存**（迴圈到尾巴為空）→ 才 os.replace。
• **不留備份**：備份檔本身就含明文＝白做。改用「換上去之前先驗算」取代備份：
  位元組差必須剛好等於 命中數 ×(36−18)，行數必須不變；對不上就中止、原檔不動。
• **預設乾跑**：要 --apply 才會真的動檔案。
• **冪等**：跑第二次命中 0、不改檔。
• JSON/JSONL 另加一道：遮蔽後必須仍能逐行 parse，否則跳過該檔（fail-closed）。
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# 與 consume_intents.redact_secrets / secret_leak_scan.KEY_ID_RE 同一個形狀
KEY_ID_RE = re.compile(
    rb"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
MARKER = b"<key-id-redacted>"
_UUID_LEN = 36

DEFAULT_DATA_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "TradingBot"
_SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip",
                  ".gz", ".db", ".sqlite", ".exe", ".dll", ".woff", ".woff2"}


# ---------------------------------------------------------------------------
# 純函式層
# ---------------------------------------------------------------------------
def redact_bytes(data: bytes) -> tuple[bytes, int]:
    """回 (遮蔽後位元組, 命中數)。純函式、位元組進位元組出，不碰編碼。"""
    hits = len(KEY_ID_RE.findall(data or b""))
    return KEY_ID_RE.sub(MARKER, data or b""), hits


def verify_substitution(before: bytes, after: bytes, hits: int) -> tuple[bool, str]:
    """換檔前的驗算：只准少掉 key-id，不准少掉別的（純函式）。

    這取代了「先備份再改」——備份檔本身含明文，留著等於沒清。
    """
    # 直接重算「唯一可接受的結果」再逐位元組比對——比任何還原式檢查都強，
    # 而且免疫於「檔案本來就含 <key-id-redacted> 字樣」。
    # ⚠️ 這裡踩過一次坑（r70 實測）：原本用「把標記換回等長佔位再兩邊對齊」，
    #    遇到 overseer_l2_log.jsonl 這種**早就引述過遮蔽後樣本**的檔案時，
    #    既有標記也會被一起換掉 ⇒ 誤判「還有別的位元組被改動」而拒絕改檔。
    #    fail-closed 沒造成損害，但那是誤報。下面的等式比對不會有這個問題。
    expected = KEY_ID_RE.sub(MARKER, before)
    if after != expected:
        if len(after) != len(expected):
            return False, f"位元組數對不上：預期 {len(expected)}、實得 {len(after)}"
        return False, "除了 key-id 之外還有位元組被改動"
    if before.count(b"\n") != after.count(b"\n"):
        return False, "行數改變了（遮蔽不該增刪換行）"
    if KEY_ID_RE.search(after):
        return False, "遮蔽後仍殘留 key-id"
    return True, "ok"


def _json_still_parses(path: Path, data: bytes) -> bool:
    """.json / .jsonl 遮蔽後必須仍可解析，否則不換（fail-closed）。"""
    suf = path.suffix.lower()
    if suf not in (".json", ".jsonl"):
        return True
    try:
        text = data.decode("utf-8-sig")
        if suf == ".json":
            json.loads(text)
        else:
            for line in text.splitlines():
                if line.strip():
                    json.loads(line)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# IO 層
# ---------------------------------------------------------------------------
def redact_file(path: Path, *, apply: bool = False) -> dict:
    """遮蔽單一檔案。回 {path, hits, changed, reason}。永不拋（回 reason 說明）。"""
    res = {"path": str(path), "hits": 0, "changed": False, "reason": ""}
    try:
        size0 = path.stat().st_size
        with open(path, "rb") as f:
            head = f.read(size0)
    except Exception as exc:
        res["reason"] = f"讀不到：{exc}"
        return res

    body, hits = redact_bytes(head)
    res["hits"] = hits
    if hits == 0:
        res["reason"] = "無明文 key-id（或已遮蔽過）"
        return res
    if not apply:
        res["reason"] = "乾跑（加 --apply 才會真的改）"
        return res

    tmp = path.with_suffix(path.suffix + ".redact-tmp")
    try:
        with open(tmp, "wb") as out:
            out.write(body)
            # 尾巴安全：把「開始跑之後」新 append 的內容補進來，補到尾巴為空為止。
            # 沒有這段，執行器每分鐘寫的那一輪就會被這支吃掉。
            pos = size0
            for _ in range(8):
                cur = path.stat().st_size
                if cur <= pos:
                    break
                with open(path, "rb") as f:
                    f.seek(pos)
                    tail = f.read(cur - pos)
                t_body, t_hits = redact_bytes(tail)
                out.write(t_body)
                res["hits"] += t_hits
                pos = cur
            out.flush()
            os.fsync(out.fileno())

        with open(path, "rb") as f:
            before_full = f.read(pos)
        with open(tmp, "rb") as f:
            after_full = f.read()
        ok, why = verify_substitution(before_full, after_full, res["hits"])
        if not ok:
            res["reason"] = f"驗算不過，原檔未動：{why}"
            return res
        if not _json_still_parses(path, after_full):
            res["reason"] = "遮蔽後 JSON 解析失敗，原檔未動"
            return res
        if path.stat().st_size != pos:
            res["reason"] = "換檔前又被 append，本輪放棄（下次再跑即可）"
            return res
        os.replace(tmp, path)
        res["changed"] = True
        res["reason"] = f"已遮蔽 {res['hits']} 處"
    except Exception as exc:
        res["reason"] = f"未完成，原檔未動：{exc}"
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
    return res


def scan_dir(dirpath: Path) -> list:
    """列出目錄下含明文 key-id 的檔（純讀，永不拋）。"""
    found = []
    try:
        for p in sorted(dirpath.iterdir()):
            if not p.is_file() or p.suffix.lower() in _SKIP_SUFFIXES:
                continue
            try:
                if KEY_ID_RE.search(p.read_bytes()):
                    found.append(p)
            except Exception:
                continue
    except Exception:
        pass
    return found


# ---------------------------------------------------------------------------
# 自我測試
# ---------------------------------------------------------------------------
def _selftest() -> bool:
    import tempfile
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("  [OK] " if cond else "  [FAIL] ") + name)
        ok = ok and bool(cond)

    # ⛔ 測試素材一律用「掃描器認得的假貨」，絕不抄真實日誌原文（r71 實測踩到：
    #    v172 這支自己就把真實出口 IP 寫進測試素材、隨 e7c3b3b 進了 PUBLIC repo，
    #    而該輪還宣稱 secret_leak_scan 乾淨）。判準見 tools/secret_leak_scan.py：
    #    key-id 佔位＝00000000 開頭；IP 佔位＝RFC 5737 文件用網段（203.0.113.0/24）。
    KID = b"00000000-0000-4000-8000-000000000000"
    LINE = ("⚠️ 設槓桿失敗 MU-USDT-SWAP/long：Error: HTTP 401 from OKX: "
            "Your IP 203.0.113.7 is not included in your API key's ").encode()

    out, hits = redact_bytes(LINE + KID + b" IP whitelist.\n")
    check("命中並換成標記", hits == 1 and MARKER in out and KID not in out)
    # ⛔ 回歸鎖：IP 是使用者補白名單唯一有用的資訊，永遠不遮。
    check("IP 不被遮（口徑同 redact_secrets）", b"203.0.113.7" in out)
    check("其餘文字原樣保留", b"MU-USDT-SWAP/long" in out and out.endswith(b" IP whitelist.\n"))
    check("冪等：再跑一次零命中", redact_bytes(out)[1] == 0)
    check("空輸入不炸", redact_bytes(b"") == (b"", 0))

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)

        # BOM 與非 ASCII 必須位元組級保留（PS 5.1 的 utf8 是帶 BOM 的）
        p = d / "atk_live.log"
        raw = b"\xef\xbb\xbf" + LINE + KID + b"\n" + "第二行中文\n".encode()
        p.write_bytes(raw)
        r = redact_file(p, apply=False)
        check("乾跑不改檔", r["hits"] == 1 and not r["changed"] and p.read_bytes() == raw)
        r = redact_file(p, apply=True)
        after = p.read_bytes()
        check("--apply 有改到", r["changed"] and KID not in after)
        check("BOM 保留", after.startswith(b"\xef\xbb\xbf"))
        check("中文行原樣保留", "第二行中文".encode() in after)
        check("行數不變", raw.count(b"\n") == after.count(b"\n"))
        check("冪等：第二次跑不改檔", redact_file(p, apply=True)["changed"] is False)

        # 尾巴安全——這條是這支存在的理由之一：天真的 read→write 會吃掉這行。
        # （模擬 schtasks 在遮蔽進行中 append 了一輪）
        p2 = d / "tail.log"
        p2.write_bytes(LINE + KID + b"\n")
        _orig_stat_read = redact_bytes
        appended = {"done": False}

        def _hooked(data):
            # 第一次呼叫（處理主體）時偷偷 append 一行，模擬併發寫入
            if not appended["done"]:
                appended["done"] = True
                with open(p2, "ab") as f:
                    f.write("⚠️ 本輪故障 auth_ip_whitelist\n".encode())
            return _orig_stat_read(data)

        globals()["redact_bytes"] = _hooked
        try:
            r2 = redact_file(p2, apply=True)
        finally:
            globals()["redact_bytes"] = _orig_stat_read
        got = p2.read_bytes()
        check("併發 append 的那一輪沒被吃掉", r2["changed"] and "本輪故障".encode() in got)
        check("併發情境下 key-id 仍清乾淨", KID not in got)

        # JSONL 遮蔽後仍可解析
        p3 = d / "overseer_l2_log.jsonl"
        p3.write_bytes(json.dumps({"reason": "API key's " + KID.decode()},
                                  ensure_ascii=False).encode() + b"\n")
        r3 = redact_file(p3, apply=True)
        parsed = json.loads(p3.read_text(encoding="utf-8-sig").strip())
        check("JSONL 遮蔽後仍可解析", r3["changed"] and MARKER.decode() in parsed["reason"])

        check("scan_dir 掃得到殘留", scan_dir(d) == [])

    # 驗算層：假冒的「遮蔽結果」必須被擋下（不然這支就等於沒有備份也沒有把關）
    before = LINE + KID + b"\n"
    tampered = before.replace(KID, MARKER).replace(b"MU-USDT", b"XX-USDT")
    ok_v, _ = verify_substitution(before, tampered, 1)
    check("驗算擋下「順手改了別的位元組」", ok_v is False)
    ok_v2, _ = verify_substitution(before, before.replace(KID, MARKER), 1)
    check("驗算放行正常遮蔽", ok_v2 is True)
    ok_v3, _ = verify_substitution(before, before[:-1].replace(KID, MARKER), 1)
    check("驗算擋下少了一行", ok_v3 is False)

    # r70 實測抓到的誤報：檔案**本來就含**遮蔽標記（引述過遮蔽後的樣本），
    # 又有一個新的明文 key-id。舊的「還原式」驗算會把既有標記一起換掉而誤判成
    # 「還有別的位元組被改動」→ 拒改，明文就永遠留著。這條把它釘死。
    mixed_before = (b'{"a":"' + MARKER + b'","b":"' + KID + b'"}\n')
    ok_v4, why4 = verify_substitution(mixed_before,
                                      KEY_ID_RE.sub(MARKER, mixed_before), 1)
    check("既有遮蔽標記不會害驗算誤報（r70 誤擋 overseer log 的成因）",
          ok_v4 is True)
    return ok


def main(argv: list) -> int:
    if "--selftest" in argv:
        return 0 if _selftest() else 1
    apply = "--apply" in argv
    targets = [Path(a) for a in argv if not a.startswith("--")]
    if not targets:
        targets = scan_dir(DEFAULT_DATA_DIR)
        if not targets:
            print(f"資料目錄沒有殘留明文 key-id：{DEFAULT_DATA_DIR}")
            return 0
    print("模式：" + ("實際改檔（--apply）" if apply else "乾跑（加 --apply 才會真的改）"))
    bad = 0
    for p in targets:
        r = redact_file(p, apply=apply)
        flag = "改了" if r["changed"] else "未改"
        print(f"  [{flag}] {p.name}：命中 {r['hits']} 處——{r['reason']}")
        if apply and r["hits"] and not r["changed"]:
            bad = 1
    return bad


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
