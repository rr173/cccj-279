"""可用性账随机化性质测试：边界扫描结果必须等价于逐时刻朴素裁定，
且查询只访问窗内实际可达的设备、服务与相关记录。

运行: python3 availability_property_test.py
"""

import random

from maint_facts import (
    DOWN,
    AvailabilityIndex,
    Dependency,
    Event,
    Interval,
    MonitorRecord,
    STATE_AVAILABLE,
    STATE_UNKNOWN,
    recompute_from_basis,
)
from maint_facts.availability import DEVICE, SERVICE


def rand_iv(rng, lo_max=40, hi_max=45) -> Interval:
    lo = rng.randrange(0, lo_max)
    hi = rng.randrange(lo + 1, hi_max + 1)
    return Interval(lo, hi)


def gen_case(rng):
    """随机生成无环依赖图（服务间边只允许小编号 -> 大编号）。"""
    n_svc = rng.randint(1, 6)
    n_dev = rng.randint(1, 6)
    services = [f"S{i}" for i in range(n_svc)]
    devices = [f"D{i}" for i in range(n_dev)]

    deps = []
    for i in range(n_svc):
        for j in range(i + 1, n_svc):
            if rng.random() < 0.3:
                deps.append(Dependency.on_service(services[i], services[j], rand_iv(rng)))
        for k in range(n_dev):
            if rng.random() < 0.25:
                deps.append(Dependency.on_device(services[i], devices[k], rand_iv(rng)))
    # 偶发重复下级边（测“相同下级只计一个”）
    if deps and rng.random() < 0.5:
        d = rng.choice(deps)
        deps.append(Dependency(d.parent, d.child, d.child_kind, rand_iv(rng)))

    monitors = []
    seq = 0
    for d in devices:
        for _ in range(rng.randint(0, 2)):
            monitors.append(MonitorRecord(f"m{seq}", d, rand_iv(rng)))
            seq += 1

    events = []
    for d in devices:
        for k in range(rng.randint(0, 2)):
            events.append(Event(
                f"down-{d}-{k}", rng.randint(1, 9), DOWN,
                {"asset": d, "interval": rand_iv(rng)},
            ))

    thresholds = {s: rng.randint(0, 3) for s in services}
    return services, devices, deps, monitors, events, thresholds


def brute_state_at(deps, thresholds, down_rows, mon_rows, service, t):
    """朴素参考：在时刻 t 逐步裁定（生成器保证无环）。"""
    adj = {}
    for dep in deps:
        if dep.interval.contains_point(t):
            adj.setdefault(dep.parent, []).append(dep)

    def dev_state(d):
        for lo, hi, _eid, _rev in down_rows.get(d, ()):  # 停机优先
            if lo <= t < hi:
                return "不可用"
        for lo, hi, _rid in mon_rows.get(d, ()):
            if lo <= t < hi:
                return "可用"
        return "未知"

    memo = {}

    def eval_svc(s):
        if s in memo:
            return memo[s]
        children = {(d.child_kind, d.child) for d in adj.get(s, ())}
        available = unknown = 0
        for kind, name in children:
            st = dev_state(name) if kind == DEVICE else eval_svc(name)
            if st == STATE_AVAILABLE:
                available += 1
            elif st == STATE_UNKNOWN:
                unknown += 1
        thr = thresholds.get(s, 0)
        if available >= thr:
            st = STATE_AVAILABLE
        elif available + unknown < thr:
            st = "不可用"
        else:
            st = STATE_UNKNOWN
        memo[s] = st
        return st

    return eval_svc(service)


def rows_by_device(events, monitors):
    down_rows, mon_rows = {}, {}
    for e in events:
        iv = e.interval()
        down_rows.setdefault(e.asset, []).append((iv.lo, iv.hi, e.eid, e.revision))
    for m in monitors:
        mon_rows.setdefault(m.device, []).append(
            (m.interval.lo, m.interval.hi, m.record_id)
        )
    return down_rows, mon_rows


def main() -> None:
    rng = random.Random(20261125)
    rounds = 300
    seg_total = 0

    for step in range(rounds):
        services, devices, deps, monitors, events, thresholds = gen_case(rng)
        down_rows, mon_rows = rows_by_device(events, monitors)
        index = AvailabilityIndex(events, monitors=monitors, dependencies=deps,
                                  thresholds=thresholds)

        service = rng.choice(services)
        window = rand_iv(rng, lo_max=44, hi_max=45)
        report = index.query(service, window)
        seg_total += len(report.segments)

        # 1) 逐时刻等价于朴素裁定
        for t in range(window.lo, window.hi):
            expect = brute_state_at(deps, thresholds, down_rows, mon_rows, service, t)
            got = report.state_at(t)
            assert got == expect, (
                f"step {step}: t={t} 状态不一致 扫描={got} 朴素={expect}"
            )

        # 2) 分段不变式：覆盖整窗、连续、相邻段已合并、时长守恒、依据可复算
        segs = report.segments
        assert segs[0].interval.lo == window.lo and segs[-1].interval.hi == window.hi
        for a, b in zip(segs, segs[1:]):
            assert a.interval.hi == b.interval.lo
            assert not (a.state == b.state and a.basis == b.basis), "相邻段应已合并"
        assert sum(s.interval.length for s in segs) == window.length
        assert sum(report.durations().values()) == window.length
        for s in segs:
            assert recompute_from_basis(s.basis, service) == s.state

        # 3) 访问边界：只访问窗内实际可达的服务/设备/记录
        expect_svcs = {service}
        queue = [service]
        while queue:
            svc = queue.pop()
            for dep in deps:
                if dep.parent == svc and dep.interval.intersects(window):
                    if dep.child_kind == SERVICE and dep.child not in expect_svcs:
                        expect_svcs.add(dep.child)
                        queue.append(dep.child)
        assert report.accessed.services == expect_svcs, (
            f"step {step}: 访问服务集 {report.accessed.services} != 可达集 {expect_svcs}"
        )

        expect_devs = set()
        for t in range(window.lo, window.hi):
            adj = {}
            for dep in deps:
                if dep.interval.contains_point(t):
                    adj.setdefault(dep.parent, []).append(dep)
            seen, stack = set(), [service]
            while stack:
                s = stack.pop()
                if s in seen:
                    continue
                seen.add(s)
                for dep in adj.get(s, ()):
                    if dep.child_kind == SERVICE:
                        stack.append(dep.child)
                    else:
                        expect_devs.add(dep.child)
        assert report.accessed.devices == expect_devs, (
            f"step {step}: 访问设备集 {report.accessed.devices} != 实际可达集 {expect_devs}"
        )

        expect_down = {
            e.eid for e in events
            if e.asset in expect_devs and e.interval().intersects(window)
        }
        expect_mon = {
            m.record_id for m in monitors
            if m.device in expect_devs and m.interval.intersects(window)
        }
        assert report.accessed.down_events == expect_down
        assert report.accessed.monitor_records == expect_mon

        if step % 50 == 49:
            print(f"  step {step + 1:3d}: 逐时刻等价 + 访问边界 全部一致，"
                  f"累计分段 {seg_total}")

    print(f"\n{rounds} 轮随机用例全部通过：扫描 == 逐时刻朴素裁定；"
          f"访问集 == 窗内实际可达集。")


if __name__ == "__main__":
    main()
