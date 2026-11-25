"""产线可用性账手算例与端到端验证（全部数据在内存构造，不读取外部文件）。

时间轴为整数、半开 [lo, hi)。运行: python3 demo_availability.py
"""

from maint_facts import (
    DOWN,
    LOC,
    WO,
    AvailabilityIndex,
    Dependency,
    DependencyCycleError,
    DeviceEvidence,
    DownRef,
    EdgeRef,
    Event,
    Interval,
    MaintenanceFactEngine,
    MonitorRecord,
    STATE_AVAILABLE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    recompute_from_basis,
)
from maint_facts.availability import DEVICE, SERVICE


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(f"手算核对失败: {msg}")
    print(f"  [OK] {msg}")


def check_report(report, service, window) -> None:
    """所有报告共有的不变式：覆盖整窗、连续、相邻段已合并、依据可复算。"""
    segs = report.segments
    check(segs[0].interval.lo == window.lo and segs[-1].interval.hi == window.hi,
          "分段覆盖整个查询窗")
    check(all(a.interval.hi == b.interval.lo for a, b in zip(segs, segs[1:])),
          "分段连续无缝")
    check(all(not (a.state == b.state and a.basis == b.basis)
              for a, b in zip(segs, segs[1:])),
          "相邻段不冗余（同状态同依据者已合并）")
    check(sum(s.interval.length for s in segs) == window.length,
          "分段总长 = 窗长")
    check(sum(report.durations().values()) == window.length,
          "各状态时长合计 = 窗长")
    check(all(recompute_from_basis(s.basis, service) == s.state for s in segs),
          "每段依据可独立复算出相同状态")


def show(report) -> None:
    print(f"服务 {report.service} 窗 [{report.window.lo},{report.window.hi}) 分段:")
    for seg in report.segments:
        print(f"  [{seg.interval.lo},{seg.interval.hi}) {seg.state}")
        for v in seg.basis.services:
            print(f"      服务 {v.service}: 门槛={v.threshold} "
                  f"可用={v.available} 未知={v.unknown} 不可用={v.unavailable} -> {v.state}")
        for d in seg.basis.devices:
            extra = ""
            if d.down_events:
                extra = " 停机=" + ",".join(f"{r.eid}#rev{r.revision}" for r in d.down_events)
            if d.monitor_records:
                extra = " 监测=" + ",".join(d.monitor_records)
            print(f"      设备 {d.device}: {d.state}{extra}")
    print(f"  各状态时长: {report.durations()}")


