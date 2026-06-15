"""指標白話對照表（task #10 P0-C 雙受眾呈現層 — 第一塊）。

設計理念（雙受眾，單一真實來源）：
    使用者是非程式背景的散戶；機器人卻滿口 CVD／OI／Funding／R 倍數。看不懂就無法
    信任、也無法照著做。本模組把機器人實際會吐出的每一個術語，用「白話＋怎麼看＋
    誠實提醒」三句話講清楚，並且<b>同一份 canonical TERMS</b> 同時渲染給兩種受眾：
      • 人話卡片  render_overview_cards() / render_term()  → 給 Telegram 的人看
      • 機器 JSON glossary_json()                          → 給 AI Agent／未來信任網頁(#11)讀

紅線對齊（不臆造）：
    每個術語都附「誠實提醒」欄，明講它的侷限——例如資金費率不是越高越好、強勢分是
    相對排名不是勝率保證、CVD 背離只是線索不是進場訊號。對照表本身不預測、不承諾報酬。

純資料 + 純函式，無 Telegram／網路相依，可離線 `python -m l3_dispatcher.glossary` 自測。
"""
from __future__ import annotations

from dataclasses import dataclass

GLOSSARY_VERSION = "1.1"  # 隨術語增修調版號（未來信任網頁/Agent 可據此判快取）

# 分類（顯示順序即此順序）
CATEGORIES: dict[str, str] = {
    "risk": "🎯 風控與部位",
    "flow": "🌊 訂單流與情緒",
    "struct": "🏗 結構與趨勢",
    "macro": "🌐 宏觀與估值",
    "whale": "🐋 鯨魚與機構",
}


@dataclass(frozen=True)
class Term:
    key: str        # 穩定識別字（英數，給程式/網址用）
    zh: str         # 中文名
    abbr: str       # 常見英文/縮寫（沒有就空字串）
    what: str       # 白話：這是什麼
    how: str        # 怎麼看：數值高/低各代表什麼
    caveat: str     # 誠實提醒：它的侷限、別誤用
    cat: str        # 分類 key


