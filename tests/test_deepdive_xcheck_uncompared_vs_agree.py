"""v227（監督員 r120）：per-symbol deepdive 的「跨所交叉驗證」區塊，
**沒比對過**不再被寫成「✅ 與主源大致一致，訊號可信度較高」。

同物種第 47 次。落點是 `_format_symbol_data()` 的跨所交叉驗證段
（synthesizer.py:936-949）＋其產出端 `macro.py::_binance_divergence()`。

── 為什麼這一次比前 46 次更該修 ──
前面 46 次治的是「未知被折成一個數字」（0.0、$0.0M、走平…）。這一次被折出來的
不是數字，是**一句對訊號可信度的加分結論**——而且那正是這個區塊唯一的存在理由：
它宣稱「兩家交易所看法一致，所以這個訊號比較可信」。當比對根本沒發生時，這句話
是憑空生出來的信心。deepdive 的產出會被抽成 PLAN_JSON（進場／停損／目標），
「已交叉驗證通過」與「這輪沒驗證」對該不該進場、倉位開多大是相反的輸入。

── 線上實測（2026-08-03，非假想路徑）──
CoinGlass 自 2026-07-08 停權後，`_fetch_coinglass_overlays()` 的 funding 與
ls_ratio **兩項都是 None**。`_binance_divergence()` 的兩個比對各自包在
`if cgf is not None and bnf is not None:` 與 `if cgl and bnl:` 底下 ⇒
一次都沒跑 ⇒ `flags` 是空 list。而 synthesizer 端只看 `if xc.get("flags"):`，
空的就走 else 印那句 ✅。實測 BTC／SOL 兩支，LLM 實際讀到的是：

    ## 🔀 跨所交叉驗證（Binance 第二來源）
    - Binance 大戶多空比：1.99
    - Binance 資金費率：+0.0077%/8h
    - ✅ 與主源(OKX/CoinGlass)大致一致，訊號可信度較高

主源那一欄一個數字都沒有，卻宣稱「與主源一致」。**每一張加密 deepdive 卡片
自 7/08 起都是這樣**，且日誌全程沒有任何一行說它壞了。

根因與前 46 次同形：`flags == []` 是**三種語意相反的狀態**共用的表示——
  (1) 比過了，沒有背離     ＝ 答案（該給 ✅）
  (2) 主源缺值，沒得比     ＝ 未知（⛔ 不可給 ✅）
  (3) Binance 側缺值，沒得比 ＝ 未知（⛔ 不可給 ✅）
synthesizer 手上沒有 cg，**結構上無法**分辨這三者 ⇒ 判準必須由產出端寫進資料：
`_binance_divergence()` 改為同時回 `compared`（實際比過的項目）與
`uncompared`（沒比成的項目 + 原因）。沿用本專案既有作法（`_htf_input`、
`_smc_unknown_components`）：**鍵在＝答案，鍵不在／另立鍵＝未知**。

── 順帶同函式、同物種的兩處（目前線上不可達，CG 復通後才會走到）──
  (b) CVD 斜率 None ⇒ 舊碼 `(slope or 0)` 讓兩個比較都是 False ⇒ 落到 `else "走平"`，
      印出「近 24h 斜率 n/a（走平）」——數字誠實說 n/a，括號裡卻斷言走平＝多空均衡。
      這是 v223 在訊號數學輸入端治過的那一個，在 LLM prompt 端的孿生。
  (c) OI `oi_delta_24h` None ⇒ 舊碼整句 24h 變化**靜靜消失**（v225 物種），
      而且 `oi_trend` 那個三元運算在 None 時算出「持平」後根本沒被用到＝死碼，
      正好說明作者當時以為 None 會被印成「持平」。
  這兩處目前不可達：CG 停權下 `cvd`／`oi` 皆為 None ⇒ 整個佐證區塊的
  `any(... is not None)` 守門不會進。⇒ **本輪修補線上觸發 0 次，不得宣稱已實證。**

⛔ 邊界線（沿用 v208–v226）：來源明講的「一致」仍是答案，照舊給 ✅（真的兩所都
   無背離是常見且有意義的事實，誤標成缺料會反過來騙人）；資料齊全時逐字不變。
⛔ `ls_ratio` 的比對舊碼用 `if cgl and bnl:`＝truthiness，來源回 0.0 會被當缺料
   （本物種的反方向）。改 `is not None`。
⛔ 不印來源原始錯誤字串（repo 是 PUBLIC）。
⛔ 未碰 strength.py／eval_cvd_divergence。

測試斷言的是**可觀測輸出字串**（LLM 實際讀到的那幾行）與產出端的鍵，
不是新輔助函式的內部細節。
"""
from __future__ import annotations

