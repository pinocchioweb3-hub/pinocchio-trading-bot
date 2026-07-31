# -*- coding: utf-8 -*-
"""公開 repo 洩漏掃描（v161）——把「出口 IP／API key-id 不得進 repo」從人的紀律變成閘。

為什麼要有這支：
  這個 repo 是 PUBLIC。2026-07-30 起的 401 白名單斷流事件裡，我們每一輪都在處理
  兩種一旦落進 repo 就外洩的字串——出口 IP 與 API key-id（UUID）——它們大量出現在
  atk_live.log、健康檔、錯誤訊息裡，而「不要把日誌片段貼進 docs／commit 訊息」
  這條規矩到目前為止只靠人記得。v160 已把執行器那端的回顯遮蔽補上（先遮再截斷），
  但那守的是「寫進日誌」，守不住「人把日誌抄進 docs 再 commit」這條路徑。
  這支就是後者的閘：純唯讀掃描，掃 git 追蹤中的檔案與 commit 訊息。

⚠️ 邊界（誠實聲明，勿當成全能秘密掃描器）：
  只認兩種樣態——UUID 形狀的 key-id、可路由（global）的 IPv4。
  不掃 API secret／passphrase／token（那些沒有穩定形狀，且 .env 本來就在 .gitignore）。
  掃不到不等於乾淨。
"""
from __future__ import annotations

import ipaddress
import re
import subprocess
import sys
from pathlib import Path

# 與 consume_intents.redact_secrets 同一個形狀（OKX 錯誤訊息裡的 API key 識別碼）
KEY_ID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
# 前後不接數字或點，避免把 2026.07.31.1 這種切一段出來當 IP
IPV4_RE = re.compile(r"(?<![0-9.])((?:[0-9]{1,3}\.){3}[0-9]{1,3})(?![0-9.])")

# 允許的佔位 UUID：全零開頭（測試用假 key-id）。真實 key-id 不可能長這樣。
_PLACEHOLDER_UUID_PREFIX = "00000000"

_BINARY_SUFFIXES = {
    ".db", ".sqlite", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf",
    ".zip", ".gz", ".xlsx", ".woff", ".woff2", ".ttf", ".exe", ".dll",
}


def is_placeholder_key_id(value: str) -> bool:
    """佔位 UUID（測試素材）→ 不算洩漏。"""
    return value.replace("-", "").lower().startswith(_PLACEHOLDER_UUID_PREFIX)


def is_leakable_ip(value: str) -> bool:
    """這個 IPv4 字面值算不算「會洩漏個資的真實位址」。

    不算的：私有／loopback／link-local／保留／RFC 5737 文件用範例網段
    （192.0.2.0/24、198.51.100.0/24、203.0.113.0/24——現有測試素材正是用這個），
    以及主機碼 0 或 255（網段位址／廣播位址，實務上是版本號或網段標記，
    例如 User-Agent 的 Chrome/137.0.0.0）。
    """
    try:
        octets = [int(p) for p in value.split(".")]
    except ValueError:
        return False
    if len(octets) != 4 or any(o > 255 for o in octets):
        return False           # 根本不是合法 IPv4
    if octets[3] in (0, 255):
        return False           # 網段／廣播位址，不是某台機器的出口 IP
    try:
        addr = ipaddress.IPv4Address(value)
    except ValueError:
        return False
    return addr.is_global


def scan_text(text: str) -> list[tuple[int, str, str]]:
    """掃一段文字。回 [(行號, 類別, 命中字串)]；類別為 key_id / public_ip。"""
    found: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for m in KEY_ID_RE.finditer(line):
            if not is_placeholder_key_id(m.group(0)):
                found.append((lineno, "key_id", m.group(0)))
        for m in IPV4_RE.finditer(line):
            if is_leakable_ip(m.group(1)):
                found.append((lineno, "public_ip", m.group(1)))
    return found


def _git(args: list[str], root: Path) -> str:
    r = subprocess.run(["git", *args], cwd=str(root), check=True,
                       capture_output=True)
    return r.stdout.decode("utf-8", errors="replace")


def list_staged_files(root: Path) -> list[str]:
    """本次 commit 即將寫進歷史的檔案（index 版本，非工作區版本）。

    v174（監督員 r72）：pre-commit 閘要看的是 index，不是工作區——
    `git add` 之後又改回來的檔案，工作區乾淨但進歷史的是髒的那版。
    -z：這個 repo 有大量中文檔名，預設輸出會被 git 加引號並跳脫，切不出真名。
    """
    raw = _git(["diff", "--cached", "--name-only", "-z",
                "--diff-filter=ACM"], root)
    out = []
    for name in raw.split("\x00"):
        if not name.strip():
            continue
        if Path(name).suffix.lower() in _BINARY_SUFFIXES:
            continue
        out.append(name)
    return out


def scan_staged(root: Path) -> list[tuple[str, int, str, str]]:
    """掃 index 裡的內容。回 [(相對路徑, 行號, 類別, 命中字串)]。"""
    hits: list[tuple[str, int, str, str]] = []
    for name in list_staged_files(root):
        try:
            text = _git(["show", f":{name}"], root)
        except subprocess.CalledProcessError:
            continue        # 例如 submodule／已被同一次 commit 移走
        for lineno, kind, value in scan_text(text):
            hits.append((name, lineno, kind, value))
    return hits


