"""手算例与端到端验证（全部数据在内存构造，不读取外部文件）。

时间轴为整数、半开 [lo, hi)；迁移瞬间 t 归新位置。
运行: python3 demo_handcalc.py
"""

from maint_facts import (
    Agg,
    DOWN,
    LOC,
    SPARE,
    WO,
    Event,
    EventStore,
    Interval,
    LedgerEntry,
    MaintenanceFactEngine,
    PublishFailed,
    RevisionConflict,
    RevisionRejected,
    UNKNOWN,
)
from maint_facts.engine import (
    BUCKET_PENDING,
    BUCKET_UNASSIGNED,
    DOWN_NO_WO,
    DOWN_TIE,
    LOC_AMBIGUOUS,
    SPARE_EXPLICIT_INVALID,
    SPARE_NO_WO,
    SPARE_TIE,
)


# --------------------------- 断言辅助 ---------------------------
def fact(engine: MaintenanceFactEngine, asset: str, wo: str, loc: str) -> Agg:
    from maint_facts import FactKey

    return engine.facts.get(FactKey(asset, wo, loc), Agg())


def ledger_sum(engine: MaintenanceFactEngine, asset: str, reason: str) -> Agg:
    total = Agg()
    for e in engine.ledger_for_asset(asset):
        if e.reason == reason:
            total = total + e.amount
    return total


def ledger_count(engine: MaintenanceFactEngine, asset: str, reason: str | None = None) -> int:
    return sum(
        1
        for e in engine.ledger_for_asset(asset)
        if reason is None or e.reason == reason
    )


def show_asset(engine: MaintenanceFactEngine, asset: str, title: str) -> None:
    print(f"\n--- {title}：{asset} ---")
    print("归属事实:")
    rows = [(k, v) for k, v in engine.facts.items() if k.asset == asset]
    for k, v in sorted(rows, key=lambda kv: (kv[0].wo, kv[0].loc)):
        print(f"  工单={k.wo:<4} 位置={k.loc:<6} {v.pretty()}")
    print("未归属/待核:")
    if not engine.ledger_for_asset(asset):
        print("  （无）")
    for e in sorted(engine.ledger_for_asset(asset), key=lambda x: (x.interval.lo, x.reason)):
        extra = f" {e.detail}" if e.detail else ""
        wo = f" 工单={e.wo}" if e.wo else ""
        print(f"  [{e.bucket}] {e.reason} [{e.interval.lo},{e.interval.hi}) "
              f"{e.amount.pretty()}{wo}{extra}")


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(f"手算核对失败: {msg}")
    print(f"  [OK] {msg}")


# --------------------------- 例1：重叠工单优先级变动 ---------------------------
def example_1_priority_change() -> None:
    print("=" * 78)
    print("例1 重叠工单优先级变动：停机每刻只归最高优先级工单，修订后净变化")
    print("=" * 78)
    print(
        "手算设定: 资产 A 全程在 L1；工单 W1[2,12) 优先级1，W2[6,10) 优先级2；\n"
        "          停机 D[4,11)。修订 rev2 把 W1 优先级提到 5。"
    )
    engine = MaintenanceFactEngine()
    engine.bootstrap([
        Event("loc-a", 1, LOC, {"asset": "A", "loc": "L1", "interval": Interval(0, 12)}),
        Event("wo-w1", 1, WO, {"asset": "A", "wo": "W1", "interval": Interval(2, 12), "priority": 1}),
        Event("wo-w2", 1, WO, {"asset": "A", "wo": "W2", "interval": Interval(6, 10), "priority": 2}),
        Event("down-d", 1, DOWN, {"asset": "A", "interval": Interval(4, 11)}),
        # 窗之外的远隔历史事件，修订时不应被读取
        Event("wo-old", 1, WO, {"asset": "A", "wo": "W9", "interval": Interval(100, 110), "priority": 9}),
        Event("down-old", 1, DOWN, {"asset": "A", "interval": Interval(120, 130)}),
    ])
    show_asset(engine, "A", "修订前 v1")

    # 手算（修订前）
    # 工时只看工单自身区间：W1=10, W2=4
    check(fact(engine, "A", "W1", "L1").labor == 10, "W1 工时 10（工单自身区间 10）")
    check(fact(engine, "A", "W2", "L1").labor == 4, "W2 工时 4（工单自身区间 4）")
    # 停机 [4,11)=7：[4,6) 仅 W1 ->2；[6,10) W2 优先级高 ->4；[10,11) 仅 W1 ->1
    check(fact(engine, "A", "W1", "L1").down == 3, "停机 W1=2+1=3")
    check(fact(engine, "A", "W2", "L1").down == 4, "停机 W2=4（优先级更高的 [6,10)）")

    print("\n>> 提交 rev2: W1 优先级 1 -> 5（先暂存，展示净变化后再发布）")
    proposal = engine.submit(
        Event("wo-w1", 2, WO, {"asset": "A", "wo": "W1", "interval": Interval(2, 12), "priority": 5})
    )
    print(proposal.describe())
    v2 = engine.publish()
    check(v2 == 2, "发布后版本 v2")
    show_asset(engine, "A", "修订后 v2")

    # 手算（修订后）：停机 [4,11) 全部归 W1=7；W2 停机清零但工时仍为 4
    check(fact(engine, "A", "W1", "L1") == Agg(labor=10, down=7), "修订后 W1 工时10/停机7")
    check(fact(engine, "A", "W2", "L1") == Agg(labor=4), "修订后 W2 仅余工时4，停机被撤回")
    check(
        all(not e.in_window(Interval(2, 12)) for e in engine.ledger_for_asset("A")),
        "修订窗 [2,12) 内无未归属/待核（窗之外的远隔历史各自独立）",
    )
    check("wo-w2" in proposal.accessed_eids, "与窗相交的 W2 被读取（窗内重算所必需）")
    check(
        proposal.other_assets_unchanged,
        "增量只重算受影响资产 A：其他资产的事件与事实完全不访问、不变更",
    )


