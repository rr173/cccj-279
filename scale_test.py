"""增量边界与规模测试：修订成本只随受影响资产增长，与全局历史/其他资产隔离。

运行: python3 scale_test.py
"""

import time

from maint_facts import DOWN, LOC, SPARE, WO, Event, Interval, MaintenanceFactEngine


def build(assets: int, events_per_asset: int) -> MaintenanceFactEngine:
    events = []
    for a in range(assets):
        asset = f"A{a:04d}"
        base = a * 1000
        for k in range(events_per_asset):
            lo = base + k * 10
            events.append(Event(
                f"{asset}-loc{k}", 1, LOC,
                {"asset": asset, "loc": f"L{k % 3}", "interval": Interval(lo, lo + 8)},
            ))
            events.append(Event(
                f"{asset}-wo{k}", 1, WO,
                {"asset": asset, "wo": f"W{k % 4}", "interval": Interval(lo, lo + 6),
                 "priority": (k % 3) + 1},
            ))
            if k % 2 == 0:
                events.append(Event(
                    f"{asset}-down{k}", 1, DOWN,
                    {"asset": asset, "interval": Interval(lo + 1, lo + 5)},
                ))
            if k % 3 == 0:
                events.append(Event(
                    f"{asset}-spare{k}", 1, SPARE,
                    {"asset": asset, "time": lo + 3, "qty": (k % 5) + 1},
                ))
    engine = MaintenanceFactEngine()
    engine.bootstrap(events)
    return engine


def main() -> None:
    # 固定单资产规模，扩大资产总数；修订其中一个资产，访问量应保持稳定
    per_asset = 200
    access_by_scale = {}
    elapsed = {}
    for assets in (10, 50, 200):
        engine = build(assets, per_asset)
        target = "A0000"
        # 修订目标资产上的一个工单优先级
        rev = Event(
            "A0000-wo10", 2, WO,
            {"asset": target, "wo": "W10", "interval": Interval(100, 106), "priority": 9},
        )
        t0 = time.perf_counter()
        proposal = engine.submit(rev)
        engine.publish()
        elapsed[assets] = time.perf_counter() - t0

        accessed_assets = {engine.store.get(eid).asset for eid in proposal.accessed_eids}
        access_by_scale[assets] = (len(proposal.accessed_eids), accessed_assets)

        assert accessed_assets <= {target}, (
            f"修订只允许访问受影响资产，实际访问: {accessed_assets}"
        )
        assert proposal.other_assets_unchanged, "其他资产必须保持不变"
        print(
            f"资产数={assets:4d} 总事件≈{assets * per_asset * 3:6d} | "
            f"本次访问事件={len(proposal.accessed_eids):4d}（全部属于 {accessed_assets}）"
            f" | 耗时={elapsed[assets] * 1000:7.2f}ms"
        )

    counts = [access_by_scale[n][0] for n in (10, 50, 200)]
    assert max(counts) - min(counts) <= per_asset, (
        f"访问量应基本不随资产总数增长，实际 {counts}"
    )
    # 访问量远小于全局事件总量
    engine_big = build(200, per_asset)
    total_events = len(engine_big.store.all_events())
    rev = Event("A0000-wo10", 3, WO,
                {"asset": "A0000", "wo": "W10", "interval": Interval(100, 106), "priority": 7})
    p = engine_big.submit(rev)
    engine_big.publish()
    ratio = len(p.accessed_eids) / total_events
    print(f"\n最大规模下：全局事件 {total_events}，单次修订仅访问 "
          f"{len(p.accessed_eids)} 个（占比 {ratio:.2%}）")
    assert ratio < 0.02, "单次修订访问占比应远低于全量"
    print("\n增量边界测试通过：修订成本与受影响资产数据量相关，与全局历史/其他资产隔离。")


if __name__ == "__main__":
    main()