# ===========================================================================
# Canonical 術語表 —— 單一真實來源（人話卡片 與 機器 JSON 都從這裡渲染）
# 收錄原則：只收「機器人實際會吐到訊息/簡報裡」的術語（grep message_format.py /
# synthesizer.py 對齊），不灌水、不收用不到的名詞。
# ===========================================================================
TERMS: tuple[Term, ...] = (
    # --- 風控與部位 ---
    Term("r_multiple", "R 倍數", "R",
         "把「這一筆你願意虧的錢」當成 1 個單位（1R）。賺 2R＝賺了 2 倍當初的風險。",
         "用 R 看績效比看金額公平：不管本金多大，+1R＝賺到一倍風險、-1R＝賠掉一次預算。"
         "1R 等於多少錢由你自己定，不是固定 100U。",
         "R 是相對單位，不等於勝率。10 筆 6 勝但每次只賺 0.5R、4 敗各賠 1R，總和仍是虧。",
         "risk"),
    Term("stop_loss", "止損", "SL",
         "事先設好的「認錯出場價」。價格到這裡就出場，把單筆虧損鎖在 1R。",
         "止損距進場越遠，1R 換算的價格空間越大，但同樣金額風險下倉位就越小。",
         "把止損往後挪＝把 1R 凹成 2R、3R，是散戶爆倉頭號原因。設好就別動。",
         "risk"),
    Term("take_profit", "止盈分批", "TP1/TP2/TP3",
         "分三段獲利了結：到 TP1 平一部分並把止損移到成本價，之後就立於不敗。",
         "TP 用 R 標：TP1=1R、TP2=1.5R… 代表賺到風險的幾倍才減碼。",
         "三段都設不代表一定到得了；行情可能 TP1 後就反轉，所以 TP1 後移止損保本最關鍵。",
         "risk"),
    Term("leverage", "槓桿", "Leverage",
         "借交易所的錢放大部位。10x＝用 1 塊本金開 10 塊的單，50x＝開 50 塊。",
         "本工具讓你 1–50x 自己設定（/settings）；它只放大保證金效率，不改變你設定的 1R 風險"
         "（風險由止損距離決定）。高波動的小幣系統會自動調降槓桿、降低瞬間爆倉機率。",
         "槓桿越高、離強制平倉（爆倉）價越近，一根插針就可能歸零。用多少是你自己的決定，"
         "本工具不替你建議倍數，只誠實告訴你風險。",
         "risk"),
    Term("margin", "保證金", "Margin",
         "開這筆單實際被凍結的本金。槓桿越高、同樣倉位佔用的保證金越少。",
         "保證金 ≈ 倉位名目 ÷ 槓桿。看「margin ≤ 帳戶 10%」這類護欄避免單筆壓太重。",
         "保證金不是你的最大虧損；最大虧損由止損決定（正常是 1R）。",
         "risk"),
    Term("composite_score", "綜合分", "composite_score",
         "把多個方向訊號加權後的總分，正數偏多、負數偏空，絕對值越大方向越明確。",
         "綜合分只用來排「哪些標的方向較一致」，是相對強弱，不是賺賠機率。",
         "分數高不等於會賺。它是進場前的篩選器，真正決定盈虧的是風控與執行。",
         "risk"),
    Term("strength_score", "強勢分", "strength_score",
         "依近期報酬/動能算出的相對排名分，用來挑「動態交易層 Top N」。",
         "分數越高代表近期相對市場越強勢，常作為順勢做多的候選池。",
         "強勢是回看（過去強≠未來強），追強勢也可能買在階段高點，仍須止損保護。",
         "risk"),
    Term("confidence", "信心分", "Cross-Check",
         "進場前用其他獨立數據對訊號做二次查核後給的 0–100 分。",
         "≥80 高、60–79 中、40–59 低。分數低代表佐證不足，寧可放過。",
         "信心分是「佐證一致程度」，不是勝率預測；高信心單一樣會虧，只是賠率較合理。",
         "risk"),
    # --- 訂單流與情緒 ---
    Term("cvd", "累積成交量差", "CVD",
         "把主動買單量減主動賣單量一路累加。上升＝買方積極，下降＝賣方積極。",
         "CVD 和價格同向＝有量推動較健康；背道而馳＝動能可能在轉弱（見 CVD 背離）。",
         "CVD 估算自公開成交，不同資料源演算法略有差異，看趨勢方向別摳絕對數字。",
         "flow"),
    Term("cvd_divergence", "CVD 背離", "cvd_divergence",
         "價格創新高（低）但 CVD 沒跟上，代表「效果與力道不一致」，動能可能枯竭。",
         "看漲背離＝跌勢中賣壓在縮；看跌背離＝漲勢中買盤在縮。是潛在反轉的線索。",
         "背離只是線索不是進場訊號，常常背離很久才反轉。須清算/OI 等其他數據確認。",
         "flow"),
    Term("funding", "資金費率", "Funding",
         "永續合約多空之間每 8 小時互付的費用。正值＝多方付錢給空方（多頭擁擠）。",
         "輕微正值正常；極端正值（過熱）常是漲多回調前兆，極端負值反而可能是底部。",
         "資金費率不是越高越好——它衡量擁擠度，過熱是反指標，不是看多理由。",
         "flow"),
    Term("open_interest", "未平倉量", "OI",
         "市場上還沒平倉的合約總量。代表有多少資金「在場上押注」。",
         "價漲＋OI 增＝新資金進場推動（較實）；價漲＋OI 減＝空頭回補（較虛）。",
         "OI 只說『有多少倉位』，不說方向；要配合價格與 CVD 一起讀才有意義。",
         "flow"),
    Term("large_holder", "大戶 vs 散戶", "top trader ratio",
         "交易所揭露的大戶持倉多空比 對照 散戶多空比。看「聰明錢」站哪邊。",
         "大戶偏多而散戶偏空時，常被視為較有利的順大戶方向。",
         "比率是延遲且粗略的代理指標，大戶也會看錯；當輔助、別當聖杯。",
         "flow"),
    Term("liquidation", "清算", "Liquidation",
         "槓桿單被強制平倉。多殺多＝多單被掃，軋空＝空單被掃。",
         "大量清算常造成價格瞬間插針；清算失衡（imbalance）指出哪一邊燃料較多。",
         "清算數據多為估算且事後才知，適合解讀『剛剛發生什麼』而非預測下一步。",
         "flow"),
    # --- 結構與趨勢 ---
    Term("atr_coiling", "ATR 收斂", "atr_coiling",
         "ATR（波動幅度）持續收窄，像彈簧被壓緊，常是大行情爆發前的醞釀。",
         "波動越壓越緊＋量縮，之後一旦放量突破，方向那邊的走勢通常較猛。",
         "收斂只說『可能要動』不說『往哪動』；突破方向才是重點，假突破也常見。",
         "struct"),
    Term("volume_drying", "量能枯竭", "volume_drying",
         "成交量持續萎縮，代表大家在觀望、籌碼換手停滯，常與 ATR 收斂一起出現。",
         "量縮到極致後的第一根放量 K，往往是行情啟動的訊號。",
         "量縮也可能只是沒人要的冷門幣，須搭配結構與強勢分判斷，別單看。",
         "struct"),
    Term("higher_lows", "高低點抬升", "higher_lows",
         "低點一個比一個高，是上升趨勢最基本的結構特徵。",
         "只要高低點持續墊高，趨勢就還在；跌破前一個低點才是結構轉弱。",
         "結構本身沒有 alpha（單看結構勝率不顯著），須靠獨立數據確認才有意義。",
         "struct"),
    Term("btc_gate", "BTC 閘", "btc_gate",
         "用 BTC 是否站上 4h 200MA 當總開關。BTC 走弱時暫停所有做多。",
         "🟢開＝大盤健康可正常運作；🔴關＝BTC 跌破 200MA，山寨做多風險過高先停。",
         "閘是粗略的大盤風險過濾，不保證開著就會賺、關著就會跌，只是降低逆風開單。",
         "struct"),
    Term("ma_200", "均線", "200MA / 50MA / 90MA",
         "過去 N 天收盤的平均價，把雜訊抹平看趨勢。價在均線上方偏多、下方偏空。",
         "站上長均線（如 200MA）常視為中期偏多；多條均線多頭排列趨勢較強。",
         "均線是落後指標（回看平均），盤整時頻繁穿越會給出大量假訊號。",
         "struct"),
    Term("setup_type", "日內爆發 / 左側埋伏", "intraday / ambush",
         "兩種進場劇本：日內爆發＝順突破追動能；左側埋伏＝在支撐區下方分批掛低等回踩。",
         "日內爆發看『現在就動』，左側埋伏看『便宜接、容忍先震盪』。",
         "左側埋伏前 36h 沒爆量是正常震盪別被嚇出；但接刀失敗也要靠止損認錯。",
         "struct"),
    Term("drawdown", "回撤", "Drawdown",
         "從近期最高點回落的幅度。「距期內高 -15%」代表已從高點跌了 15%。",
         "回撤看風險暴露：個人帳戶的日回撤逼近熔斷線時，系統會提醒降檔放慢。",
         "回撤是回看的傷口大小，不預測會不會繼續跌；控制它靠的是事前止損與部位。",
         "struct"),
    # --- 宏觀與估值 ---
    Term("fear_greed", "恐懼貪婪指數", "Fear & Greed",
         "綜合波動/動能/社群情緒的 0–100 市場情緒分。低＝恐懼，高＝貪婪。",
         "常被當反指標：極度恐懼（<20）多是相對低位，極度貪婪（>80）多是相對高位。",
         "情緒指標雜訊大、可以維持極端很久，當背景參考別當擇時訊號。",
         "macro"),
    Term("ahr999", "AHR999 估值", "ahr999",
         "專測 BTC 估值的指標，綜合定投成本與長期成長線，判斷現在貴或便宜。",
         "數值低＝相對低估（適合定投/囤），高＝相對高估。",
         "只適用 BTC、且基於歷史規律；範式若改變（如機構化）歷史區間未必重演。",
         "macro"),
    Term("etf_flow", "ETF 機構流向", "ETF flow",
         "現貨 ETF 的每日淨申購/贖回金額，代表傳統機構資金進出。",
         "持續淨流入（綠）＝機構在買，淨流出（紅）＝在撤，是中期資金面風向。",
         "ETF 流向是日頻且延遲的資金面背景，不適合拿來抓短線進出場點。",
         "macro"),
    Term("basis", "期現基差", "Basis",
         "期貨價相對現貨價的偏離。期貨較貴＝溢價（多頭情緒），較便宜＝折價（看空）。",
         "溢價擴大代表槓桿做多擁擠；折價常見於恐慌或現貨需求弱。",
         "基差反映情緒與套利成本，極端值是擁擠訊號（反指標傾向），非方向保證。",
         "macro"),
    Term("options_oi", "期權未平倉", "Options OI",
         "選擇權市場的未平倉量與其變化，反映機構用選擇權建倉或避險的動向。",
         "OI 大幅增加常代表機構在布局；配合 call/put 偏向看情緒。",
         "選擇權 OI 結構複雜（避險 vs 投機難分），當粗略機構動向參考即可。",
         "macro"),
    Term("eth_btc", "ETH/BTC 比值", "ETH/BTC",
         "用 BTC 計價的 ETH 價格，衡量資金在龍頭與山寨之間的輪動。",
         "比值上升＝資金往 ETH/山寨流（風險偏好升），下降＝回流 BTC（避險）。",
         "比值是相對強弱，不說大盤整體漲跌；兩者可同跌只是跌幅不同。",
         "macro"),
    Term("btc_cycle", "BTC 週期指標", "Pi Cycle / Puell / Golden Ratio / 2yr MA",
         "一組用長期均線/挖礦收益判斷牛熊位置的宏觀指標（如 Pi Cycle 頂部訊號）。",
         "多用來警示『相對頂部/底部區』，例如 Pi Cycle 短長均線交叉示警過熱。",
         "都是基於少數歷史週期的經驗法則，樣本極少，僅供長期背景、不可當擇時。",
         "macro"),
    # --- 鯨魚與機構 ---
    Term("whale_net", "鯨魚淨倉", "Hyperliquid whale net",
         "鏈上可見的大戶（鯨魚）在永續上的淨多/淨空失衡，看大資金押哪邊。",
         "淨多 >50% 壓倒做多、淨空 <-50% 壓倒做空；極端失衡值得留意。",
         "只涵蓋特定平台（如 Hyperliquid）的可見地址，不是全市場，且鯨魚也會錯。",
         "whale"),
    Term("funding_arb", "跨所資金費套利", "funding arb",
         "同一幣在不同交易所資金費率有差時，一邊做多一邊做空賺價差的中性策略。",
         "APR 越高代表當下價差越大；屬市場中性、賺費率不賭方向。",
         "需雙邊同時操作且承擔轉倉/手續費/滑點成本，本工具只揭露機會不代為執行。",
         "whale"),
)