# --------------------------- 例1：设备三态与服务门槛 ---------------------------
def example_1_device_states_and_threshold() -> None:
    print("=" * 78)
    print("例1 设备三态（停机>监测>未知）与服务门槛裁定")
    print("=" * 78)
    print(
        "手算设定: SVC 依赖 P1/P2/P3（[0,20) 有效），门槛 2；窗 [0,12)。\n"
        "  P1 监测 m-p1[0,20)            -> 全程可用\n"
        "  P2 监测 m-p2[0,20)，停机 d-p2[5,9) rev3（工单 W1 名下）-> [5,9) 不可用\n"
        "  P3 监测 m-p3[0,4)              -> [0,4) 可用，之后未知\n"
        "手算: [0,4) 3可用 -> 可用；[4,5) 2可用1未知 -> 可用；\n"
        "      [5,9) 1可用1未知1停机 -> 未知（1<2 且 1+1>=2）；[9,12) 2可用1未知 -> 可用。"
    )
    engine = MaintenanceFactEngine()
    engine.bootstrap([
        Event("loc-p2", 1, LOC, {"asset": "P2", "loc": "L1", "interval": Interval(0, 20)}),
        Event("wo-p2", 1, WO, {"asset": "P2", "wo": "W1", "interval": Interval(0, 20), "priority": 1}),
        Event("d-p2", 3, DOWN, {"asset": "P2", "interval": Interval(5, 9)}),
    ])
    index = AvailabilityIndex(
        engine,
        monitors=[
            MonitorRecord("m-p1", "P1", Interval(0, 20)),
            MonitorRecord("m-p2", "P2", Interval(0, 20)),
            MonitorRecord("m-p3", "P3", Interval(0, 4)),
        ],
        dependencies=[
            Dependency.on_device("SVC", "P1", Interval(0, 20)),
            Dependency.on_device("SVC", "P2", Interval(0, 20)),
            Dependency.on_device("SVC", "P3", Interval(0, 20)),
        ],
        thresholds={"SVC": 2},
    )
    report = index.query("SVC", Interval(0, 12))
    show(report)

    segs = report.segments
    check([(s.interval.lo, s.interval.hi, s.state) for s in segs] == [
        (0, 4, STATE_AVAILABLE),
        (4, 5, STATE_AVAILABLE),
        (5, 9, STATE_UNKNOWN),
        (9, 12, STATE_AVAILABLE),
    ], "分段与手算一致（4 段）")
    check(report.durations() == {STATE_AVAILABLE: 8, STATE_UNAVAILABLE: 0, STATE_UNKNOWN: 4},
          "时长：可用 4+1+3=8，未知 4")

    # 不可用段的依据：命中停机事件 ID+修订号；停机时段监测记录不计入命中
    dev_p2 = next(d for d in segs[2].basis.devices if d.device == "P2")
    check(dev_p2 == DeviceEvidence("P2", STATE_UNAVAILABLE, (DownRef("d-p2", 3),), ()),
          "P2 [5,9) 不可用，依据命中 d-p2 rev3（不论记到 W1 哪张工单）")
    dev_p3 = next(d for d in segs[2].basis.devices if d.device == "P3")
    check(dev_p3 == DeviceEvidence("P3", STATE_UNKNOWN, (), ()),
          "P3 [5,9) 无监测覆盖 -> 未知，无命中记录")
    verdict = next(v for v in segs[2].basis.services if v.service == "SVC")
    check((verdict.threshold, verdict.available, verdict.unknown, verdict.children) == (2, 1, 1, 3),
          "SVC [5,9) 计票：门槛2 可用1 未知1 -> 未知")
    # [4,5) 与 [9,12) 状态与依据完全相同，但不相邻 -> 不合并
    check(segs[1].basis == segs[3].basis, "[4,5) 与 [9,12) 依据相同（被 [5,9) 隔开，仍各为一段）")
    check_report(report, "SVC", Interval(0, 12))


# --------------------------- 例2：嵌套服务与共享依赖 ---------------------------
def example_2_nested_shared_deps() -> None:
    print("\n" + "=" * 78)
    print("例2 嵌套服务：共享依赖只记一次，相同下级只计一个")
    print("=" * 78)
    print(
        "手算设定: TOP 依赖 MID1/MID2 门槛2；MID1 依赖 DA/DB 门槛1；\n"
        "          MID2 依赖 DB/DC 门槛2（DB 被两条路径共享）；\n"
        "          MID1->DA 另有一条重复依赖记录（相同下级）。\n"
        "  DA/DB 有监测全程可用；DC 无监测 -> 未知。\n"
        "手算: MID1 可用2>=1 -> 可用；MID2 可用1+未知1，门槛2 -> 未知；\n"
        "      TOP 可用1+未知1，门槛2 -> 未知。"
    )
    index = AvailabilityIndex(
        [],  # 无停机事件
        monitors=[
            MonitorRecord("m-a", "DA", Interval(0, 10)),
            MonitorRecord("m-b", "DB", Interval(0, 10)),
        ],
        dependencies=[
            Dependency.on_service("TOP", "MID1", Interval(0, 10)),
            Dependency.on_service("TOP", "MID2", Interval(0, 10)),
            Dependency.on_device("MID1", "DA", Interval(0, 10)),
            Dependency.on_device("MID1", "DB", Interval(0, 10)),
            Dependency.on_device("MID1", "DA", Interval(0, 10)),  # 重复记录：相同下级
            Dependency.on_device("MID2", "DB", Interval(0, 10)),
            Dependency.on_device("MID2", "DC", Interval(0, 10)),
        ],
        thresholds={"TOP": 2, "MID1": 1, "MID2": 2},
    )
    report = index.query("TOP", Interval(0, 10))
    show(report)

    segs = report.segments
    check(len(segs) == 1 and segs[0].state == STATE_UNKNOWN, "整窗一段：未知")
    basis = segs[0].basis
    check(len(basis.edges) == 6, "7 条依赖记录在依据中去重为 6 条边（重复 MID1->DA 只记一次）")
    check(len(basis.devices) == 3, "共享设备 DB 只记一次：设备证据共 3 条（DA/DB/DC）")
    check(sorted(v.service for v in basis.services) == ["MID1", "MID2", "TOP"],
          "共享服务不按路径重复展开：3 个服务各一条裁定")
    v_mid1 = next(v for v in basis.services if v.service == "MID1")
    check((v_mid1.children, v_mid1.available, v_mid1.state) == (2, 2, STATE_AVAILABLE),
          "MID1 下级去重为 2（DA 只计一个），可用 -> 可用")
    v_mid2 = next(v for v in basis.services if v.service == "MID2")
    check((v_mid2.available, v_mid2.unknown, v_mid2.state) == (1, 1, STATE_UNKNOWN),
          "MID2 可用1+未知1 门槛2 -> 未知")
    check(report.accessed.services == frozenset({"TOP", "MID1", "MID2"}),
          "只访问可达服务 TOP/MID1/MID2")
    check(report.accessed.monitor_records == frozenset({"m-a", "m-b"}),
          "只命中窗内可达设备的监测记录")
    check_report(report, "TOP", Interval(0, 10))


