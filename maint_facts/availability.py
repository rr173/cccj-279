"""产线可用性账：可重复查询的只读分段状态账。

定位
----
排查产线服务中断时，回答“哪里确实停了、哪里只是监测缺报”：
对给定服务与半开时间窗，产出覆盖整窗的分段状态（可用/不可用/未知）、
各状态时长与每段的复算依据。本模块只读检修账，查询不改动任何已发布事实。

输入（同一次发布后的同一批快照）
--------------------------------
- 现行检修事件：取已发布事件中的停机（down）事件；停机不论归到哪张工单，
  覆盖即不可用。来源可为 MaintenanceFactEngine / EventStore / Event 可迭代。
- 设备监测覆盖区间：MonitorRecord(record_id, device, interval)。
- 服务依赖：Dependency(parent, child, child_kind, interval)，生效区间半开；
  下级可以是设备（DEVICE）或其他服务（SERVICE）。
- 服务门槛：{服务: 最少可用下级数}；未配置门槛的服务视为 0。

裁定规则（某时刻 t）
-------------------
设备：
1. 有现行停机事件覆盖 -> 不可用（停机优先，该时段监测记录不计入命中）；
2. 否则有监测覆盖 -> 可用；
3. 无覆盖 -> 未知。
服务（只按当时有效且从被查服务可达的依赖裁定；相同下级同一时刻只计一个）：
1. 已确定可用的下级数 >= 门槛 -> 可用；
2. 可用数 + 未知下级数 < 门槛 -> 不可用（即使未知下级全部可用也不够数）；
3. 其余 -> 未知。
若当时可达且生效的依赖中存在服务环，整次查询抛 DependencyCycleError，
指出至少一条环及查询窗内其确实可达且生效的一段时间。

分段与依据
----------
- 输出分段连续覆盖整个查询窗；状态与依据都相同的相邻段合并为一段。
- 每段依据（SegmentBasis）可独立复算：当时可达依赖（每条只记一次，
  不按路径重复展开）、各服务门槛与计票、各设备状态及命中的
  监测记录 ID / 停机事件 ID+修订号。recompute_from_basis 可校验。

性能
----
构建期仅为该批输入建索引（按设备/父服务有序）。查询按窗二分检索，
只访问窗内实际可达的设备、服务与相关记录（report.accessed 可审计），
按边界点扫描，不逐时间单位推进，成本与全厂历史总量无关。
索引是该批输入的快照：发布新批次后需重建索引。
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from typing import Iterable

from .engine import MaintenanceFactEngine
from .events import DOWN, Event, EventStore
from .intervals import Interval

# 三态
STATE_AVAILABLE = "可用"
STATE_UNAVAILABLE = "不可用"
STATE_UNKNOWN = "未知"
STATES = (STATE_AVAILABLE, STATE_UNAVAILABLE, STATE_UNKNOWN)

# 下级类型
DEVICE = "device"
SERVICE = "service"


class DependencyCycleError(Exception):
    """当时可达且生效的依赖中存在服务环：整次查询失败。

    cycle     : 环上的服务序列，首尾相同（如 ["A", "B", "C", "A"]）。
    effective : 查询窗内该环确实可达且生效的一段时间（半开）。
    """

    def __init__(self, cycle: list[str], effective: Interval) -> None:
        self.cycle = list(cycle)
        self.effective = effective
        super().__init__(
            "服务依赖环: "
            + " -> ".join(self.cycle)
            + f"；在 [{effective.lo},{effective.hi}) 内从被查服务可达且生效"
        )


@dataclass(frozen=True)
class MonitorRecord:
    """设备监测覆盖区间（半开）：覆盖即“有监测”，记录 ID 进入命中依据。"""

    record_id: str
    device: str
    interval: Interval

    def __post_init__(self) -> None:
        if not isinstance(self.record_id, str) or not self.record_id:
            raise ValueError("监测记录 ID 必须为非空字符串")
        if not isinstance(self.device, str) or not self.device:
            raise ValueError("监测记录的设备名必须为非空字符串")
        if not isinstance(self.interval, Interval):
            raise TypeError("监测覆盖区间必须为 Interval（半开）")


@dataclass(frozen=True)
class Dependency:
    """服务直接依赖：parent 服务在生效区间（半开）内依赖 child（设备或服务）。"""

    parent: str
    child: str
    child_kind: str  # DEVICE / SERVICE
    interval: Interval

    def __post_init__(self) -> None:
        if not isinstance(self.parent, str) or not self.parent:
            raise ValueError("依赖的父服务名必须为非空字符串")
        if not isinstance(self.child, str) or not self.child:
            raise ValueError("依赖的下级名必须为非空字符串")
        if self.child_kind not in (DEVICE, SERVICE):
            raise ValueError(f"下级类型必须为 {DEVICE!r} 或 {SERVICE!r}")
        if not isinstance(self.interval, Interval):
            raise TypeError("依赖生效区间必须为 Interval（半开）")

    @staticmethod
    def on_device(parent: str, device: str, interval: Interval) -> "Dependency":
        return Dependency(parent, device, DEVICE, interval)

    @staticmethod
    def on_service(parent: str, service: str, interval: Interval) -> "Dependency":
        return Dependency(parent, service, SERVICE, interval)


@dataclass(frozen=True, order=True)
class DownRef:
    """命中的停机事件：稳定 ID + 修订号。"""

    eid: str
    revision: int


@dataclass(frozen=True, order=True)
class EdgeRef:
    """当时可达且有效的一条依赖（共享依赖只记一次，不按路径重复展开）。"""

    parent: str
    child_kind: str
    child: str


@dataclass(frozen=True)
class DeviceEvidence:
    """设备状态及命中记录：不可用带停机事件，可用带监测记录，未知为空。"""

    device: str
    state: str
    down_events: tuple[DownRef, ...] = ()
    monitor_records: tuple[str, ...] = ()


@dataclass(frozen=True)
class ServiceVerdict:
    """服务裁定：门槛与计票（下级按身份去重，同一时刻相同下级只计一个）。"""

    service: str
    state: str
    threshold: int
    available: int
    unknown: int
    children: int

    @property
    def unavailable(self) -> int:
        return self.children - self.available - self.unknown


@dataclass(frozen=True)
class SegmentBasis:
    """一段的复算依据：当时可达依赖、各服务门槛与计票、各设备状态与命中记录。"""

    edges: tuple[EdgeRef, ...]
    devices: tuple[DeviceEvidence, ...]
    services: tuple[ServiceVerdict, ...]


@dataclass(frozen=True)
class AvailabilitySegment:
    interval: Interval
    state: str
    basis: SegmentBasis


@dataclass(frozen=True)
class AccessStats:
    """本次查询实际访问的服务/设备/记录（用于审计“只访问窗内可达部分”）。"""

    services: frozenset
    devices: frozenset
    down_events: frozenset
    monitor_records: frozenset


@dataclass(frozen=True)
class AvailabilityReport:
    """一次查询的结果：分段连续覆盖整窗，durations 给出各状态总时长。"""

    service: str
    window: Interval
    segments: tuple[AvailabilitySegment, ...]
    accessed: AccessStats

    def durations(self) -> dict[str, int]:
        """各状态总时长（三种状态齐全，未出现的为 0）。"""
        out = {state: 0 for state in STATES}
        for seg in self.segments:
            out[seg.state] += seg.interval.length
        return out

    def state_at(self, t: int) -> str:
        """窗内某时刻的状态（半开：lo 命中、hi 不命中）。"""
        for seg in self.segments:
            if seg.interval.contains_point(t):
                return seg.state
        raise ValueError(f"时刻 {t} 不在查询窗 [{self.window.lo},{self.window.hi}) 内")


def recompute_from_basis(basis: SegmentBasis, service: str) -> str:
    """只凭分段依据复算服务状态（审计用）：可达边 + 设备状态 + 门槛。

    不读取任何索引或检修账；结果应与对应分段的 state 一致。
    """
    adj: dict[str, list[EdgeRef]] = {}
    for edge in basis.edges:
        adj.setdefault(edge.parent, []).append(edge)
    device_state = {d.device: d.state for d in basis.devices}
    threshold = {v.service: v.threshold for v in basis.services}

    memo: dict[str, str] = {}
    stack = [service]
    while stack:
        node = stack[-1]
        if node in memo:
            stack.pop()
            continue
        edges = adj.get(node, ())
        svc_children = sorted({e.child for e in edges if e.child_kind == SERVICE})
        pending = [c for c in svc_children if c not in memo]
        if pending:
            stack.append(pending[0])
            continue
        available = unknown = 0
        for kind, name in {(e.child_kind, e.child) for e in edges}:
            st = device_state[name] if kind == DEVICE else memo[name]
            if st == STATE_AVAILABLE:
                available += 1
            elif st == STATE_UNKNOWN:
                unknown += 1
        thr = threshold.get(node, 0)
        if available >= thr:
            memo[node] = STATE_AVAILABLE
        elif available + unknown < thr:
            memo[node] = STATE_UNAVAILABLE
        else:
            memo[node] = STATE_UNKNOWN
    return memo[service]


def _svc_children(adj: dict[str, list[Dependency]], node: str) -> list[str]:
    return sorted({d.child for d in adj.get(node, ()) if d.child_kind == SERVICE})


def _find_cycle(root: str, adj: dict[str, list[Dependency]]) -> list[str] | None:
    """从 root 沿服务间边迭代 DFS，返回一条环（首尾相同）或 None。"""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {root: GRAY}
    path = [root]
    stack = [(root, iter(_svc_children(adj, root)))]
    while stack:
        node, it = stack[-1]
        advanced = False
        for nxt in it:
            c = color.get(nxt, WHITE)
            if c == GRAY:
                return path[path.index(nxt):] + [nxt]
            if c == WHITE:
                color[nxt] = GRAY
                path.append(nxt)
                stack.append((nxt, iter(_svc_children(adj, nxt))))
                advanced = True
                break
        if not advanced:
            color[node] = BLACK
            stack.pop()
            path.pop()
    return None


def _intersecting(rows: list[tuple], window: Interval) -> list[tuple]:
    """rows 按 lo 有序；只扫描 lo < window.hi 的候选，再精确判断半开相交。"""
    limit = bisect_left(rows, (window.hi,))
    return [row for row in rows[:limit] if row[1] > window.lo]


class AvailabilityIndex:
    """为同一批输入预建的可用性索引；可重复查询，查询不改动检修账。

    source 可以是 MaintenanceFactEngine（读其已发布事件）、EventStore
    或 Event 可迭代；其中只有停机（down）事件参与可用性裁定。
    """

    def __init__(
        self,
        source: "MaintenanceFactEngine | EventStore | Iterable[Event]",
        monitors: Iterable[MonitorRecord] = (),
        dependencies: Iterable[Dependency] = (),
        thresholds: dict[str, int] | None = None,
    ) -> None:
        if isinstance(source, MaintenanceFactEngine):
            events = source.store.all_events()
        elif isinstance(source, EventStore):
            events = source.all_events()
        else:
            events = list(source)

        down_by_device: dict[str, list[tuple[int, int, str, int]]] = {}
        for event in events:
            if not isinstance(event, Event):
                raise TypeError("事件来源必须只含 Event")
            if event.etype != DOWN:
                continue
            iv = event.interval()
            down_by_device.setdefault(event.asset, []).append(
                (iv.lo, iv.hi, event.eid, event.revision)
            )
        self._down = {dev: sorted(rows) for dev, rows in down_by_device.items()}

        mon_by_device: dict[str, list[tuple[int, int, str]]] = {}
        for record in monitors:
            if not isinstance(record, MonitorRecord):
                raise TypeError("监测覆盖必须是 MonitorRecord")
            mon_by_device.setdefault(record.device, []).append(
                (record.interval.lo, record.interval.hi, record.record_id)
            )
        self._mon = {dev: sorted(rows) for dev, rows in mon_by_device.items()}

        deps_by_parent: dict[str, list[Dependency]] = {}
        for dep in dependencies:
            if not isinstance(dep, Dependency):
                raise TypeError("依赖必须是 Dependency")
            deps_by_parent.setdefault(dep.parent, []).append(dep)
        self._deps = {
            parent: sorted(deps, key=lambda d: (d.interval.lo, d.interval.hi, d.child_kind, d.child))
            for parent, deps in deps_by_parent.items()
        }

        self._thresholds: dict[str, int] = {}
        for svc, thr in (thresholds or {}).items():
            if not isinstance(svc, str) or not svc:
                raise ValueError("门槛的服务名必须为非空字符串")
            if not isinstance(thr, int) or thr < 0:
                raise ValueError("服务门槛必须为非负整数")
            self._thresholds[svc] = thr

    # ---------------- 查询 ----------------
    def query(self, service: str, window: Interval) -> AvailabilityReport:
        """分段查询：覆盖整窗的状态、时长与复算依据。只读，不改动检修账。"""
        if not isinstance(window, Interval):
            raise TypeError("查询窗必须为 Interval（半开 [lo, hi)）")
        if not isinstance(service, str) or not service:
            raise ValueError("被查服务名必须为非空字符串")

        accessed_services: set[str] = set()
        accessed_devices: set[str] = set()
        accessed_down: set[str] = set()
        accessed_mon: set[str] = set()

        # 1) 候选依赖：与窗相交且沿父服务索引从被查服务可达（先忽略具体时刻）。
        #    只展开可达服务的依赖清单，不扫描全厂边。
        cand: list[tuple[Dependency, Interval]] = []
        seen = {service}
        queue = [service]
        while queue:
            svc = queue.pop()
            accessed_services.add(svc)
            for dep in self._deps.get(svc, ()):
                if dep.interval.lo >= window.hi:
                    break  # 按 lo 有序，之后的更晚
                clipped = dep.interval.clip(window)
                if clipped is None:
                    continue
                cand.append((dep, clipped))
                if dep.child_kind == SERVICE and dep.child not in seen:
                    seen.add(dep.child)
                    queue.append(dep.child)

        # 2) 依赖边界切段；逐段求“当时有效且可达”的子图并查环。
        edge_bounds = {window.lo, window.hi}
        for _, clipped in cand:
            edge_bounds.add(clipped.lo)
            edge_bounds.add(clipped.hi)
        epts = sorted(edge_bounds)

        edge_segments: list[tuple[int, int, set[str], set[EdgeRef], dict]] = []
        reachable_devices: set[str] = set()
        for lo, hi in zip(epts, epts[1:]):
            adj: dict[str, list[Dependency]] = {}
            for dep, clipped in cand:
                if clipped.lo <= lo < clipped.hi:
                    adj.setdefault(dep.parent, []).append(dep)
            reach_services: set[str] = set()
            reach_devices: set[str] = set()
            edge_refs: set[EdgeRef] = set()
            stack = [service]
            while stack:
                svc = stack.pop()
                if svc in reach_services:
                    continue
                reach_services.add(svc)
                for dep in adj.get(svc, ()):
                    edge_refs.add(EdgeRef(dep.parent, dep.child_kind, dep.child))
                    if dep.child_kind == SERVICE:
                        stack.append(dep.child)
                    else:
                        reach_devices.add(dep.child)
            cycle = _find_cycle(service, adj)
            if cycle is not None:
                raise DependencyCycleError(cycle, Interval(lo, hi))
            edge_segments.append((lo, hi, reach_devices, edge_refs, adj))
            reachable_devices |= reach_devices

        # 3) 实际可达设备的窗内状态时间线（按设备二分检索，不扫全厂历史）。
        timelines: dict[str, list[tuple[Interval, DeviceEvidence]]] = {}
        for dev in sorted(reachable_devices):
            accessed_devices.add(dev)
            timelines[dev] = self._device_timeline(dev, window, accessed_down, accessed_mon)

        # 4) 全局边界扫描：依赖边界 ∪ 可达设备的状态边界；不逐时间单位推进。
        bounds = set(edge_bounds)
        for segs in timelines.values():
            for iv, _ in segs:
                bounds.add(iv.lo)
                bounds.add(iv.hi)
        pts = sorted(bounds)
        edge_los = [seg[0] for seg in edge_segments]
        dev_ptr = {dev: 0 for dev in timelines}

        segments: list[AvailabilitySegment] = []
        for lo, hi in zip(pts, pts[1:]):
            _, _, reach_devs, edge_refs, adj = edge_segments[bisect_right(edge_los, lo) - 1]
            evidences: list[DeviceEvidence] = []
            evidence_map: dict[str, DeviceEvidence] = {}
            for dev in sorted(reach_devs):
                segs = timelines[dev]
                p = dev_ptr[dev]
                while segs[p][0].hi <= lo:
                    p += 1
                dev_ptr[dev] = p
                ev = segs[p][1]
                evidences.append(ev)
                evidence_map[dev] = ev
            verdicts = self._evaluate(service, adj, evidence_map)
            basis = SegmentBasis(
                edges=tuple(sorted(edge_refs)),
                devices=tuple(evidences),
                services=tuple(verdicts[s] for s in sorted(verdicts)),
            )
            state = verdicts[service].state
            # 状态与依据都相同的相邻段合并
            if segments and segments[-1].state == state and segments[-1].basis == basis:
                prev = segments[-1]
                segments[-1] = AvailabilitySegment(Interval(prev.interval.lo, hi), state, basis)
            else:
                segments.append(AvailabilitySegment(Interval(lo, hi), state, basis))

        # 不变式：分段连续且覆盖整窗
        assert segments[0].interval.lo == window.lo and segments[-1].interval.hi == window.hi
        for a, b in zip(segments, segments[1:]):
            assert a.interval.hi == b.interval.lo

        return AvailabilityReport(
            service=service,
            window=window,
            segments=tuple(segments),
            accessed=AccessStats(
                services=frozenset(accessed_services),
                devices=frozenset(accessed_devices),
                down_events=frozenset(accessed_down),
                monitor_records=frozenset(accessed_mon),
            ),
        )

    # ---------------- 内部 ----------------
    def _evaluate(
        self,
        root: str,
        adj: dict[str, list[Dependency]],
        evidence_map: dict[str, DeviceEvidence],
    ) -> dict[str, ServiceVerdict]:
        """逐服务裁定（迭代后序；共享下级只算一次）。调用前已确认无环。"""
        verdicts: dict[str, ServiceVerdict] = {}
        stack = [root]
        while stack:
            node = stack[-1]
            if node in verdicts:
                stack.pop()
                continue
            deps = adj.get(node, ())
            svc_children = sorted({d.child for d in deps if d.child_kind == SERVICE})
            pending = [c for c in svc_children if c not in verdicts]
            if pending:
                stack.append(pending[0])
                continue
            available = unknown = 0
            children = {(d.child_kind, d.child) for d in deps}  # 相同下级只计一个
            for kind, name in children:
                st = evidence_map[name].state if kind == DEVICE else verdicts[name].state
                if st == STATE_AVAILABLE:
                    available += 1
                elif st == STATE_UNKNOWN:
                    unknown += 1
            thr = self._thresholds.get(node, 0)
            if available >= thr:
                state = STATE_AVAILABLE
            elif available + unknown < thr:
                state = STATE_UNAVAILABLE
            else:
                state = STATE_UNKNOWN
            verdicts[node] = ServiceVerdict(node, state, thr, available, unknown, len(children))
            stack.pop()
        return verdicts

    def _device_timeline(
        self,
        device: str,
        window: Interval,
        accessed_down: set[str],
        accessed_mon: set[str],
    ) -> list[tuple[Interval, DeviceEvidence]]:
        """窗内设备状态时间线：按记录边界扫描，相邻同依据合并。"""
        downs = _intersecting(self._down.get(device, []), window)
        mons = _intersecting(self._mon.get(device, []), window)
        accessed_down.update(row[2] for row in downs)
        accessed_mon.update(row[2] for row in mons)

        bounds = {window.lo, window.hi}
        for lo, hi, *_ in downs + mons:
            bounds.add(max(lo, window.lo))
            bounds.add(min(hi, window.hi))
        pts = sorted(bounds)

        out: list[tuple[Interval, DeviceEvidence]] = []
        for lo, hi in zip(pts, pts[1:]):
            hit_down = tuple(sorted(
                DownRef(eid, rev)
                for dlo, dhi, eid, rev in downs
                if dlo <= lo < dhi
            ))
            if hit_down:
                ev = DeviceEvidence(device, STATE_UNAVAILABLE, hit_down, ())
            else:
                hit_mon = tuple(sorted(rid for mlo, mhi, rid in mons if mlo <= lo < mhi))
                if hit_mon:
                    ev = DeviceEvidence(device, STATE_AVAILABLE, (), hit_mon)
                else:
                    ev = DeviceEvidence(device, STATE_UNKNOWN, (), ())
            if out and out[-1][1] == ev:
                out[-1] = (Interval(out[-1][0].lo, hi), ev)
            else:
                out.append((Interval(lo, hi), ev))
        return out
