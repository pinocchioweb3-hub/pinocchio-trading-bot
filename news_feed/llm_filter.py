"""LLM 智慧新聞過濾 + 繁體中文翻譯（v14）。

用 Claude Code Headless（Max 訂閱，$0 邊際成本）對每篇通過 tier 過濾的貼文做：
    1. 相關性判定：總經/美股/加密/影響市場的地緣政治 → 保留；純選舉政治/罵戰/生活 → 丟棄
    2. 繁體中文翻譯 + 一句話摘要
    3. 重要度評分 1-10

呼叫模式與 l3_dispatcher/synthesizer.py 相同（subprocess + 中性 cwd）。
CLI 失敗時 fallback 到關鍵字過濾（保守：寧可多推不漏推），翻譯則退回英文原文。
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import tempfile

CLASSIFY_PROMPT = """你是一位加密貨幣與美股交易員的新聞分析師。判斷以下社群貼文對「金融市場交易決策」是否有參考價值，並翻譯成繁體中文。

判定規則：
- relevant=true：關稅/貿易戰、聯準會/利率/通膨/就業數據、財政政策、美元/匯率、石油能源、會影響市場的地緣政治（戰爭、制裁、外交衝突）、加密貨幣/比特幣/穩定幣/監管、股市/個股/財報、科技產業、大宗商品
- relevant=false：純選舉造勢/拉票、對政敵或媒體的人身攻擊與罵戰、體育娛樂、個人生活瑣事、宗教節日問候、與市場無關的內政（移民執法細節、地方政治）
- 模稜兩可時：若該訊息可能引發市場波動（例如總統暗示重大政策），判 relevant=true

importance 評分基準（1-10）：
- 9-10：直接重大市場影響（宣布關稅、聯準會決策反應、加密貨幣行政命令）
- 6-8：間接但明確的市場訊號（威脅制裁、暗示政策方向、點名特定公司）
- 3-5：背景資訊（一般經濟評論、模糊表態）
- 1-2：邊緣相關

只輸出一個 JSON 物件，不要 markdown 程式碼框、不要任何其他文字：
{"relevant": true, "category": "macro", "importance": 7, "summary_zh": "一句話繁中摘要", "translation_zh": "全文繁體中文翻譯（保留語氣）", "reason": "判定理由一句話"}

