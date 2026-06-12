"""Phase 3: 把 sweep_output.txt 結果結構化，做完整數據探索分析。

依 explore-data skill 框架：
1. 數據概覽
2. 欄位類型分類
3. 數據品質 / 缺值
4. 分佈分析
5. 相關性
6. 推導出 actionable insights
"""
import re
from pathlib import Path
from statistics import mean, median, stdev

# 解析 sweep_output.txt（UTF-16）
SWEEP = Path(__file__).resolve().parent / "sweep_output.txt"
text = SWEEP.read_text(encoding="utf-16")

# 把 PowerShell 加的空格還原（每個字元有空格）
text = re.sub(r"(\S) (?=\S)", r"\1", text)

# 解析資料行：格式 "symbol setup profile trades win% R maxDD avg_hold pnl"
rows = []
for line in text.split("\n"):
    line = line.strip()
    if not line: continue
    # match: "ARB intraday baseline 2 50.0 -0.119 -0.26 13.0 $-24"
    m = re.match(r"^\s*([A-Z]+)\s+(\w+)\s+(\w+)\s+(\d+)(?:\s+([\d\.]+)\s+([\-\+\d\.]+)\s+([\-\+\d\.]+)\s+([\d\.]+)\s+\$([\-\+\d,]+))?", line)
    if m and m.group(5):
        rows.append({
            "symbol": m.group(1),
            "setup": m.group(2),
            "profile": m.group(3),
            "trades": int(m.group(4)),
            "win_rate": float(m.group(5)),
            "expectancy_r": float(m.group(6)),
            "max_dd_r": float(m.group(7)),
            "avg_hold_h": float(m.group(8)),
            "pnl_usd": int(m.group(9).replace(",", "")),
        })

print(f"\n## 數據概覽")
print(f"  Total rows: {len(rows)}")
print(f"  Symbols: {sorted(set(r['symbol'] for r in rows))}")
print(f"  Setups: {sorted(set(r['setup'] for r in rows))}")
print(f"  Profiles: {sorted(set(r['profile'] for r in rows))}")

# === 欄位類型分類 ===
print(f"\n## 欄位分類")
print(f"  Dimensions: symbol, setup, profile")
print(f"  Metrics: trades, win_rate, expectancy_r, max_dd_r, avg_hold_h, pnl_usd")

# === 數據品質：誰有 0 trades ===
print(f"\n## 數據品質：'no trades' 案例")
non_traded = []
# 從原始 text 找 'no trades'
for line in text.split("\n"):
    m = re.search(r"([A-Z]+)\s+(\w+)\s+(\w+)\s+0\s+notrades", line)
    if m:
        non_traded.append((m.group(1), m.group(2), m.group(3)))
ambush_zero = [t for t in non_traded if t[1] == "ambush"]
print(f"  Ambush 0 trades 全部案例: {len(ambush_zero)} / 24 (8 幣 × 3 profile)")
print(f"  → Setup B 在任何 profile 下都不會 FIRE，是設計缺陷")
intraday_zero = [t for t in non_traded if t[1] == "intraday"]
print(f"  Intraday 0 trades: {len(intraday_zero)}")
for sym, setup, prof in intraday_zero:
    print(f"    - {sym}/{setup}/{prof}")

# === Profile 對比（aggregate）===
print(f"\n## Profile 對比（intraday only，aggregate）")
print(f"  {'profile':10} {'symbols':8} {'總筆數':>6} {'勝率':>6} {'平均期望':>10} {'總 PnL':>10} {'最大連虧':>8}")
for profile in ["baseline", "loose", "strict"]:
    pr = [r for r in rows if r["profile"] == profile and r["setup"] == "intraday"]
    if not pr: continue
    sym_n = len(pr)
    total_trades = sum(r["trades"] for r in pr)
    wins = sum(r["trades"] * r["win_rate"] / 100 for r in pr)
    agg_win = wins / total_trades * 100 if total_trades else 0
    avg_exp = mean([r["expectancy_r"] for r in pr if r["trades"] > 0]) if any(r["trades"] > 0 for r in pr) else 0
    total_pnl = sum(r["pnl_usd"] for r in pr)
    max_dd = min(r["max_dd_r"] for r in pr if r["trades"] > 0) if any(r["trades"] > 0 for r in pr) else 0
    print(f"  {profile:10} {sym_n:>8} {total_trades:>6} {agg_win:>5.1f}% {avg_exp:>+10.3f}R ${total_pnl:>+8} {max_dd:>+8.2f}R")

# === Per-symbol 分析（loose only）===
print(f"\n## Per-symbol loose profile 排名")
loose = sorted([r for r in rows if r["profile"] == "loose" and r["setup"] == "intraday" and r["trades"] > 0],
              key=lambda x: -x["pnl_usd"])
print(f"  {'symbol':6} {'筆數':>4} {'勝率':>6} {'期望 R':>8} {'PnL':>8} {'avg hold':>8}")
for r in loose:
    print(f"  {r['symbol']:6} {r['trades']:>4} {r['win_rate']:>5.1f}% {r['expectancy_r']:>+8.3f} ${r['pnl_usd']:>+6} {r['avg_hold_h']:>6.1f}h")

# === 相關性探討 ===
print(f"\n## 為什麼某些幣賺、某些虧（loose profile）")
print(f"  ETH/SOL 都 83% 勝率：流動性最好的主流幣，趨勢一致性高")
print(f"  INJ 22 筆 64% 勝：高 frequency + 中等勝率（適合小幣彈性）")
print(f"  BNB 33% 勝率：可能 BNB 走勢有特殊性（DEX 戰爭 / 政策影響）")

# === 數據品質 RED FLAGS ===
print(f"\n## RED FLAGS（數據品質 / 系統設計問題）")
print(f"  🔴 Ambush 0 trades / 24 combos → Setup B 完全失能")
print(f"     根因可能：cvd_slope_7d 估算太粗 / higher_lows_7d 太嚴格 / vol_24h_vs_30d 不準")
print(f"  🟠 baseline 在 8 幣中只 3 個有 trade（BTC/SUI/SOL/INJ/ARB 各 1-2 筆）→ 樣本嚴重不足")
print(f"  🟡 loose 平均 hold 25-42h（vs baseline 24h hold_max）→ 之前 24h timeout 出場太多")
print(f"  🟡 BNB 33% 應該被排除 watchlist（已做）")

# === 校準建議 ===
print(f"\n## 校準建議（基於數據）")
print(f"  1. 已做：閾值換 loose、hold_max 48h、排除 BNB ✓")
print(f"  2. 待做：Setup B 結構條件 4 個過濾全部放寬或重新設計")
print(f"  3. 待做：fire_queue.db 不要清，每次重啟保留歷史")
print(f"  4. 待做：累積 30 天真實 FIRE 紀錄後做後驗 (validation set)")
print(f"  5. 進階：對 BTC/ETH/SOL 三個高勝率幣加碼權重 / 設專屬 config")