# --------------------------- 例2：迁移点 ---------------------------
def example_2_migration() -> None:
    print("\n" + "=" * 78)
    print("例2 设备迁移：工单工时按自身区间切位置，迁移瞬间归新位置")
    print("=" * 78)
    print(
        "手算设定: A 位于 L1[0,5)，t=5 迁至 L2[5,10)；工单 W[0,10)。\n"
        "          半开边界 t=5 归新位置 -> W@L1 工时5、W@L2 工时5。"
    )
    engine = MaintenanceFactEngine()
    engine.bootstrap([
        Event("loc-1", 1, LOC, {"asset": "A", "loc": "L1", "interval": Interval(0, 5)}),
        Event("loc-2", 1, LOC, {"asset": "A", "loc": "L2", "interval": Interval(5, 10)}),
        Event("wo-w", 1, WO, {"asset": "A", "wo": "W", "interval": Interval(0, 10), "priority": 1}),
    ])
    show_asset(engine, "A", "结果")
    check(fact(engine, "A", "W", "L1") == Agg(labor=5), "W@L1 工时 [0,5)=5")
    check(fact(engine, "A", "W", "L2") == Agg(labor=5), "W@L2 工时 [5,10)=5，瞬间归新位置")


# --------------------------- 例3：重复停机并集 ---------------------------
def example_3_overlapping_downtime() -> None:
    print("\n" + "=" * 78)
    print("例3 重复/重叠停机：真实时间并集只计一次，不重复计量")
    print("=" * 78)
    print(
        "手算设定: A 全程 L1；工单 W[0,10)；停机 D1[2,6) 与 D2[4,8) 重叠。\n"
        "          并集=[2,8) 长度6（不是 4+4=8）。"
    )
    engine = MaintenanceFactEngine()
    engine.bootstrap([
        Event("loc-a", 1, LOC, {"asset": "A", "loc": "L1", "interval": Interval(0, 10)}),
        Event("wo-w", 1, WO, {"asset": "A", "wo": "W", "interval": Interval(0, 10), "priority": 1}),
        Event("d1", 1, DOWN, {"asset": "A", "interval": Interval(2, 6)}),
        Event("d2", 1, DOWN, {"asset": "A", "interval": Interval(4, 8)}),
    ])
    show_asset(engine, "A", "结果")
    check(fact(engine, "A", "W", "L1") == Agg(labor=10, down=6),
          "W 工时10 不被停机反推；停机并集=6，无重复计量")
    check(not engine.ledger, "无备查条目")


# --------------------------- 例4：未归属（无工单 + 未知位置）---------------------------
def example_4_unassigned() -> None:
    print("\n" + "=" * 78)
    print("例4 未归属：无有效工单的停机/备件；无位置覆盖记为未知位置")
    print("=" * 78)
    print(
        "手算设定: A 的位置 L1[0,6)；工单 W[0,4)。\n"
        "          停机 D[5,9)：[5,6) 在 L1 无工单 -> 未归属1；\n"
        "                        [6,9) 无位置覆盖且无工单 -> 未归属3（未知位置）。\n"
        "          备件 S 在 t=7 数量5 -> 未归属5；W 自身工时4 落在 L1。"
    )
    engine = MaintenanceFactEngine()
    engine.bootstrap([
        Event("loc-a", 1, LOC, {"asset": "A", "loc": "L1", "interval": Interval(0, 6)}),
        Event("wo-w", 1, WO, {"asset": "A", "wo": "W", "interval": Interval(0, 4), "priority": 1}),
        Event("d", 1, DOWN, {"asset": "A", "interval": Interval(5, 9)}),
        Event("s", 1, SPARE, {"asset": "A", "time": 7, "qty": 5}),
    ])
    show_asset(engine, "A", "结果")
    check(fact(engine, "A", "W", "L1") == Agg(labor=4), "W@L1 工时4")
    check(ledger_sum(engine, "A", DOWN_NO_WO) == Agg(down=4), "未归属停机共 1+3=4")
    check(ledger_sum(engine, "A", SPARE_NO_WO) == Agg(spare_qty=5), "未归属备件5")
    unassigned = engine.unassigned("A")
    check(any(e.interval == Interval(6, 9) for e in unassigned), "含未知位置时段 [6,9) 的未归属条目")


