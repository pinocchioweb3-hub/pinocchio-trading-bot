

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
