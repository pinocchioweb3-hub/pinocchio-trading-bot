# -*- coding: utf-8 -*-
"""公開 repo 洩漏閘測試（v161）。零網路。

分兩層：
  1. 純函式層——正向檢定（真實形狀的 key-id／可路由 IP 一定要被抓到）與
     負向檢定（佔位 UUID、文件用網段、私有網段、User-Agent 版本號不得誤報）。
  2. repo 層——實際掃現行追蹤檔與最近 commit 訊息，必須乾淨。
     另附「掃描範圍非空」檢定：閘最常見的死法是掃了零個檔案卻回報乾淨。

⚠️ 本檔刻意不寫任何真實形狀的 key-id／公網 IP「字面值」——素材一律在執行時
   由片段組出來，否則這支測試自己就會被第 2 層的 repo 掃描判成洩漏。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import secret_leak_scan as sls  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# 執行時組出素材（原始碼裡看不到完整字面值）
FAKE_KEY_ID = "-".join(("a1b2c3d4", "e5f6", "4a7b", "8c9d", "0e1f2a3b4c5d"))
PUBLIC_IP = ".".join(("8", "8", "8", "8"))          # 公開 DNS，與使用者無關
PUBLIC_IP_2 = ".".join(("1", "1", "1", "1"))


# ── 第 1 層：正向檢定（閘抓得到才有意義） ──────────────────────────────
def test_real_shaped_key_id_is_flagged():
    hits = sls.scan_text(f"key {FAKE_KEY_ID} 出現在這行")
    assert [(k, v) for _, k, v in hits] == [("key_id", FAKE_KEY_ID)]


def test_public_ip_is_flagged():
    hits = sls.scan_text(f"出口 IP 是 {PUBLIC_IP}")
    assert [(k, v) for _, k, v in hits] == [("public_ip", PUBLIC_IP)]


def test_okx_401_log_line_is_flagged_on_both_axes():
    """實務上最可能被抄進 docs 的那一行——兩種洩漏物同時在裡面。"""
    line = (f"Error: HTTP 401 from OKX: Your IP {PUBLIC_IP} is not included "
            f"in your API key's {FAKE_KEY_ID} IP whitelist.")
    kinds = sorted(k for _, k, _ in sls.scan_text(line))
    assert kinds == ["key_id", "public_ip"]


def test_line_numbers_are_reported():
    hits = sls.scan_text(f"乾淨第一行\n第二行有 {PUBLIC_IP_2}")
    assert hits[0][0] == 2


# ── 第 1 層：負向檢定（不得誤報，否則閘會被關掉） ──────────────────────
def test_placeholder_uuid_is_not_flagged():
    assert sls.scan_text("key's 00000000-0000-4000-8000-000000000000 whitelist") == []


def test_rfc5737_documentation_ips_are_not_flagged():
    # 現有 test_atk_consumer.py／consume_intents.py 的 401 素材正是用這個網段
    assert sls.scan_text("Your IP 203.0.113.7 is not included") == []
    assert sls.scan_text("192.0.2.1 198.51.100.9") == []


def test_private_and_loopback_ips_are_not_flagged():
    assert sls.scan_text("bind 127.0.0.1:47654") == []
    assert sls.scan_text("192.168.1.10 10.0.0.5 172.16.3.4 169.254.1.1") == []


def test_user_agent_version_is_not_flagged():
    # Chrome/137.0.0.0 主機碼為 0＝網段位址，不是某台機器的出口 IP
    assert sls.scan_text("Chrome/137.0.0.0 Safari/537.36") == []


def test_dotted_numbers_that_are_not_ips_are_not_flagged():
    assert sls.scan_text("okx-trade-cli@1.4.2 / 2026.07.31.1 / 0.147") == []


def test_out_of_range_octets_are_not_ips():
    assert sls.is_leakable_ip("999.1.1.1") is False
    assert sls.is_leakable_ip("203.0.113") is False


# ── 第 2 層：repo 現況閘 ────────────────────────────────────────────────
def test_scan_scope_is_not_empty():
    """防「掃了零個檔案所以乾淨」的假通過。"""
    files = sls.list_repo_files(ROOT)
    assert len(files) > 100
    names = {p.name for p in files}
    assert "consume_intents.py" in names and "test_atk_consumer.py" in names


def test_tracked_files_contain_no_leaks():
    hits = sls.scan_repo(ROOT)
    assert hits == [], f"追蹤檔中發現疑似洩漏：{hits[:5]}"


def test_untracked_files_contain_no_leaks():
    """早一步的閘：未追蹤但未被 .gitignore 排除的檔案＝下一次 git add 的候選。
    追蹤檔一旦 push 就補不回來，所以這道要擋在 commit 之前。
    誤報時的正解是把該檔移出 repo 樹或加進 .gitignore，不是關掉這道閘。"""
    hits = sls.scan_repo(ROOT, include_untracked=True)
    assert hits == [], f"工作目錄檔案中發現疑似洩漏：{hits[:5]}"


def test_recent_commit_messages_contain_no_leaks():
    hits = sls.scan_commit_messages(ROOT, limit=50)
    assert hits == [], f"最近 50 筆 commit 訊息中發現疑似洩漏：{hits[:5]}"