# --------------------------- 例5：最高优先级并列 + 备件不拆量 ---------------------------
def example_5_tie_no_split() -> None:
    print("\n" + "=" * 78)
    print("例5 最高优先级并列 -> 待核；备件并列不拆量；指定工单校验")
    print("=" * 78)
    print(
        "手算设定: A 全程 L1；W1[0,10) 与 W2[2,10) 同为优先级1；停机 D[3,7)。\n"
        "          备件 S1 t=5 数量4 未指定工单（并列 -> 整笔4 待核，不拆成2+2）；\n"
        "          备件 S2 t=5 数量3 指定 W2（同资产且当时有效 -> W2 认领3）。"
    )
    engine = MaintenanceFactEngine()
    engine.bootstrap([
        Event("loc-a", 1, LOC, {"asset": "A", "loc": "L1", "interval": Interval(0, 10)}),
        Event("w1", 1, WO, {"asset": "A", "wo": "W1", "interval": Interval(0, 10), "priority": 1}),
        Event("w2", 1, WO, {"asset": "A", "wo": "W2", "interval": Interval(2, 10), "priority": 1}),
        Event("d", 1, DOWN, {"asset": "A", "interval": Interval(3, 7)}),
        Event("s1", 1, SPARE, {"asset": "A", "time": 5, "qty": 4}),
        Event("s2", 1, SPARE, {"asset": "A", "time": 5, "qty": 3, "wo": "W2"}),
    ])
    show_asset(engine, "A", "结果")
    check(fact(engine, "A", "W1", "L1") == Agg(labor=10), "W1 工时10（工时不看优先级）")
    check(fact(engine, "A", "W2", "L1") == Agg(labor=8, spare_qty=3), "W2 工时8 + 指定备件3")
    check(ledger_sum(engine, "A", DOWN_TIE) == Agg(down=4), "并列停机 [3,7)=4 整段待核")
    check(ledger_sum(engine, "A", SPARE_TIE) == Agg(spare_qty=4), "备件整笔4待核，未拆量")

    # 指定工单校验失败：t=1 时 W2 尚未生效
    print("\n>> 追加备件 S3 t=1 数量2 指定 W2（W2 当时无效 -> 待核）")
    p = engine.submit(Event("s3", 1, SPARE, {"asset": "A", "time": 1, "qty": 2, "wo": "W2"}))
    engine.publish()
    check(ledger_sum(engine, "A", SPARE_EXPLICIT_INVALID) == Agg(spare_qty=2),
          "指定工单当时无效 -> 待核2")
    check(fact(engine, "A", "W2", "L1").spare_qty == 3, "W2 备件仍为3，未被错误认领")
    print(p.describe().splitlines()[0])


# --------------------------- 例6：位置歧义待核 + 未知位置 ---------------------------
def example_6_ambiguous_location() -> None:
    print("\n" + "=" * 78)
    print("例6 同刻命中多个不同位置值 -> 待核；无位置 -> 未知位置（非待核）")
    print("=" * 78)
    print(
        "手算设定: A 位置 L1[0,6) 与 Lx[3,8) 冲突；工单 W[0,10)；备件 S t=4 数量7。\n"
        "          [3,6) 位置歧义：工时3、备件7 全部待核；\n"
        "          [6,8) 仅 Lx -> W@Lx 工时2；[8,10) 无位置 -> W@未知位置 工时2。"
    )
    engine = MaintenanceFactEngine()
    engine.bootstrap([
        Event("loc-1", 1, LOC, {"asset": "A", "loc": "L1", "interval": Interval(0, 6)}),
        Event("loc-x", 1, LOC, {"asset": "A", "loc": "Lx", "interval": Interval(3, 8)}),
        Event("w", 1, WO, {"asset": "A", "wo": "W", "interval": Interval(0, 10), "priority": 1}),
        Event("s", 1, SPARE, {"asset": "A", "time": 4, "qty": 7}),
    ])
    show_asset(engine, "A", "结果")
    check(fact(engine, "A", "W", "L1") == Agg(labor=3), "W@L1 工时 [0,3)=3")
    check(fact(engine, "A", "W", "Lx") == Agg(labor=2), "W@Lx 工时 [6,8)=2")
    check(fact(engine, "A", "W", UNKNOWN) == Agg(labor=2), "W@未知位置 工时 [8,10)=2")
    amb = ledger_sum(engine, "A", LOC_AMBIGUOUS)
    check(amb == Agg(labor=3, spare_qty=7), "歧义窗：工时3 与备件7 均待核")


