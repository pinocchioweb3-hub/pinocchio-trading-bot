# .githooks —— commit 期的洩漏閘

這個 repo 是 **PUBLIC**。歷史一旦推出去就追不回，force-push 改寫也只能算補救。

## 為什麼 hook 放這裡而不是 `.git/hooks/`

`.git/hooks/` 不進版控：re-clone 之後就沒了，而且沒有任何人會發現它沒了。
放進 `.githooks/` 換來版控與可審閱，代價是**每次 clone 後要手動接上一次**：

```
git config core.hooksPath .githooks
```

沒接上時 `tests/test_secret_leak_hook.py::test_repo_hookspath_is_wired` 會紅。

## 兩支 hook 各守什麼

| hook | 掃什麼 | 為什麼需要 |
| --- | --- | --- |
| `pre-commit` | **index**（不是工作區）裡的檔案內容 | `git add` 髒版本後把工作區改乾淨，進歷史的仍是髒的那版 |
| `commit-msg` | 即將送出的 commit 訊息 | 2026-07-31 那次外洩，抄進去的正是日誌原文；訊息同樣公開 |

兩支都呼叫 `tools/secret_leak_scan.py`，非 0 就擋下 commit。

## 邊界（誠實聲明）

只認兩種樣態：UUID 形狀的 API key-id、可路由的 IPv4。
**掃不到不等於乾淨**——API secret／passphrase／token 沒有穩定形狀，不在守備範圍
（那些靠 `.env` 進 `.gitignore`）。

## 這支閘存在的理由

掃描器與測試在 v161 就都有了，靠的是「記得跑」。2026-07-31 跳過一次，
真實出口 IP 就進了公開歷史。**紀律不是機制；閘要接在路徑上才叫閘。**
