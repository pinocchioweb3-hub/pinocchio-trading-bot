

# ------------------------------------------------- v117 餓死型提早轉市價（稽核rank2）
def test_early_convert_ready_pure_judge():
    """純函式判準：掛齡+方向進度雙條件；價在壞側/太年輕/退化風險距離一律 False。"""
    from l3_dispatcher.demo_operator import early_convert_ready
    H = 3600 * 1000
    now = 10 * H
    # bull：entry 100 stop 96（R=4）。價 101（進度 0.25R）且掛齡 2h → True
    assert early_convert_ready("bull", 100, 96, 101.0, now - 2 * H, now,
                               min_hours=1.5, progress_frac=0.25)
    # 太年輕（1h < 1.5h）→ False
    assert not early_convert_ready("bull", 100, 96, 101.0, now - 1 * H, now,
                                   min_hours=1.5, progress_frac=0.25)
    # 進度不足（100.5 = 0.125R）→ False
    assert not early_convert_ready("bull", 100, 96, 100.5, now - 2 * H, now,
                                   min_hours=1.5, progress_frac=0.25)
    # 價在壞側（99 < entry，限價等成交中）→ False
    assert not early_convert_ready("bull", 100, 96, 99.0, now - 5 * H, now,
                                   min_hours=1.5, progress_frac=0.25)
    # bear 對稱：entry 100 stop 104，價 99 → True；價 101（壞側）→ False
    assert early_convert_ready("bear", 100, 104, 99.0, now - 2 * H, now,
                               min_hours=1.5, progress_frac=0.25)
    assert not early_convert_ready("bear", 100, 104, 101.0, now - 2 * H, now,
                                   min_hours=1.5, progress_frac=0.25)
    # 退化：risk=0 / cur=0 / 缺價 → False（不臆測）
    assert not early_convert_ready("bull", 100, 100, 101.0, now - 2 * H, now, 1.5, 0.25)
    assert not early_convert_ready("bull", 100, 96, 0, now - 2 * H, now, 1.5, 0.25)
    assert not early_convert_ready("bull", None, 96, 101.0, now - 2 * H, now, 1.5, 0.25)


# ------------------------------------------------- v119 alt 同向持倉總閘（稽核rank7）
def test_alt_same_dir_cap_pure_gate():
    from l3_dispatcher.demo_operator import alt_same_dir_blocked
    mk = lambda s, d, st="open": {"symbol": s, "direction": d, "status": st}
    three_bull_alts = [mk("SOL", "bull"), mk("AAVE", "bull"), mk("SUI", "bull", "pending")]
    # 第 4 筆同向 alt → 擋
    assert alt_same_dir_blocked("APT", "bull", three_bull_alts, cap=3)
    # 反方向不擋（空單不受多單佔額影響）
    assert not alt_same_dir_blocked("APT", "bear", three_bull_alts, cap=3)
    # 主流 BTC/ETH 永不擋
    assert not alt_same_dir_blocked("BTC", "bull", three_bull_alts, cap=3)
    assert not alt_same_dir_blocked("ETH", "bull", three_bull_alts, cap=3)
    # 在場含 BTC/ETH 不計入 alt 額度
    mixed = [mk("BTC", "bull"), mk("ETH", "bull"), mk("SOL", "bull")]
    assert not alt_same_dir_blocked("APT", "bull", mixed, cap=3)   # alt 僅 1 筆
    # 已平倉/拒單不計
    stale = three_bull_alts + [mk("LTC", "bull", "closed"), mk("OP", "bull", "rejected")]
    assert alt_same_dir_blocked("APT", "bull", stale, cap=3)       # 仍是 3 筆活的 → 擋
    assert not alt_same_dir_blocked("APT", "bull", three_bull_alts[:2], cap=3)  # 2<3 不擋


# ------------------------------------------------- v127 智能動態倉位閘（使用者設計）
def test_dynamic_slot_gate_base_and_max():
    from l3_dispatcher.demo_operator import dynamic_slot_gate
    mk = lambda s, st="open", ct=10.0: {"symbol": s, "pos_side": "short",
                                        "status": st, "contracts": ct}
    # 基礎槽：在場 2 < 3 → 放行
    ok, why = dynamic_slot_gate([mk("ETH"), mk("SOL")], {}, base=3, bonus=2)
    assert ok and "base_slot" in why
    # 絕對上限：在場 5 ≥ 3+2 → 擋（就算全鎖利）
    five = [mk(s) for s in ("A", "B", "C", "D", "E")]
    sizes = {(t["symbol"], "short"): 4.0 for t in five}      # 全部已減倉=鎖利
    ok, why = dynamic_slot_gate(five, sizes, base=3, bonus=2)
    assert not ok and "max_slots" in why


def test_dynamic_slot_gate_profit_unlock():
    from l3_dispatcher.demo_operator import dynamic_slot_gate
    mk = lambda s, st="open", ct=10.0: {"symbol": s, "pos_side": "short",
                                        "status": st, "contracts": ct}
    three = [mk("ETH"), mk("SOL"), mk("FIL")]
    # ①三單皆已鎖利（OKX 現量 < 原始張數 → TP 腿落袋）→ 解鎖第 4 槽
    locked = {("ETH", "short"): 4.0, ("SOL", "short"): 6.0, ("FIL", "short"): 5.0}
    ok, why = dynamic_slot_gate(three, locked, base=3, bonus=2)
    assert ok and "profit_unlocked" in why
    # ②任一單未鎖利（現量=原始）→ 不解鎖
    partial = {**locked, ("FIL", "short"): 10.0}
    ok, why = dynamic_slot_gate(three, partial, base=3, bonus=2)
    assert not ok and "FIL" in why
    # ③有 pending 掛單 → 不可能鎖利 → 不解鎖
    with_pending = three[:2] + [mk("APT", st="pending")]
    ok, why = dynamic_slot_gate(with_pending, locked, base=3, bonus=2)
    assert not ok and "pending" in why
    # ④OKX 查不到現量 → fail-closed 不解鎖
    ok, why = dynamic_slot_gate(three, {}, base=3, bonus=2)
    assert not ok


# ------------------------------------------------- v130 同幣同向併倉防護
def test_dup_position_merge_risk_logic():
    """同幣同向在場（pending/open）→ 應被拒（OKX hedge 併倉歸屬歧義防護）。
    純邏輯驗證（鏡像 _place_one 內的判斷式）。"""
    open_trades = [{"symbol": "BTC", "direction": "bear", "status": "open"},
                   {"symbol": "LAB", "direction": "bull", "status": "pending"},
                   {"symbol": "SOL", "direction": "bull", "status": "closed"}]
    def dup(symbol, direction):
        return any(t.get("symbol") == symbol and t.get("direction") == direction
                   and t.get("status") in ("pending", "open") for t in open_trades)
    assert dup("BTC", "bear")            # 同幣同向 open → 擋
    assert dup("LAB", "bull")            # 同幣同向 pending → 擋
    assert not dup("BTC", "bull")        # 反向 → 放行
    assert not dup("SOL", "bull")        # closed 不算在場 → 放行
    assert not dup("ETH", "bear")        # 不同幣 → 放行