# --------------------------- 例7：修订协议与发布失败 ---------------------------
def example_7_revision_protocol() -> None:
    print("\n" + "=" * 78)
    print("例7 修订协议：同号同内容无操作 / 同号异内容拒绝 / 高修订整份替换 / 发布失败留旧版")
    print("=" * 78)
    engine = MaintenanceFactEngine()
    engine.bootstrap([
        Event("loc-a", 1, LOC, {"asset": "A", "loc": "L1", "interval": Interval(0, 10)}),
        Event("w", 1, WO, {"asset": "A", "wo": "W", "interval": Interval(0, 10), "priority": 1}),
        Event("d", 1, DOWN, {"asset": "A", "interval": Interval(2, 5)}),
        # 另一资产，验证“其他资产不变”
        Event("loc-b", 1, LOC, {"asset": "B", "loc": "L9", "interval": Interval(0, 10)}),
        Event("wb", 1, WO, {"asset": "B", "wo": "WB", "interval": Interval(0, 10), "priority": 1}),
    ])
    snapshot_b = dict(engine.facts_for_asset("B"))
    check(engine.version == 1, "初始发布 v1")
    base_fact = fact(engine, "A", "W", "L1")
    check(base_fact == Agg(labor=10, down=3), "v1: W@L1 工时10/停机3")

    # 1) 同号同内容重送
    p = engine.submit(
        Event("d", 1, DOWN, {"asset": "A", "interval": Interval(2, 5)})
    )
    check(p.verdict == "NOOP", "同号同内容重送 -> NOOP")
    check(engine.publish() == 1, "NOOP 不产生新版本，仍为 v1")

    # 2) 同号异内容拒绝
    try:
        engine.submit(Event("d", 1, DOWN, {"asset": "A", "interval": Interval(2, 6)}))
        raise AssertionError("应抛出 RevisionConflict")
    except RevisionConflict as exc:
        check(True, f"同号异内容拒绝: {exc}")

    # 3) 低修订号拒绝
    try:
        engine.submit(Event("d", 0, DOWN, {"asset": "A", "interval": Interval(2, 6)}))
        raise AssertionError("应抛出 RevisionRejected")
    except (RevisionRejected, ValueError) as exc:
        check(True, f"非递增修订拒绝: {type(exc).__name__}")

    # 4) 高修订整份替换 + 发布失败回滚
    print("\n>> 提交停机 rev2: [2,5) -> [2,9)，模拟发布失败")
    proposal = engine.submit(
        Event("d", 2, DOWN, {"asset": "A", "interval": Interval(2, 9)})
    )
    print(proposal.describe())
    try:
        engine.publish(fail=True)
        raise AssertionError("应抛出 PublishFailed")
    except PublishFailed as exc:
        check(True, str(exc))
    check(engine.version == 1, "发布失败后仍为 v1")
    check(fact(engine, "A", "W", "L1") == Agg(labor=10, down=3), "失败回滚：停机恢复为3")
    check(engine.proposal is None, "失败重开后无未决提案")

    print("\n>> 重新提交并成功发布 rev2")
    proposal2 = engine.submit(
        Event("d", 2, DOWN, {"asset": "A", "interval": Interval(2, 9)})
    )
    check(engine.publish() == 2, "成功发布 -> v2")
    check(fact(engine, "A", "W", "L1") == Agg(labor=10, down=7), "v2: 停机撤回3记7，净+4")
    check(engine.facts_for_asset("B") == snapshot_b, "其他资产 B 的事实完全不变")
    check(all("wb" not in eid and "loc-b" not in eid for eid in proposal2.accessed_eids),
          "日常修订只访问受影响资产 A 的相交索引项，不重扫 B 的历史")


def main() -> None:
    example_1_priority_change()
    example_2_migration()
    example_3_overlapping_downtime()
    example_4_unassigned()
    example_5_tie_no_split()
    example_6_ambiguous_location()
    example_7_revision_protocol()
    print("\n" + "=" * 78)
    print("全部手算例核对通过。")
    print("=" * 78)


if __name__ == "__main__":
    main()