# ===========================================================================
# 渲染：人話卡片（給 Telegram 的人看）
# ===========================================================================
def _esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_overview_cards(max_chars: int = 3600) -> list[str]:
    """概覽：每個術語一行（名稱＋一句白話）。依分類切成多張卡，每張 < max_chars。

    回 list[str]，呼叫端逐張送（避開 Telegram 4096 上限）。"""
    cards: list[str] = []
    header = ("📖 <b>指標白話對照表</b>\n"
              "━━━━━━━━━━━━━━━━\n"
              "看不懂機器人在說什麼？這裡用白話解釋每個術語。\n"
              "想看某個詞的完整說明：<code>/指標 CVD</code>（或 /glossary funding）\n")
    cur = header
    for ck, clabel in CATEGORIES.items():
        block_lines = [f"\n<b>{clabel}</b>"]
        for t in TERMS:
            if t.cat != ck:
                continue
            abbr = f"（{_esc(t.abbr)}）" if t.abbr else ""
            block_lines.append(f"• <b>{_esc(t.zh)}</b>{abbr}：{_esc(t.what)}")
        block = "\n".join(block_lines) + "\n"
        if len(cur) + len(block) > max_chars:
            cards.append(cur.rstrip())
            cur = block.lstrip("\n")
        else:
            cur += block
    if cur.strip():
        cards.append(cur.rstrip())
    return cards