# --------------------------- 例3：生效区间切分与相邻合并 ---------------------------
def example_3_segmentation_and_merging() -> None:
    print("\n" + "=" * 78)
    print("例3 依赖生效区间切分；状态与依据都相同的相邻段合并")
    print("=" * 78)
    print(
        "手算设定: S 门槛1；S->D1 有两条记录 [0,6) 与 [3,10)（相同下级只计一个）；\n"
        "          S->D2 [0,10)。D1 监测 m1[0,10)；D2 监测 m2[0,10)、停机 d2[4,8) rev2。\n"
        "手算: 全程至少 D1 可用 -> 全程可用；[4,8) 的依据含 D2 停机，\n"
        "      边界 3 与 6 处依据不变 -> 合并，最终 3 段：[0,4) [4,8) [8,10)。"
    )
    engine = MaintenanceFactEngine()
    engine.bootstrap([
        Event("d2", 2, DOWN, {"asset": "D2", "interval": Interval(4, 8)}),
    ])
    index = AvailabilityIndex(
        engine,
        monitors=[
            MonitorRecord("m1", "D1", Interval(0, 10)),
            MonitorRecord("m2", "D2", Interval(0, 10)),
        ],
        dependencies=[
            Dependency.on_device("S", "D1", Interval(0, 6)),
            Dependency.on_device("S", "D1", Interval(3, 10)),
            Dependency.on_device("S", "D2", Interval(0, 10)),
        ],
        thresholds={"S": 1},
    )
    report = index.query("S", Interval(0, 10))
    show(report)

    segs = report.segments
    check([(s.interval.lo, s.interval.hi, s.state) for s in segs] == [
        (0, 4, STATE_AVAILABLE),
        (4, 8, STATE_AVAILABLE),
        (8, 10, STATE_AVAILABLE),
    ], "合并后恰 3 段：边界 3/6 处依据不变被合并，4/8 处依据变化保留")
    check(report.durations()[STATE_AVAILABLE] == 10, "全程可用，可用时长 10")
    v = next(v for v in segs[0].basis.services if v.service == "S")
    check((v.children, v.available) == (2, 2), "下级去重为 2（D1 两条记录只计一个）")
    check(set(segs[0].basis.edges) == {
        EdgeRef("S", DEVICE, "D1"), EdgeRef("S", DEVICE, "D2"),
    }, "依据中的可达依赖：S->D1、S->D2 各一条")
    dev_d2 = next(d for d in segs[1].basis.devices if d.device == "D2")
    check(dev_d2.down_events == (DownRef("d2", 2),), "[4,8) D2 不可用，命中 d2 rev2")
    check_report(report, "S", Interval(0, 10))