category 可選值：macro / crypto / stocks / geopolitics / politics / other"""

# fallback 關鍵字（CLI 失敗時用；保守 = 命中就推）
_FALLBACK_KEYWORDS = {
    "tariff", "trade", "fed", "rate", "inflation", "economy", "economic",
    "dollar", "tax", "oil", "energy", "crypto", "bitcoin", "btc", "stablecoin",
    "stock", "market", "sec", "etf", "china", "sanction", "war", "iran",
    "nuclear", "deal", "company", "billion", "treasury", "debt", "powell",
}


def _extract_json(text: str) -> dict | None:
    """從 Claude 輸出萃取 JSON（容忍 markdown fence / 前後雜訊）"""
    text = text.strip()
    # 剝 markdown fence
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        text = m.group(1)
    else:
        # 找第一個 { 到最後一個 }
        s, e = text.find("{"), text.rfind("}")
        if s >= 0 and e > s:
            text = text[s:e + 1]
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


async def _call_claude(prompt: str, timeout_sec: int = 90) -> str | None:
    """Claude Code headless 呼叫（同 synthesizer 模式）"""
    claude_exe = shutil.which("claude")
    if not claude_exe:
        return None
    neutral_cwd = tempfile.gettempdir()

    if claude_exe.endswith(".ps1") or claude_exe.endswith(".cmd"):
        proc = await asyncio.create_subprocess_exec(
            "powershell.exe", "-NoProfile",
            "-Command", "claude -p --output-format text",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=neutral_cwd,
        )
    else:
        proc = await asyncio.create_subprocess_exec(
            claude_exe, "-p", "--output-format", "text",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=neutral_cwd,
        )
    try:
        stdout, _stderr = await asyncio.wait_for(
            proc.communicate(input=prompt.encode("utf-8")),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        # v14.1: kill 整個進程樹（只 kill powershell 包裝層會留下 node 孤兒進程）
        try:
            import subprocess as _sp
            _sp.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True, timeout=10)
        except Exception:
            proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:
            pass
        return None
    if proc.returncode != 0:
        return None
    return stdout.decode("utf-8", errors="replace").strip() or None


def _fallback_classify(content: str) -> dict:
    """CLI 失敗時的 fallback：寧可多推不漏推 — relevant 一律 True，
    關鍵字只用來調整 importance（字界比對，避免 'celebrate' 命中 'rate'）。"""
    hits = {k for k in _FALLBACK_KEYWORDS
            if re.search(rf"\b{re.escape(k)}\b", content, re.IGNORECASE)}
    return {
        "relevant": True,   # v14.1: CLI 掛掉時絕不漏推（原設計 T0 全推）
        "category": "other",
        "importance": min(5 + len(hits), 8) if hits else 4,
        "summary_zh": "",
        "translation_zh": content,   # 翻譯失敗 → 推原文
        "reason": f"LLM 不可用，fallback 全推（關鍵字: {', '.join(sorted(hits)[:5]) or '無'}）",
        "_fallback": True,
    }


def is_low_content(content: str) -> bool:
    """純連結 / 過短貼文（沒東西可分析）→ 直接丟，不浪費 LLM 呼叫"""
    stripped = re.sub(r"https?://\S+", "", content).strip()
    return len(stripped) < 15


async def classify_and_translate(handle: str, label: str, content: str,
                                  timeout_sec: int = 90) -> dict:
    """主入口：分類 + 翻譯一篇貼文。

    Returns dict:
        relevant: bool        — 是否推送
        category: str         — macro/crypto/stocks/geopolitics/politics/other
        importance: int       — 1-10
        summary_zh: str       — 繁中一句話摘要
        translation_zh: str   — 繁中全文翻譯
        reason: str           — 判定理由
        _fallback: bool       — （僅 fallback 時出現）
    """
    prompt = (
        f"{CLASSIFY_PROMPT}\n\n---\n"
        f"發文者：@{handle}（{label}）\n"
        f"貼文內容：\n{content[:3000]}"
    )
    raw = await _call_claude(prompt, timeout_sec)
    if raw:
        obj = _extract_json(raw)
        if obj and "relevant" in obj:
            # v14.1: 明確覆寫正規化 — setdefault 擋不住 Claude 輸出的 null 值
            rel = obj.get("relevant")
            obj["relevant"] = (rel is True) or (str(rel).strip().lower() == "true")
            obj["category"] = str(obj.get("category") or "other")
            obj["summary_zh"] = str(obj.get("summary_zh") or "")[:300]
            obj["translation_zh"] = str(obj.get("translation_zh") or content)
            obj["reason"] = str(obj.get("reason") or "")
            try:
                obj["importance"] = max(1, min(10, int(obj.get("importance") or 5)))
            except (TypeError, ValueError):
                obj["importance"] = 5
            return obj
    return _fallback_classify(content)


# ===========================================================================
# 自測
# ===========================================================================
if __name__ == "__main__":
    async def selftest():
        cases = [
            ("realDonaldTrump", "Trump",
             "I am announcing a 100% TARIFF on all foreign-made semiconductors, effective immediately. American chips for American jobs!"),
            ("realDonaldTrump", "Trump",
             "Crooked Joe's approval ratings are the LOWEST in history. Even his own party is abandoning him. SAD!"),
        ]
        for handle, label, content in cases:
            print(f"\n=== @{handle}: {content[:60]}... ===")
            r = await classify_and_translate(handle, label, content)
            print(f"relevant={r['relevant']} cat={r['category']} imp={r['importance']} "
                  f"fallback={r.get('_fallback', False)}")
            print(f"摘要: {r['summary_zh']}")
            print(f"翻譯: {r['translation_zh'][:120]}")
            print(f"理由: {r['reason']}")

    asyncio.run(selftest())