# git 在 `commit -v` 時把整份 diff 附在這條剪刀線之下，並全數以 # 開頭；
# 那段不會進 commit 訊息，掃它只會製造假警報。
_SCISSORS = "------------------------ >8 ------------------------"


def strip_commit_comments(text: str) -> str:
    """把 git 會自行丟棄的部分（# 註解行、剪刀線以下）先拿掉再掃。"""
    kept = []
    for line in text.splitlines():
        if _SCISSORS in line:
            break
        if line.startswith("#"):
            continue
        kept.append(line)
    return "\n".join(kept)


def scan_message_file(path: Path) -> list[tuple[int, str, str]]:
    """掃「即將送出」的 commit 訊息檔（commit-msg hook 的 $1）。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    return scan_text(strip_commit_comments(text))


def list_repo_files(root: Path, include_untracked: bool = False) -> list[Path]:
    """git 追蹤中的檔案（可選：加上未追蹤、未被 .gitignore 排除的檔案）。"""
    names = _git(["ls-files"], root).splitlines()
    if include_untracked:
        names += _git(["ls-files", "--others", "--exclude-standard"],
                      root).splitlines()
    out = []
    for n in names:
        if not n.strip():
            continue
        p = root / n
        if p.suffix.lower() in _BINARY_SUFFIXES or not p.is_file():
            continue
        out.append(p)
    return out


def scan_repo(root: Path, include_untracked: bool = False
              ) -> list[tuple[str, int, str, str]]:
    """掃 repo 檔案。回 [(相對路徑, 行號, 類別, 命中字串)]。"""
    hits: list[tuple[str, int, str, str]] = []
    for p in list_repo_files(root, include_untracked):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, kind, value in scan_text(text):
            hits.append((str(p.relative_to(root)).replace("\\", "/"),
                         lineno, kind, value))
    return hits


def scan_commit_messages(root: Path, limit: int = 50
                         ) -> list[tuple[str, str, str]]:
    """掃最近 N 筆 commit 訊息。回 [(短雜湊, 類別, 命中字串)]。"""
    raw = _git(["log", f"-{limit}", "--format=%h%x00%B%x1e"], root)
    hits: list[tuple[str, str, str]] = []
    for chunk in raw.split("\x1e"):
        if "\x00" not in chunk:
            continue
        sha, body = chunk.split("\x00", 1)
        for _lineno, kind, value in scan_text(body):
            hits.append((sha.strip(), kind, value))
    return hits


def _main_staged(root: Path) -> int:
    """pre-commit 模式：只掃 index，回非 0 就擋下這次 commit。"""
    hits = scan_staged(root)
    for path, lineno, kind, value in hits:
        print(f"LEAK staged {path}:{lineno} [{kind}] {value}")
    if hits:
        print(f"\n[secret-leak] 這次 commit 的暫存內容有 {len(hits)} 筆疑似洩漏"
              f"——⛔ 已擋下。清掉後重 commit（repo 是 PUBLIC，進了歷史就追不回）。")
        return 4
    print("[secret-leak] 暫存內容乾淨")
    return 0


def _main_message(root: Path, msg_path: Path) -> int:
    """commit-msg 模式：只掃即將送出的訊息本身。"""
    hits = scan_message_file(msg_path)
    for lineno, kind, value in hits:
        print(f"LEAK message :{lineno} [{kind}] {value}")
    if hits:
        print(f"\n[secret-leak] commit 訊息有 {len(hits)} 筆疑似洩漏——⛔ 已擋下。"
              f"訊息裡不要抄日誌原文（出口 IP／key-id）。")
        return 4
    print("[secret-leak] commit 訊息乾淨")
    return 0


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parent.parent
    include_untracked = "--include-untracked" in argv
    commits = 50
    msg_file = None
    for i, a in enumerate(argv):
        if a == "--commits" and i + 1 < len(argv):
            commits = int(argv[i + 1])
        if a == "--message-file" and i + 1 < len(argv):
            msg_file = Path(argv[i + 1])

    if msg_file is not None:
        return _main_message(root, msg_file)
    if "--staged" in argv:
        return _main_staged(root)

    file_hits = scan_repo(root, include_untracked)
    msg_hits = scan_commit_messages(root, commits)

    for path, lineno, kind, value in file_hits:
        print(f"LEAK file {path}:{lineno} [{kind}] {value}")
    for sha, kind, value in msg_hits:
        print(f"LEAK commit {sha} [{kind}] {value}")

    total = len(file_hits) + len(msg_hits)
    scope = "追蹤中＋未追蹤" if include_untracked else "追蹤中"
    if total:
        print(f"\n[secret-leak] 發現 {total} 筆疑似洩漏（範圍：{scope}檔案 + "
              f"最近 {commits} 筆 commit 訊息）——⛔ 不要 push，先清掉。")
        return 4
    print(f"[secret-leak] 乾淨（範圍：{scope}檔案 + 最近 {commits} 筆 commit 訊息）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