# --------------------------- 例4：服务依赖环 ---------------------------
def example_4_dependency_cycle() -> None:
    print("\n" + "=" * 78)
    print("例4 可达且生效的依赖环 -> 整次查询报错；不可达/不同时生效的环不报错")
    print("=" * 78)

    # A) 可达环：A->B->C->A 在 [0,10) 全程有效
    index = AvailabilityIndex([], dependencies=[
        Dependency.on_service("A", "B", Interval(0, 10)),
        Dependency.on_service("B", "C", Interval(0, 10)),
        Dependency.on_service("C", "A", Interval(0, 10)),
    ])
    try:
        index.query("A", Interval(0, 5))
        raise AssertionError("应抛出 DependencyCycleError")
    except DependencyCycleError as exc:
        check(exc.cycle == ["A", "B", "C", "A"], f"报出环 {' -> '.join(exc.cycle)}")
        check(exc.effective == Interval(0, 5), "环在查询窗内可达且生效的时段 [0,5)")
        print(f"  错误信息: {exc}")

    # B) 自环
    index_self = AvailabilityIndex([], dependencies=[
        Dependency.on_service("X", "X", Interval(0, 4)),
    ])
    try:
        index_self.query("X", Interval(0, 4))
        raise AssertionError("应抛出 DependencyCycleError")
    except DependencyCycleError as exc:
        check(exc.cycle == ["X", "X"], "自环 X->X 也被检出")

    # C) 不可达的环：B<->C 存在但从 ROOT 不可达 -> 不报错，也不访问 B/C
    index_far = AvailabilityIndex(
        [],
        monitors=[MonitorRecord("m-k", "K", Interval(0, 10))],
        dependencies=[
            Dependency.on_device("ROOT", "K", Interval(0, 10)),
            Dependency.on_service("B", "C", Interval(0, 10)),
            Dependency.on_service("C", "B", Interval(0, 10)),
        ],
        thresholds={"ROOT": 1},
    )
    report = index_far.query("ROOT", Interval(0, 10))
    check(len(report.segments) == 1 and report.segments[0].state == STATE_AVAILABLE,
          "不可达的环不影响查询：ROOT 可用")
    check(report.accessed.services == frozenset({"ROOT"}),
          "不可达服务 B/C 完全不被访问")

    # D) 环边不同时生效：A->B [0,5)，B->A [5,10) -> 无环
    index_shift = AvailabilityIndex([], dependencies=[
        Dependency.on_service("A", "B", Interval(0, 5)),
        Dependency.on_service("B", "A", Interval(5, 10)),
    ])
    report = index_shift.query("A", Interval(0, 10))
    check([s.state for s in report.segments] == [STATE_AVAILABLE, STATE_AVAILABLE],
          "边不同时生效则不成环：两段均可用（门槛默认 0）")
    check(len(report.segments) == 2, "依据不同（[0,5) 有边 A->B）-> 两段")

    # E) 环在查询窗之外才生效：不报错
    index_later = AvailabilityIndex([], dependencies=[
        Dependency.on_service("A", "B", Interval(100, 200)),
        Dependency.on_service("B", "A", Interval(100, 200)),
    ])
    report = index_later.query("A", Interval(0, 5))
    check(len(report.segments) == 1 and report.segments[0].state == STATE_AVAILABLE,
          "环边在窗外才生效 -> 窗内无环，A 可用")


