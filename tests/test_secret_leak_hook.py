# -*- coding: utf-8 -*-
"""commit 期洩漏閘測試（v174 / 監督員 r72）。零網路。

v161 的掃描器只有「事後全 repo 掃」一種模式，要靠人記得跑；2026-07-31 跳過一次，
真實出口 IP 就進了 PUBLIC repo 的歷史。本檔守的是把它接成 hook 之後的兩條新路徑：
  1. --staged：掃 index（不是工作區），因為進歷史的是 index 那版。
  2. --message-file：掃即將送出的 commit 訊息，且不得被 `commit -v` 的註解 diff 誤報。

⚠️ 與 test_secret_leak_scan.py 同一條規矩：本檔不寫任何真實形狀的
   key-id／公網 IP「字面值」，素材一律在執行時由片段組出來。
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import secret_leak_scan as sls  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

PUBLIC_IP = ".".join(("8", "8", "8", "8"))          # 公開 DNS，與使用者無關
DOC_IP = ".".join(("192", "0", "2", "7"))           # RFC 5737 文件用網段＝不算洩漏


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True,
                          capture_output=True)


def _repo(tmp_path: Path) -> Path:
    """做一個只在本機、無 remote 的小 repo 當素材。"""
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "t@t.local"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    return tmp_path


# ── --staged：掃的必須是 index，不是工作區 ────────────────────────────
def test_staged_leak_is_flagged(tmp_path):
    repo = _repo(tmp_path)
    (repo / "note.md").write_text(f"出口 IP {PUBLIC_IP}\n", encoding="utf-8")
    _git(["add", "note.md"], repo)
    hits = sls.scan_staged(repo)
    assert [(p, k, v) for p, _, k, v in hits] == [("note.md", "public_ip", PUBLIC_IP)]


def test_staged_reads_index_not_worktree(tmp_path):
    """`git add` 髒版本後把工作區改乾淨——進歷史的仍是髒的那版，閘必須抓到。"""
    repo = _repo(tmp_path)
    f = repo / "note.md"
    f.write_text(f"出口 IP {PUBLIC_IP}\n", encoding="utf-8")
    _git(["add", "note.md"], repo)
    f.write_text("已清乾淨\n", encoding="utf-8")          # 只改工作區，不再 add
    hits = sls.scan_staged(repo)
    assert [k for _, _, k, _ in hits] == ["public_ip"], \
        "掃工作區會回報乾淨＝閘形同虛設；必須掃 index"


def test_staged_ignores_unstaged_file(tmp_path):
    """沒 add 的檔案不進這次歷史，不該擋 commit（避免假警報養成跳過習慣）。"""
    repo = _repo(tmp_path)
    (repo / "clean.md").write_text("乾淨\n", encoding="utf-8")
    _git(["add", "clean.md"], repo)
    (repo / "dirty.md").write_text(f"出口 IP {PUBLIC_IP}\n", encoding="utf-8")
    assert sls.scan_staged(repo) == []


def test_staged_handles_cjk_filenames(tmp_path):
    """本 repo 大量中文檔名——沒有 -z 就會被 git 加引號跳脫，切出來的路徑讀不到檔。"""
    repo = _repo(tmp_path)
    (repo / "研究筆記.md").write_text(f"出口 IP {PUBLIC_IP}\n", encoding="utf-8")
    _git(["add", "研究筆記.md"], repo)
    hits = sls.scan_staged(repo)
    assert len(hits) == 1 and hits[0][0] == "研究筆記.md"


def test_staged_skips_binary_suffix(tmp_path):
    repo = _repo(tmp_path)
    (repo / "shot.png").write_bytes(f"IP {PUBLIC_IP}".encode("utf-8"))
    _git(["add", "shot.png"], repo)
    assert sls.scan_staged(repo) == []


# ── --message-file：掃訊息本身，且不被 commit -v 的註解 diff 誤報 ──────
def test_message_leak_is_flagged(tmp_path):
    p = tmp_path / "COMMIT_EDITMSG"
    p.write_text(f"fix: 401 說 IP {PUBLIC_IP} 不在白名單\n", encoding="utf-8")
    assert [(k, v) for _, k, v in sls.scan_message_file(p)] == \
        [("public_ip", PUBLIC_IP)]


def test_message_ignores_verbose_diff_below_scissors(tmp_path):
    """`git commit -v` 把整份 diff 附在剪刀線下且不會進訊息；掃它＝假警報。"""
    p = tmp_path / "COMMIT_EDITMSG"
    p.write_text(
        "fix: 正常訊息\n"
        "# Please enter the commit message for your changes.\n"
        "# ------------------------ >8 ------------------------\n"
        "diff --git a/log.txt b/log.txt\n"
        f"+Your IP {PUBLIC_IP} is not included\n",
        encoding="utf-8")
    assert sls.scan_message_file(p) == []


def test_message_ignores_comment_lines(tmp_path):
    p = tmp_path / "COMMIT_EDITMSG"
    p.write_text(f"fix: 正常訊息\n# 分支資訊 {PUBLIC_IP}\n", encoding="utf-8")
    assert sls.scan_message_file(p) == []


def test_message_placeholder_material_is_not_flagged(tmp_path):
    """既有行為不可弄壞：文件用網段是測試素材，永遠不算洩漏。"""
    p = tmp_path / "COMMIT_EDITMSG"
    p.write_text(f"docs: 範例位址 {DOC_IP}\n", encoding="utf-8")
    assert sls.scan_message_file(p) == []


# ── hook 本體：閘存在但沒接上＝跟沒有一樣 ─────────────────────────────
def test_hooks_exist_and_invoke_the_scanner():
    for name, flag in (("pre-commit", "--staged"),
                       ("commit-msg", "--message-file")):
        body = (ROOT / ".githooks" / name).read_text(encoding="utf-8")
        assert "secret_leak_scan.py" in body and flag in body, \
            f"{name} 沒有真的呼叫掃描器"


def test_repo_hookspath_is_wired():
    """.githooks 進版控但 core.hooksPath 要手動接——沒接上時要在這裡變紅。"""
    out = subprocess.run(["git", "config", "--get", "core.hooksPath"],
                         cwd=str(ROOT), capture_output=True, text=True)
    assert out.stdout.strip() == ".githooks", \
        "hook 未生效；接法：git config core.hooksPath .githooks"