def render_term(t: Term) -> str:
    """單一術語的完整卡片（白話＋怎麼看＋誠實提醒）。"""
    abbr = f"  <code>{_esc(t.abbr)}</code>" if t.abbr else ""
    clabel = CATEGORIES.get(t.cat, t.cat)
    return (
        f"📖 <b>{_esc(t.zh)}</b>{abbr}\n"
        f"<i>{_esc(clabel)}</i>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"<b>白話</b>：{_esc(t.what)}\n\n"
        f"<b>怎麼看</b>：{_esc(t.how)}\n\n"
        f"<b>誠實提醒</b>：{_esc(t.caveat)}"
    )


def lookup(query: str) -> str:
    """模糊查單一術語：比對 key／中文名／縮寫（不分大小寫、子字串）。

    命中 1 個 → 完整卡片；命中多個 → 列候選；找不到 → 提示。"""
    q = (query or "").strip().lower()
    if not q:
        return "用法：<code>/指標 CVD</code>　或　<code>/glossary 資金費率</code>"
    hits = [t for t in TERMS
            if q in t.key.lower() or q in t.zh.lower() or q in t.abbr.lower()]
    if not hits:
        # 退而求其次：比對白話內文
        hits = [t for t in TERMS if q in t.what.lower() or q in t.how.lower()]
    if not hits:
        names = "、".join(t.zh for t in TERMS)
        return (f"找不到「{_esc(query)}」。輸入 /指標 看全表，或試試：\n{_esc(names)}")
    if len(hits) == 1:
        return render_term(hits[0])
    opts = "\n".join(f"• <b>{_esc(t.zh)}</b>"
                     + (f"（{_esc(t.abbr)}）" if t.abbr else "") for t in hits)
    return f"「{_esc(query)}」可能是指：\n{opts}\n\n輸入更精確的名稱看完整說明。"


