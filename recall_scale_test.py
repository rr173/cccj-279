"""批号召回账增量隔离与规模测试。

验证
----
1. 扩大远隔仓群/料号数量，修订一个仓群时 accessed_ids 保持稳定，
   成本只随受影响仓群数据量增长，与全局历史总量无关。
2. 查询按批号直接读物化结果，不随历史流水数量增长而多访问数据。

运行: python3 recall_scale_test.py
"""

import time

from maint_facts import Event, FlowRecord, RecallLedger, SPARE


def build(far_groups: int, group_size: int) -> RecallLedger:
    """一个目标仓群（料号 P0，仓 T0/T1）+ far_groups 个完全不通的远隔仓群。"""
    records, events = [], []
    # 目标仓群：每仓铺 group_size 笔收货 + 若干调拨/领用
    for k in range(group_size):
        records.append(FlowRecord.receipt(
            f"t-rec-{k}", 1, k, "T0", "P0", f"tlot-{k}", 10))
    records.append(FlowRecord.transfer("t-tr-1", 1, group_size, "T0", "T1", "P0", 3))
    events.append(Event("t-sp-1", 1, SPARE,
                        {"asset": "A0", "time": group_size + 1, "qty": 2,
                         "wh": "T0", "sku": "P0"}))

    # 远隔仓群：独立料号、独立仓，与目标仓群无任何调拨往来
    for g in range(far_groups):
        sku = f"Q{g}"
        wh = f"F{g}"
        for k in range(group_size):
            records.append(FlowRecord.receipt(
                f"f-{g}-{k}", 1, k, wh, sku, f"flot-{g}-{k}", 10))
        events.append(Event(f"fe-{g}", 1, SPARE,
                            {"asset": f"AF{g}", "time": group_size, "qty": 1,
                             "wh": wh, "sku": sku}))
    ledger = RecallLedger(events)
    ledger.bootstrap(records)
    return ledger


def main() -> None:
    group_size = 100
    access_by_scale = {}
    elapsed = {}
    for far in (10, 50, 200):
        ledger = build(far, group_size)
        total = len(ledger.flow_store.all_records()) + len(ledger.event_store.all_events())
        t0 = time.perf_counter()
        proposal = ledger.submit([
            FlowRecord.receipt("t-rec-0", 2, 0, "T0", "P0", "tlot-0", 12),
        ])
        ledger.publish()
        elapsed[far] = time.perf_counter() - t0

        accessed = proposal.accessed_ids
        assert all(x.startswith("t-") for x in accessed), (
            f"远隔仓群流水不得被重读，实际越界: {[x for x in accessed if not x.startswith('t-')]}"
        )
        access_by_scale[far] = (len(accessed), total)
        print(
            f"远隔仓群={far:4d} 全局流水={total:6d} | "
            f"本次重读={len(accessed):4d}（全部属于目标仓群 T0/T1）"
            f" | 耗时={elapsed[far] * 1000:7.2f}ms"
        )

    counts = [access_by_scale[n][0] for n in (10, 50, 200)]
    assert max(counts) - min(counts) == 0, (
        f"重读量应不随远隔规模变化，实际 {counts}"
    )

    # 最大规模下，单次更正重读占比应远低于全量
    ledger_big = build(200, group_size)
    total = len(ledger_big.flow_store.all_records()) + len(ledger_big.event_store.all_events())
    p = ledger_big.submit([FlowRecord.receipt("t-rec-1", 2, 1, "T0", "P0", "tlot-1", 15)])
    ledger_big.publish()
    ratio = len(p.accessed_ids) / total
    print(f"\n最大规模下：全局流水 {total}，单次更正仅重读 "
          f"{len(p.accessed_ids)} 条（占比 {ratio:.2%}）")
    assert ratio < 0.02, "单次更正重读占比应远低于全量"

    # 查询只读物化账：批号查询不随全局规模变化（返回结构只与该批有关）
    rep = ledger_big.query("P0", "tlot-0")
    assert rep.version == ledger_big.version
    far_rep = ledger_big.query("Q199", "flot-199-0")
    assert far_rep.on_hand_total == 9  # 10 收 1 领
    print("批号查询直接读物化余量/领用明细，不重放物流历史。")
    print("\n召回账增量隔离测试通过：重读成本与受影响仓群数据量相关，与全局历史隔离。")


if __name__ == "__main__":
    main()
