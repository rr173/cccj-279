"""检修事实引擎。

产出两类结果
------------
1. 归属事实 facts: 按 (资产, 工单, 实际位置) 聚合的 Agg
   - labor  工单工时（按工单自身区间切位置，停机不反推工时）
   - down   停机时长（同资产重叠停机先做真实时间并集，只计一次）
   - spare  备件数量（整笔认领，并列不拆量）
2. 备查账 ledger: 未归属 / 待核条目，各自带原因、区间与明细

归属规则
--------
- 位置：同一时刻命中多个不同位置值 -> 待核；无位置区间覆盖 -> UNKNOWN（未知，不算待核）。
- 停机：并集后的每个停机时刻，归同时有效工单中优先级最高者；最高并列 -> 待核；无工单 -> 未归属。
- 备件：指定工单 ID 时校验同资产且当时有效，否则待核；
  未指定则按同一优先级规则唯一认领，并列整笔待核、不拆量。
- 迁移瞬间（半开边界 t）归新位置。

增量修订
--------
日常修订只取受影响资产上、与“新旧事件区间包络窗”相交的索引项重算，
替换该窗内的事实切片与备查账；窗之外及其他资产的物化结果保持不变。
发布失败回滚，已发布版本维持旧版。
"""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_left
from dataclasses import dataclass, field
from typing import Any

from .events import (
    ACCEPT,
    NOOP,
    DOWN,
    LOC,
    SPARE,
    WO,
    Event,
    EventStore,
    RevisionConflict,
    RevisionRejected,
)
from .intervals import Interval

# 位置哨兵：无位置区间覆盖
UNKNOWN = "未知位置"

# 备查账分桶
BUCKET_FACT = "归属事实"
BUCKET_UNASSIGNED = "未归属"
BUCKET_PENDING = "待核"

# 备查账原因码
DOWN_NO_WO = "停机无有效工单"
DOWN_TIE = "停机最高优先级并列"
SPARE_NO_WO = "备件无有效工单"
SPARE_TIE = "备件认领工单并列(不拆量)"
SPARE_EXPLICIT_INVALID = "备件指定工单校验失败(非同资产或当时无效)"
LOC_AMBIGUOUS = "位置同时命中多个不同值"


class PublishFailed(Exception):
    """模拟发布失败：提案回滚，已发布版本保持旧版。"""


@dataclass(frozen=True)
class Agg:
    """事实聚合量；三类量纲独立，不互相折算。"""

    labor: int = 0
    down: int = 0
    spare_qty: int = 0

    def __add__(self, other: "Agg") -> "Agg":
        return Agg(
            self.labor + other.labor,
            self.down + other.down,
            self.spare_qty + other.spare_qty,
        )

    def __sub__(self, other: "Agg") -> "Agg":
        return Agg(
            self.labor - other.labor,
            self.down - other.down,
            self.spare_qty - other.spare_qty,
        )

    def nonzero(self) -> bool:
        return bool(self.labor or self.down or self.spare_qty)

    def pretty(self) -> str:
        parts = []
        if self.labor:
            parts.append(f"工时={self.labor}")
        if self.down:
            parts.append(f"停机={self.down}")
        if self.spare_qty:
            parts.append(f"备件={self.spare_qty}")
        return ", ".join(parts) if parts else "空"


ZERO = Agg()


@dataclass(frozen=True)
class FactKey:
    asset: str
    wo: str
    loc: str


@dataclass(frozen=True)
class LedgerEntry:
    """未归属/待核条目。amount 仅承载对应的单一量纲。"""

    entry_id: str
    asset: str
    bucket: str
    reason: str
    interval: Interval
    amount: Agg
    detail: dict[str, Any] = field(default_factory=dict)
    wo: str | None = None

    def in_window(self, window: Interval) -> bool:
        return self.interval.intersects(window)


def _entry_id(asset: str, bucket: str, reason: str, iv: Interval,
              amount: Agg, detail: dict[str, Any], wo: str | None) -> str:
    """由条目内容确定性生成 ID：同一窗重建出的旧条目与已存储条目 ID 一致。"""
    body = json.dumps(
        [asset, bucket, reason, [iv.lo, iv.hi],
         [amount.labor, amount.down, amount.spare_qty], wo, detail],
        sort_keys=True, ensure_ascii=False, default=str,
    )
    return "L-" + hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