# ===========================================================================
# 渲染：機器 JSON（給 AI Agent／未來信任網頁 #11 讀）—— 同一份 canonical 來源
# ===========================================================================
def glossary_json() -> dict:
    """機器可讀的對照表。結構穩定、附版號，供 Agent/網頁消費。"""
    return {
        "schema": "trading-bot.glossary",
        "version": GLOSSARY_VERSION,
        "note": "指標白話對照表；每個術語含 what/how/caveat；caveat 標明侷限（不臆造報酬）。",
        "categories": [{"key": k, "label": v} for k, v in CATEGORIES.items()],
        "terms": [
            {
                "key": t.key, "zh": t.zh, "abbr": t.abbr,
                "what": t.what, "how": t.how, "caveat": t.caveat,
                "category": t.cat,
            }
            for t in TERMS
        ],
    }


if __name__ == "__main__":
    import json
    print(f"=== 指標白話對照表 v{GLOSSARY_VERSION} ===")
    print(f"術語數：{len(TERMS)}　分類數：{len(CATEGORIES)}")
    # 不變量：每個術語的分類都在 CATEGORIES 內
    bad = [t.key for t in TERMS if t.cat not in CATEGORIES]
    assert not bad, f"分類未定義：{bad}"
    # 不變量：key 不重複
    keys = [t.key for t in TERMS]
    assert len(keys) == len(set(keys)), "key 重複"
    print("✓ 分類完整、key 唯一")
    cards = render_overview_cards()
    print(f"\n概覽卡片數：{len(cards)}（各長度：{[len(c) for c in cards]}）")
    for c in cards:
        assert len(c) <= 4096, "卡片超過 Telegram 4096 上限"
    print("✓ 每張概覽卡 < 4096")
    print("\n--- 概覽卡 1（去標籤預覽）---")
    import re
    print(re.sub(r"<[^>]+>", "", cards[0]))
    print("\n--- 單詞查詢：CVD ---")
    print(re.sub(r"<[^>]+>", "", lookup("CVD")))
    print("\n--- 單詞查詢：資金費率 ---")
    print(re.sub(r"<[^>]+>", "", lookup("資金費率")))
    print("\n--- 模糊查詢：cvd（應命中多筆）---")
    print(re.sub(r"<[^>]+>", "", lookup("cvd")))
    print(f"\n--- 機器 JSON（前 2 筆）---")
    j = glossary_json()
    print(json.dumps({**j, "terms": j["terms"][:2]}, ensure_ascii=False, indent=1))
