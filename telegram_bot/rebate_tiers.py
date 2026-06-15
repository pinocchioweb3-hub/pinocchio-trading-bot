"""返佣「誠實分級」標籤 — 單一真相來源（v35）。

為什麼存在：
    返佣是本專案的潛在收入來源，但「返佣明細可被第三方獨立查核的程度」
    各交易所天差地遠。把這件事誠實分級、用同一套字眼對外溝通，是本專案
    最重要的信任資產（見 [[trading-bot-rebate-verification]] /
    [[trading-bot-token-strategy-verdict]] 的 31-agent 對抗式查證定案）。

鐵律（務必如實，絕不灌水）：
    - 任何 CEX（含 OKX）的返佣【永遠】到不了完全 trustless。
    - 連最先進的 zkTLS + TEE 也只能證明「依交易所回報正確計算」，
      不能證明交易所中心化帳本本身真實 → 切勿對任何 CEX 返佣掛
      「鏈上可驗 / on-chain verifiable」字樣。
    - GMX 的「部分 trustless」也僅止於『返佣碼歸屬 + tier 設定』寫在合約，
      實際返佣金額的計算與發放仍在鏈下。

本檔只描述「可查核程度」，不是投資建議，也不保證任何返佣金額。
"""
from __future__ import annotations

# ── 分級定義（由強到弱）─────────────────────────────────────────
TIER_PARTIAL_TRUSTLESS = "partial_trustless"
TIER_TRUST_BUT_VERIFY = "trust_but_verify"
TIER_SELF_DISCLOSED = "self_disclosed"

TIER_META = {
    TIER_PARTIAL_TRUSTLESS: {
        "emoji": "🟢",
        "label_zh": "部分鏈上可驗",
        "label_en": "partially trustless",
        "rank": 1,
        "meaning": "部分關鍵欄位寫在公開智能合約，任何人可不靠信任直接鏈上查核。",
    },
    TIER_TRUST_BUT_VERIFY: {
        "emoji": "🟡",
        "label_zh": "可對帳查核",
        "label_en": "trust-but-verify",
        "rank": 2,
        "meaning": "有公開 API + 鏈上紀錄 + 可複現公式，能自行重算對帳；"
                   "但需先信任交易所 API 回報的正確性。",
    },
    TIER_SELF_DISCLOSED: {
        "emoji": "🟠",
        "label_zh": "僅主動公開",
        "label_en": "self-disclosed only",
        "rank": 3,
        "meaning": "中心化帳本，返佣明細只能由交易所主動公開；"
                   "第三方無法 trustless 驗證資金真實存在。",
    },
}

# ── 各來源分級（逐項對應 31-agent 查證結論）────────────────────
REBATE_SOURCES = [
    {
        "venue": "GMX",
        "kind": "DEX（鏈上合約）",
        "tier": TIER_PARTIAL_TRUSTLESS,
        "verifiable": "返佣碼歸屬與 tier 設定寫在 ReferralStorage 合約，"
                      "任何人可直接鏈上讀取查核。",
        "not_verifiable": "實際返佣金額的計算與發放仍在鏈下，金額本身非合約可讀。",
        "source": "https://docs.gmx.io/docs/referrals",
    },
    {
        "venue": "Hyperliquid",
        "kind": "DEX（自有 L1）",
        "tier": TIER_TRUST_BUT_VERIFY,
        "verifiable": "官方 Info API 可拉逐筆成交、鏈上有 fills/claim 紀錄、"
                      "返佣公式公開可複現 → 可自行重算對帳。",
        "not_verifiable": "單筆 referral 對應關係非原生合約可直接讀，"
                          "需信任 API 回報後自算。",
        "source": "https://hyperliquid.gitbook.io/hyperliquid-docs",
    },
    {
        "venue": "OKX",
        "kind": "CEX（中心化）",
        "tier": TIER_SELF_DISCLOSED,
        "verifiable": "Affiliate API 可查被邀請人的入金 / 交易量等欄位"
                      "（需聯盟資格 + Read 權限金鑰）。",
        "not_verifiable": "中心化帳本，返佣明細僅能由交易所主動公開；"
                          "第三方無法 trustless 驗證資金真實存在。",
        "source": "https://www.okx.com/docs-v5/",
    },
]

# ── 誠實邊界聲明（對外溝通務必附上）────────────────────────────
DISCLAIMER = (
    "誠實邊界：以上分級只描述「返佣明細可被第三方獨立查核的程度」，"
    "不代表返佣金額多寡、也不是投資建議。任何 CEX（含 OKX）的返佣"
    "【永遠】到不了完全 trustless——即使最先進的 zkTLS+TEE 也只能證明"
    "「依交易所回報正確計算」，不能證明交易所帳本本身真實。"
    "因此本專案絕不對任何 CEX 返佣宣稱「鏈上可驗」。"
)


def tier_badge(tier: str) -> str:
    """回傳單一分級的短標籤，例如 '🟡 可對帳查核（trust-but-verify）'。"""
    m = TIER_META.get(tier)
    if not m:
        return tier
    return f"{m['emoji']} {m['label_zh']}（{m['label_en']}）"


def render_tiers(html: bool = False) -> str:
    """完整分級表（給系統主題報告 / 文件用）。html=True 走 Telegram HTML。"""
    b = (lambda s: f"<b>{s}</b>") if html else (lambda s: s)
    lines = [b("📊 返佣誠實分級（可查核程度）"), ""]
    for src in sorted(REBATE_SOURCES,
                      key=lambda s: TIER_META[s["tier"]]["rank"]):
        m = TIER_META[src["tier"]]
        lines.append(f"{m['emoji']} {b(src['venue'])}（{src['kind']}）"
                     f" — {m['label_zh']} / {m['label_en']}")
        lines.append(f"  ✅ 可驗：{src['verifiable']}")
        lines.append(f"  ⚠️ 不可驗：{src['not_verifiable']}")
        lines.append("")
    lines.append(("⚖️ " + DISCLAIMER))
    return "\n".join(lines)


if __name__ == "__main__":
    print(render_tiers(html=False))