class _AssetIndex:
    """单资产索引：按区间起点(备件为领用时刻)有序，支持窗相交检索。"""

    def __init__(self) -> None:
        self._rows: list[tuple[int, str, Event]] = []  # (start, eid, event)
        self._starts: list[int] = []

    def upsert(self, event: Event) -> None:
        self.remove(event.eid)
        start = event.payload["time"] if event.etype == SPARE else event.interval().lo
        pos = bisect_left(self._starts, start)
        self._starts.insert(pos, start)
        self._rows.insert(pos, (start, event.eid, event))

    def remove(self, eid: str) -> None:
        for i, (_, existing_id, _) in enumerate(self._rows):
            if existing_id == eid:
                del self._rows[i]
                del self._starts[i]
                return

    def copy(self) -> "_AssetIndex":
        clone = _AssetIndex()
        clone._rows = list(self._rows)
        clone._starts = list(self._starts)
        return clone

    def intersecting(self, etype: str, window: Interval) -> list[Event]:
        """只扫描 start < window.hi 的候选，再做精确半开相交判断。"""
        limit = bisect_left(self._starts, window.hi)
        out: list[Event] = []
        for _, eid, event in self._rows[:limit]:
            if event.etype != etype:
                continue
            if etype == SPARE:
                if window.lo <= event.payload["time"] < window.hi:
                    out.append(event)
            elif event.interval().intersects(window):
                out.append(event)
        return out

    def spare_at(self, t: int) -> list[Event]:
        return [e for _, _, e in self._rows if e.etype == SPARE and e.payload["time"] == t]

    def __iter__(self):
        return iter(self._rows)


@dataclass
class Proposal:
    """一次修订的暂存净变化（旧事实撤回 + 新事实），提交前不影响已发布版本。"""

    event: Event
    verdict: str
    version_before: int
    window: Interval | None
    fact_changes: list[tuple[FactKey, Agg, Agg]]  # 展示用：仅含差异项
    all_fact_changes: list[tuple[FactKey, Agg, Agg]]  # 提交用：窗内全部键
    removed_ledger: list[LedgerEntry]
    added_ledger: list[LedgerEntry]
    window_by_asset: dict[str, Interval]  # 受影响资产 -> 修订窗
    affected_assets: list[str]
    accessed_eids: set[str]
    other_assets_unchanged: bool

    @staticmethod
    def noop(event: Event, version: int) -> "Proposal":
        return Proposal(event, NOOP, version, None, [], [], [], [], {}, [], set(), True)
    def describe(self) -> str:
        lines = [
            f"事件 {self.event.eid} rev{self.event.revision} ({self.event.etype}): "
            f"{self.verdict}，当前已发布版本 v{self.version_before}"
        ]
        if self.verdict == NOOP:
            lines.append("  同号同内容重送，无操作；事实与备查账均不变。")
            return "\n".join(lines)
        if self.window is not None:
            lines.append(f"  受影响窗: {self._iv(self.window)}（仅读取该窗相交索引项）")
        lines.append(f"  本次访问事件数: {len(self.accessed_eids)}，其他资产不变: {self.other_assets_unchanged}")
        if self.fact_changes:
            lines.append("  事实净变化（撤回旧值 -> 新值）:")
            for key, old, new in self.fact_changes:
                lines.append(
                    f"    - {key.asset}/{key.wo}/{key.loc}: "
                    f"[{old.pretty()}] -> [{new.pretty()}]"
                )
        else:
            lines.append("  事实净变化: 无")
        # 备查账按确定性 ID 抵消：只展示真正消失/新增的条目，
        # 整资产重算中 ID 不变的条目不刷屏。
        removed_ids = {e.entry_id for e in self.removed_ledger}
        added_ids = {e.entry_id for e in self.added_ledger}
        net_removed = [e for e in self.removed_ledger if e.entry_id not in added_ids]
        net_added = [e for e in self.added_ledger if e.entry_id not in removed_ids]
        if net_removed:
            lines.append("  撤回备查账:")
            for e in net_removed:
                lines.append(f"    - [{e.bucket}] {e.reason} {self._iv(e.interval)} {e.amount.pretty()}")
        if net_added:
            lines.append("  新记备查账:")
            for e in net_added:
                lines.append(f"    + [{e.bucket}] {e.reason} {self._iv(e.interval)} {e.amount.pretty()}")
        if not net_removed and not net_added:
            lines.append("  备查账: 无变化")
        return "\n".join(lines)

    @staticmethod
    def _iv(iv: Interval) -> str:
        return f"[{iv.lo},{iv.hi})"