from l3_dispatcher.macro import _binance_divergence
from l3_dispatcher.synthesizer import _format_symbol_data


def _xcheck_section(text: str) -> str:
    """抽出跨所交叉驗證區塊全文（標題到下一個 `## ` 為止）；不存在回空字串。"""
    seg, grab = [], False
    for ln in text.split("\n"):
        if ln.startswith("## 🔀 跨所交叉驗證"):
            grab = True
        elif grab and ln.startswith("## "):
            break
        if grab:
            seg.append(ln)
    return "\n".join(seg)


def _render(cg: dict, bn: dict) -> str:
    """跑完整 deepdive 組裝，回跨所區塊（＝LLM 真正讀到的字）。"""
    state = {"coinglass": cg, "binance_xcheck": _binance_divergence(cg, bn)}
    return _xcheck_section(_format_symbol_data("BTC", state))


# 主源（CoinGlass）齊全的基準；區塊守門要求 cvd/oi/funding/ls_ratio 至少一個非 None
_CG_FULL = {"funding": 0.0001, "ls_ratio": 1.10, "cvd": None, "oi": None}
# 線上實況：CG 停權 ⇒ funding/ls_ratio 皆 None，但 cvd 有值讓佐證區塊仍會進
_CG_DEAD = {"funding": None, "ls_ratio": None, "cvd": [1.0, 2.0], "cvd_slope": 3.5}
_BN_FULL = {"funding": 0.00008, "ls_ratio": 1.12}


# ═══════ (1) 主源全缺＝沒比對過：⛔ 不可宣稱一致、不可加信心 ═══════

def test_dead_primary_source_must_not_claim_agreement():
    """線上實況重演：主源 funding/ls_ratio 皆 None、Binance 有值 ⇒ 一次都沒比。"""
    sec = _render(_CG_DEAD, _BN_FULL)
    assert sec, "區塊整個不見了"
    assert "一致" not in sec, f"一次都沒比對過，卻宣稱與主源一致：\n{sec}"
    assert "可信度較高" not in sec, f"憑空給訊號可信度加分：\n{sec}"


def test_dead_primary_source_must_say_it_was_not_compared():
    """不可只是把 ✅ 拿掉就安靜——安靜同樣會被讀成『沒問題』。要明講沒比成。"""
    sec = _render(_CG_DEAD, _BN_FULL)
    assert "沒比對" in sec or "比不了" in sec, f"沒比成卻不出聲：\n{sec}"
    assert "⛔" in sec, f"缺『不可據此推論』的禁令：\n{sec}"


def test_partial_primary_source_names_which_item_was_compared():
    """只有 funding 比得成、ls_ratio 缺 ⇒ 要分開講：哪一項比過、哪一項沒比成。

    ⛔ 這裡不可只斷言「輸出裡有『多空比』」——舊碼本來就印著
    「- Binance 大戶多空比：1.12」，那樣的測試會在 HEAD 上為錯的理由變綠。
    """
    cg = {"funding": 0.0001, "ls_ratio": None, "cvd": [1.0]}
    sec = _render(cg, _BN_FULL)
    unc = [ln for ln in sec.split("\n") if "沒比對成" in ln]
    assert unc, f"ls_ratio 沒比成卻完全不出聲：\n{sec}"
    assert "大戶多空比" in unc[0], f"沒比成的項目未被指名：\n{sec}"
    assert "資金費率" not in unc[0], f"資金費率明明比過了卻被列為沒比成：\n{sec}"
    ok = [ln for ln in sec.split("\n") if "✅" in ln]
    assert ok and "資金費率" in ok[0], f"比過的那一項未被指名：\n{sec}"
    assert "大戶多空比" not in ok[0], f"沒比過的項目被算進已比對：\n{sec}"


# ═══════ (2) 產出端必須把「比過」與「沒比成」分開寫 ═══════

def test_divergence_records_uncompared_items():
    out = _binance_divergence(_CG_DEAD, _BN_FULL)
    assert out.get("compared") == [], f"沒有任何一項比得成，compared 應為空：{out}"
    assert out.get("uncompared"), f"沒比成的項目未被記錄，下游無從分辨：{out}"