# --------------------------- 例5：只读查询，不改动检修账 ---------------------------
def example_5_read_only() -> None:
    print("\n" + "=" * 78)
    print("例5 查询不改动检修账：版本/事实/备查账完全不变")
    print("=" * 78)
    engine = MaintenanceFactEngine()
    engine.bootstrap([
        Event("loc-p1", 1, LOC, {"asset": "P1", "loc": "L1", "interval": Interval(0, 10)}),
        Event("wo-p1", 1, WO, {"asset": "P1", "wo": "W1", "interval": Interval(0, 10), "priority": 1}),
        Event("d-p1", 1, DOWN, {"asset": "P1", "interval": Interval(2, 5)}),
        Event("loc-q", 1, LOC, {"asset": "Q", "loc": "L9", "interval": Interval(0, 10)}),
        Event("wo-q", 1, WO, {"asset": "Q", "wo": "W9", "interval": Interval(0, 10), "priority": 1}),
    ])
    snap_version = engine.version
    snap_facts = dict(engine.facts)
    snap_ledger = list(engine.ledger)

    index = AvailabilityIndex(
        engine,
        monitors=[MonitorRecord("m-p1", "P1", Interval(0, 10))],
        dependencies=[Dependency.on_device("SVC", "P1", Interval(0, 10))],
        thresholds={"SVC": 1},
    )
    r1 = index.query("SVC", Interval(0, 10))
    r2 = index.query("SVC", Interval(1, 8))  # 可重复查询
    check([s.state for s in r1.segments] == [STATE_AVAILABLE, STATE_UNAVAILABLE, STATE_AVAILABLE],
          "P1 停机 [2,5) -> SVC [2,5) 不可用，前后可用")
    check([s.state for s in r2.segments] == [STATE_AVAILABLE, STATE_UNAVAILABLE, STATE_AVAILABLE],
          "重复查询（子窗 [1,8)）结果一致")
    check(engine.version == snap_version and engine.proposal is None,
          "查询后检修账版本不变、无未决提案")
    check(dict(engine.facts) == snap_facts and list(engine.ledger) == snap_ledger,
          "查询后归属事实与备查账完全不变")
    check_report(r1, "SVC", Interval(0, 10))
    check_report(r2, "SVC", Interval(1, 8))


# --------------------------- 例6：访问边界与不逐时间单位推进 ---------------------------
def example_6_access_bound() -> None:
    print("\n" + "=" * 78)
    print("例6 只访问窗内实际可达部分；边界扫描不逐时间单位推进")
    print("=" * 78)

    def build(far_devices: int):
        events = [
            # D0 在查询窗 [0,1000) 之外的旧记录：不应被访问
            Event("d-old", 1, DOWN, {"asset": "D0", "interval": Interval(2000, 2100)}),
        ]
        monitors = [
            MonitorRecord("m0", "D0", Interval(0, 1000)),
            MonitorRecord("m-old", "D0", Interval(2000, 2100)),
        ]
        deps = [Dependency.on_device("CORE", "D0", Interval(0, 1000))]
        # 远隔设备与远隔服务子图：与 CORE 无关
        for i in range(far_devices):
            events.append(Event(f"fd-{i}", 1, DOWN,
                                {"asset": f"F{i}", "interval": Interval(1500, 1600)}))
            monitors.append(MonitorRecord(f"fm-{i}", f"F{i}", Interval(1400, 1700)))
        for i in range(50):
            deps.append(Dependency.on_service(f"FS{i}", f"FS{i+1}", Interval(1000, 2000)))
        deps.append(Dependency.on_service("FARROOT", "FS0", Interval(1000, 2000)))
        index = AvailabilityIndex(events, monitors=monitors, dependencies=deps,
                                  thresholds={"CORE": 1})
        return index

    r1 = build(200).query("CORE", Interval(0, 1000))
    check(len(r1.segments) == 1 and r1.segments[0].state == STATE_AVAILABLE,
          "窗长 1000 只产出 1 段（按边界扫描，不逐时间单位推进）")
    check(r1.accessed.services == frozenset({"CORE"}), "只访问服务 CORE")
    check(r1.accessed.devices == frozenset({"D0"}), "只访问设备 D0")
    check(r1.accessed.monitor_records == frozenset({"m0"}),
          "窗外旧监测 m-old 与远隔监测均不被访问")
    check(r1.accessed.down_events == frozenset(), "窗外旧停机 d-old 与远隔停机均不被访问")

    r2 = build(2000).query("CORE", Interval(0, 1000))
    check(r1 == r2, "远隔历史扩大 10 倍：查询结果与访问量完全不变")


def main() -> None:
    example_1_device_states_and_threshold()
    example_2_nested_shared_deps()
    example_3_segmentation_and_merging()
    example_4_dependency_cycle()
    example_5_read_only()
    example_6_access_bound()
    print("\n" + "=" * 78)
    print("全部可用性手算例核对通过。")
    print("=" * 78)


if __name__ == "__main__":
    main()