class MaintenanceFactEngine:
    def __init__(self, store: EventStore | None = None) -> None:
        self.store: EventStore = store or EventStore()
        self.version: int = 0
        self.facts: dict[FactKey, Agg] = {}
        self.ledger_map: dict[str, LedgerEntry] = {}
        self.indexes: dict[str, _AssetIndex] = {}
        self.proposal: Proposal | None = None
        self._accessed: set[str] = set()

    @property
    def ledger(self) -> list[LedgerEntry]:
        """备查账只读视图：按资产、时间、原因排序。"""
        return sorted(
            self.ledger_map.values(),
            key=lambda e: (e.asset, e.interval.lo, e.interval.hi, e.reason),
        )

    # ================= 初始装载（全量，仅启动一次）=================
    def bootstrap(self, events: list[Event]) -> None:
        for event in events:
            self.store.submit(event)
        self._rebuild_all()

    def _rebuild_all(self) -> None:
        self.facts = {}
        self.ledger_map = {}
        self.indexes = {a: _AssetIndex() for a in {e.asset for e in self.store.all_events()}}
        for event in self.store.all_events():
            self.indexes[event.asset].upsert(event)
        for asset in sorted(self.indexes):
            idx = self.indexes[asset]
            ivs = [e.interval() for _, _, e in idx if e.etype != SPARE]
            window = Interval.hull(ivs)
            covered: set[str] = set()
            if window is not None:
                new_facts, new_ledger, touched = self._build_window_slice(asset, window, self.store, None)
                covered = touched.get(SPARE, set())
                self._merge_slice(asset, new_facts, new_ledger)
            for _, eid, event in idx:
                if event.etype == SPARE and event.eid not in covered:
                    f2, l2, _ = self._build_point_slice(asset, event.payload["time"], self.store, None)
                    self._merge_slice(asset, f2, l2)
        self.version = 1

    def _merge_slice(self, asset: str, facts: dict[FactKey, Agg], entries: list[LedgerEntry]) -> None:
        for key, agg in facts.items():
            self.facts[key] = self.facts.get(key, ZERO) + agg
        for entry in entries:
            self.ledger_map[entry.entry_id] = entry

    # ================= 修订提交流程 =================
    def submit(self, event: Event) -> Proposal:
        """校验并暂存修订；失败抛 RevisionRejected / RevisionConflict。"""
        if self.proposal is not None:
            raise RuntimeError("存在未决提案，请先 commit 或 rollback")
        verdict = self.store.validate(event)  # 冲突在此抛出
        if verdict == NOOP:
            self.proposal = Proposal.noop(event, self.version)
            return self.proposal

        old = self.store.get(event.eid)
        staged = self.store.copy()
        staged.apply(event)
        self._accessed = set()

        # 整份替换允许资产/类型变化：按 (资产, 足迹窗) 分组重算。
        # 旧版本所在资产做“撤回窗”，新版本所在资产做“应用窗”；
        # 资产不变时两窗合一，其他资产完全不触碰。
        jobs: dict[str, list[Interval]] = {}
        if old is not None:
            jobs.setdefault(old.asset, []).append(self._footprint(old))
        jobs.setdefault(event.asset, []).append(self._footprint(event))

        all_fact_changes: list[tuple[FactKey, Agg, Agg]] = []
        removed_ledger: list[LedgerEntry] = []
        added_ledger: list[LedgerEntry] = []
        window_by_asset: dict[str, Interval] = {}
        touched_assets = set(jobs)

        for job_asset, footprints in jobs.items():
            # 重算窗 = 该资产“修订后”全部事件足迹的包络。
            # 增量边界：只访问受影响资产的索引项，不触碰其他资产；
            # 日常修订的成本随该资产数据量线性、与全局历史总量无关。
            window = self._asset_hull(job_asset, staged, event)
            if window is None:
                window = Interval.hull(footprints)
            assert window is not None
            window_by_asset[job_asset] = window
            old_facts, old_led, _ = self._build_window_slice(
                job_asset, window, self.store, None
            )
            new_facts, new_led, _ = self._build_window_slice(
                job_asset, window, staged, event
            )
            old_led = [e for e in old_led if e.in_window(window)]
            new_led = [e for e in new_led if e.in_window(window)]
            removed_ledger.extend(old_led)
            added_ledger.extend(new_led)

            window_keys = sorted(set(old_facts) | set(new_facts),
                                 key=lambda k: (k.asset, k.wo, k.loc))
            for key in window_keys:
                all_fact_changes.append(
                    (key, old_facts.get(key, ZERO), new_facts.get(key, ZERO))
                )

        # 去重（同资产多窗时键可能重复出现，保留净变化一次即可）
        dedup: dict[FactKey, tuple[Agg, Agg]] = {}
        for key, old_agg, new_agg in all_fact_changes:
            if key in dedup:
                prev_old, prev_new = dedup[key]
                dedup[key] = (prev_old + old_agg, prev_new + new_agg)
            else:
                dedup[key] = (old_agg, new_agg)
        all_fact_changes = [
            (key, pair[0], pair[1])
            for key, pair in sorted(dedup.items(), key=lambda kv: (kv[0].asset, kv[0].wo, kv[0].loc))
        ]
        shown_changes = [c for c in all_fact_changes if c[1] != c[2]]
        window = Interval.hull(list(window_by_asset.values()))
        other_assets_unchanged = all(
            key.asset in touched_assets for key, _, _ in all_fact_changes
        ) and all(e.asset in touched_assets for e in removed_ledger + added_ledger)

        self.proposal = Proposal(
            event=event,
            verdict=ACCEPT,
            version_before=self.version,
            window=window,
            fact_changes=shown_changes,
            all_fact_changes=all_fact_changes,
            removed_ledger=removed_ledger,
            added_ledger=added_ledger,
            window_by_asset=dict(sorted(window_by_asset.items())),
            affected_assets=sorted(touched_assets),
            accessed_eids=set(self._accessed),
            other_assets_unchanged=other_assets_unchanged,
        )
        return self.proposal

    def commit(self) -> int:
        """正式发布暂存提案，版本号 +1。"""
        proposal = self._require_proposal()
        if proposal.verdict == NOOP:
            self.proposal = None
            return self.version

        # 复制后修改，保证异常时已发布状态不动。
        # 重算窗为受影响资产的全事件包络，窗内切片即该资产完整事实。
        # 对受影响资产整资产替换：移除其旧事实键后写入新切片；其他资产不变。
        affected = set(proposal.window_by_asset)
        new_facts = {
            key: agg for key, agg in self.facts.items() if key.asset not in affected
        }
        for key, _, new_agg in proposal.all_fact_changes:
            if new_agg.nonzero():
                new_facts[key] = new_facts.get(key, ZERO) + new_agg

        # ===== 备查账：受影响资产整资产键控替换 =====
        # 窗内切片即该资产完整备查账；移除受影响资产的全部旧条目，
        # 再以确定性内容键写回新切片，其他资产完全不触碰，幂等且无残段问题。
        new_ledger_map = {
            entry_id: entry
            for entry_id, entry in self.ledger_map.items()
            if entry.asset not in affected
        }
        for entry in proposal.added_ledger:
            new_ledger_map[entry.entry_id] = entry

        new_indexes = {a: idx.copy() for a, idx in self.indexes.items()}
        # 资产可能随整份替换而变化：旧资产索引移除同 ID 条目，新资产索引写入
        old_event = self.store.get(proposal.event.eid)
        if old_event is not None and old_event.asset != proposal.event.asset:
            old_idx = new_indexes.get(old_event.asset)
            if old_idx is not None:
                old_idx.remove(proposal.event.eid)
        idx = new_indexes.setdefault(proposal.event.asset, _AssetIndex())
        idx.upsert(proposal.event)

        # 最后一步才切换不可变的对外状态
        self.store.apply(proposal.event)
        self.facts = new_facts
        self.ledger_map = new_ledger_map
        self.indexes = new_indexes
        self.version += 1
        self.proposal = None
        return self.version

    def rollback(self) -> None:
        """放弃提案，已发布版本维持旧版。"""
        self.proposal = None

    def publish(self, fail: bool = False) -> int:
        """模拟发布：fail=True 时发布失败并重开（回滚），仍为旧版。"""
        self._require_proposal()
        if fail:
            event = self.proposal.event
            version = self.version
            self.rollback()
            raise PublishFailed(
                f"事件 {event.eid} 发布失败，提案已撤回，已发布版本仍为 v{version}"
            )
        return self.commit()

    def _require_proposal(self) -> Proposal:
        if self.proposal is None:
            raise RuntimeError("没有待发布的提案")
        return self.proposal

    # ================= 切片构造（窗内重算）=================
    def _build_window_slice(
        self,
        asset: str,
        window: Interval,
        store: EventStore,
        override: Event | None = None,
    ) -> tuple[dict[FactKey, Agg], list[LedgerEntry], dict[str, set[str]]]:
        idx = self._index_for(asset, store, override)
        loc_events, wo_events, down_events, spare_events = [], [], [], []
        touched: dict[str, set[str]] = {}
        for etype, bucket in ((LOC, loc_events), (WO, wo_events), (DOWN, down_events)):
            for event in idx.intersecting(etype, window):
                clipped = event.interval().clip(window)
                if clipped is not None:
                    bucket.append((event, clipped))
                    self._accessed.add(event.eid)
                    touched.setdefault(etype, set()).add(event.eid)
        for event in idx.intersecting(SPARE, window):
            spare_events.append(event)
            self._accessed.add(event.eid)
            touched.setdefault(SPARE, set()).add(event.eid)

        # 同资产重叠停机：真实时间并集，重复停机自动只计一次
        down_union = Interval.union([clipped for _, clipped in down_events])

        boundaries = {window.lo, window.hi}
        for _, clipped in loc_events + wo_events:
            boundaries.add(clipped.lo)
            boundaries.add(clipped.hi)
        for union_iv in down_union:
            boundaries.add(union_iv.lo)
            boundaries.add(union_iv.hi)
        for event, clipped in down_events:
            boundaries.add(clipped.lo)
            boundaries.add(clipped.hi)
        bounds = sorted(boundaries)

        # 边界处的增/删变化
        loc_change: dict[int, list[tuple[str, Event]]] = {}
        wo_change: dict[int, list[tuple[str, Event]]] = {}
        for event, clipped in loc_events:
            loc_change.setdefault(clipped.lo, []).append(("add", event))
            loc_change.setdefault(clipped.hi, []).append(("remove", event))
        for event, clipped in wo_events:
            wo_change.setdefault(clipped.lo, []).append(("add", event))
            wo_change.setdefault(clipped.hi, []).append(("remove", event))

        active_loc: dict[str, Event] = {}
        active_wo: dict[str, Event] = {}
        loc_states: list[tuple[int, tuple]] = []
        wo_states: list[tuple[int, list[tuple[str, int]]]] = []

        facts: dict[FactKey, Agg] = {}
        ledger: list[LedgerEntry] = []

        for t in bounds:
            for action, event in loc_change.get(t, []):
                if action == "add":
                    active_loc[event.eid] = event
                else:
                    active_loc.pop(event.eid, None)
            for action, event in wo_change.get(t, []):
                if action == "add":
                    active_wo[event.eid] = event
                else:
                    active_wo.pop(event.eid, None)
            loc_states.append((t, self._loc_state(active_loc)))
            wo_states.append((t, [(e.payload["wo"], e.payload["priority"]) for e in active_wo.values()]))

        # 逐基元段：工单工时（自身区间切位置）+ 停机并集认领
        for seg_lo, seg_hi in zip(bounds, bounds[1:]):
            seg = Interval(seg_lo, seg_hi)
            loc_state = self._state_at(loc_states, seg_lo)
            wo_snap = self._state_at(wo_states, seg_lo)

            # 工时：每个有效工单各自计，不看优先级、不由停机反推
            for wo_id, _ in wo_snap:
                amount = Agg(labor=seg.length)
                self._post(
                    facts, ledger, asset, loc_state, wo_snap, seg,
                    amount, kind="labor", explicit_wo=wo_id,
                )

            # 停机：严格按真实时间并集落账，每个停机时刻只计一次，
            # 重叠事件不会重复计量。明细列出覆盖该段的全部源事件，可追溯。
            for union_iv in down_union:
                covered = union_iv.clip(seg)
                if covered is None:
                    continue
                sources = sorted(
                    ev.eid
                    for ev, clipped in down_events
                    if clipped.intersects(covered)
                )
                self._post(
                    facts, ledger, asset, loc_state, wo_snap, covered,
                    Agg(down=covered.length), kind="down",
                    detail={"down_events": sources},
                )

        # 备件：点时刻状态取样，整笔认领不拆量
        for event in spare_events:
            t = event.payload["time"]
            loc_state = self._state_at(loc_states, t)
            wo_snap = self._state_at(wo_states, t)
            explicit = event.payload.get("wo")
            self._post(
                facts, ledger, asset, loc_state, wo_snap,
                Interval(t, t + 1), Agg(spare_qty=event.payload["qty"]),
                kind="spare", explicit_wo=explicit,
                detail={"spare_event": event.eid, "qty": event.payload["qty"]},
            )

        return facts, ledger, touched

    def _build_point_slice(
        self, asset: str, t: int, store: EventStore, override: Event | None
    ) -> tuple[dict[FactKey, Agg], list[LedgerEntry], dict[str, set[str]]]:
        """零长度窗（备件点事件）：只评估 t 时刻状态，不产生工时/停机。"""
        idx = self._index_for(asset, store, override)
        point_window = Interval(t, t + 1)
        loc_state = self._loc_state(
            {e.eid: e for e in idx.intersecting(LOC, point_window)}
        )
        wo_snap = [
            (e.payload["wo"], e.payload["priority"])
            for e in idx.intersecting(WO, point_window)
        ]
        facts: dict[FactKey, Agg] = {}
        ledger: list[LedgerEntry] = []
        touched: dict[str, set[str]] = {SPARE: set()}
        for event in idx.spare_at(t):
            self._accessed.add(event.eid)
            touched[SPARE].add(event.eid)
            self._post(
                facts, ledger, asset, loc_state, wo_snap,
                Interval(t, t + 1), Agg(spare_qty=event.payload["qty"]),
                kind="spare", explicit_wo=event.payload.get("wo"),
                detail={"spare_event": event.eid, "qty": event.payload["qty"]},
            )
        return facts, ledger, touched

    @staticmethod
    def _footprint(event: Event) -> Interval:
        """事件在时间轴上的足迹：区间事件取自身，备件点事件取单位窗。"""
        if event.etype == SPARE:
            t = event.payload["time"]
            return Interval(t, t + 1)
        return event.interval()

    def _asset_hull(
        self, asset: str, store: EventStore, override: Event | None
    ) -> Interval | None:
        """该资产修订后全部事件足迹的包络窗。"""
        idx = self._index_for(asset, store, override)
        footprints = [self._footprint(ev) for _, _, ev in idx]
        return Interval.hull(footprints)

    def _index_for(
        self, asset: str, store: EventStore, override: Event | None = None
    ) -> _AssetIndex:
        """取资产索引；暂存修订时按事件 ID 反映整份替换。

        - override 属于本资产：用新版本替换同 ID 旧条目（含跨类型替换）。
        - override 属于其他资产：仅从本资产索引移除同 ID 旧条目，
          避免已迁走的旧版本在新切片里被重复计算。
        """
        idx = self.indexes.get(asset)
        if idx is None:
            # 暂存库中的资产可能是新资产：按需建临时索引
            idx = _AssetIndex()
            for event in store.all_events():
                if event.asset == asset:
                    idx.upsert(event)
        if override is not None:
            clone = idx.copy()
            clone.remove(override.eid)
            if override.asset == asset:
                clone.upsert(override)
            return clone
        return idx

    @staticmethod
    def _loc_state(active: dict[str, Event]) -> tuple:
        values = {e.payload["loc"] for e in active.values()}
        if not values:
            return ("unknown",)
        if len(values) > 1:
            return ("amb", tuple(sorted(values)))
        return ("ok", next(iter(values)))

    @staticmethod
    def _state_at(states: list[tuple[int, Any]], t: int) -> Any:
        current = states[0][1]
        for start, state in states:
            if start <= t:
                current = state
            else:
                break
        return current

    def _post(
        self,
        facts: dict[FactKey, Agg],
        ledger: list[LedgerEntry],
        asset: str,
        loc_state: tuple,
        wo_snap: list[tuple[str, int]],
        iv: Interval,
        amount: Agg,
        kind: str,
        explicit_wo: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """按位置/工单规则把一笔量记入事实或备查账。"""
        detail = dict(detail or {})

        # 1) 位置歧义：一切量纲先待核
        if loc_state[0] == "amb":
            self._ledger(
                ledger, asset, BUCKET_PENDING, LOC_AMBIGUOUS, iv, amount,
                detail={**detail, "候选位置": list(loc_state[1])}, wo=explicit_wo,
            )
            return
        loc = UNKNOWN if loc_state[0] == "unknown" else loc_state[1]

        # 2) 工单选择
        if explicit_wo is not None:
            if kind == "spare" and not any(wo_id == explicit_wo for wo_id, _ in wo_snap):
                self._ledger(
                    ledger, asset, BUCKET_PENDING, SPARE_EXPLICIT_INVALID, iv, amount,
                    detail={**detail, "指定工单": explicit_wo}, wo=explicit_wo,
                )
                return
            chosen = explicit_wo
        else:
            if not wo_snap:
                reason = DOWN_NO_WO if kind == "down" else SPARE_NO_WO
                self._ledger(
                    ledger, asset, BUCKET_UNASSIGNED, reason, iv, amount, detail=detail,
                )
                return
            top = max(prio for _, prio in wo_snap)
            winners = sorted(wo_id for wo_id, prio in wo_snap if prio == top)
            if len(winners) > 1:
                reason = DOWN_TIE if kind == "down" else SPARE_TIE
                self._ledger(
                    ledger, asset, BUCKET_PENDING, reason, iv, amount,
                    detail={**detail, "并列工单": winners},
                )
                return
            chosen = winners[0]

        key = FactKey(asset, chosen, loc)
        facts[key] = facts.get(key, ZERO) + amount

    def _ledger(
        self,
        ledger: list[LedgerEntry],
        asset: str,
        bucket: str,
        reason: str,
        iv: Interval,
        amount: Agg,
        detail: dict[str, Any],
        wo: str | None = None,
    ) -> None:
        ledger.append(
            LedgerEntry(
                entry_id=_entry_id(asset, bucket, reason, iv, amount, detail, wo),
                asset=asset,
                bucket=bucket,
                reason=reason,
                interval=iv,
                amount=amount,
                detail=detail,
                wo=wo,
            )
        )

    # ================= 查询 =================
    def facts_for_asset(self, asset: str) -> dict[FactKey, Agg]:
        return {k: v for k, v in self.facts.items() if k.asset == asset}

    def ledger_for_asset(self, asset: str) -> list[LedgerEntry]:
        return [e for e in self.ledger if e.asset == asset]

    def unassigned(self, asset: str | None = None) -> list[LedgerEntry]:
        return [e for e in self.ledger if e.bucket == BUCKET_UNASSIGNED
                and (asset is None or e.asset == asset)]

    def pending(self, asset: str | None = None) -> list[LedgerEntry]:
        return [e for e in self.ledger if e.bucket == BUCKET_PENDING
                and (asset is None or e.asset == asset)]