def test_divergence_records_compared_items_when_both_present():
    out = _binance_divergence(_CG_FULL, _BN_FULL)
    assert set(out.get("compared") or []) == {"資金費率", "大戶多空比"}, out
    assert not out.get("uncompared"), out


def test_zero_ratio_from_source_is_an_answer_not_missing_data():
    """邊界另一側：來源明講 0.0 是答案，不可被 truthiness 判成缺料。"""
    out = _binance_divergence({"funding": 0.0001, "ls_ratio": 0.0},
                              {"funding": 0.00008, "ls_ratio": 0.0})
    assert "大戶多空比" in (out.get("compared") or []), \
        f"來源回 0.0 被當成缺料（本物種的反方向）：{out}"


# ═══════ (3) Binance 側鍵在值全 None ⇒ 不可只印一句 ✅ ═══════

def test_all_none_binance_dict_must_not_render_bare_checkmark():
    """`_fetch_binance_raw` 一律寫鍵（`fund.get("funding")` 缺值就是 None）⇒
    `{"funding": None, "ls_ratio": None}` 是**非空 dict**，舊碼 `if bn:` 判有料
    （v178 治過的同一個坑）⇒ 整段只剩標題 + 那句 ✅，零數據。"""
    sec = _render(_CG_FULL, {"funding": None, "ls_ratio": None})
    assert "可信度較高" not in sec, f"零數據卻給訊號加分：\n{sec}"
    assert "一致" not in sec, f"第二來源一個數字都沒有，卻宣稱一致：\n{sec}"


# ═══════ (4) 同函式同物種：CVD 斜率／OI 24h ═══════

def _cg_section(text: str) -> str:
    seg, grab = [], False
    for ln in text.split("\n"):
        if ln.startswith("## 📊 CoinGlass"):
            grab = True
        elif grab and ln.startswith("## "):
            break
        if grab:
            seg.append(ln)
    return "\n".join(seg)


def test_unknown_cvd_slope_must_not_be_called_flat():
    sec = _cg_section(_format_symbol_data(
        "BTC", {"coinglass": {"cvd": [10.0, 12.0], "cvd_slope": None}}))
    assert "走平" not in sec, f"斜率沒算出來被斷言成走平＝多空均衡：\n{sec}"
    assert "判不了" in sec or "沒算出來" in sec, f"缺料未出聲：\n{sec}"


def test_unknown_oi_delta_must_not_vanish_silently():
    sec = _cg_section(_format_symbol_data(
        "BTC", {"coinglass": {"oi": [1e9, 1.1e9], "oi_delta_24h": None}}))
    assert "24h" in sec, f"24h 變化整句靜靜消失，讀者不知道它缺了：\n{sec}"
    assert "持平" not in sec, f"沒算出來被寫成持平：\n{sec}"


# ═══════ (5) 反向側守門：資料齊全時逐字不變（HEAD 上就必須是綠的）═══════

def test_real_agreement_still_gets_the_checkmark():
    """兩所都有值且無背離＝答案，照舊給 ✅（誤標成缺料會反過來騙人）。"""
    sec = _render(_CG_FULL, _BN_FULL)
    assert "✅" in sec, f"真的比過且一致，卻不給 ✅：\n{sec}"
    assert "Binance 大戶多空比：1.12" in sec
    assert "沒比對" not in sec, f"資料齊全卻誤報缺料：\n{sec}"


def test_real_divergence_flag_unchanged():
    """背離照舊逐字印出。"""
    sec = _render({"funding": 0.0009, "ls_ratio": 1.10, "cvd": [1.0]},
                  {"funding": -0.0009, "ls_ratio": 1.12})
    assert "資金費率跨所背離" in sec, sec
    assert "⚠️" in sec and "兩所分歧，留意" in sec, sec


def test_known_cvd_slope_wording_unchanged():
    sec = _cg_section(_format_symbol_data(
        "BTC", {"coinglass": {"cvd": [10.0, 12.0], "cvd_slope": 5.5}}))
    assert "上升=買方主動吸籌" in sec, sec


def test_known_oi_delta_wording_unchanged():
    sec = _cg_section(_format_symbol_data(
        "BTC", {"coinglass": {"oi": [1e9, 1.1e9], "oi_delta_24h": 3.25}}))
    assert "24h +3.25%（增倉）" in sec, sec
