"""批号召回账：按批号盘点在库余量与已发给设备的备件领用明细。

定位
----
班组收到供应商召回通知后，凭“料号 + 批号”回答两件事：
1. 该批货现在还剩在哪些仓、各多少（非零分仓余量）；
2. 该批货已经发给了哪些设备（领用事件 ID、资产、出自该批的数量）。
本账只做实物视角的库存流：实物出库只看“出库仓 + 料号”，与领用事件挂在哪张
工单名下无关。

三类流水（共用稳定 ID 空间，业务时刻均为整数）
----------------------------------------------
- receipt 收货:    flow_id, time, wh(仓), sku(料号), lot(批号), qty
- transfer 仓间调拨: flow_id, time, src(起仓), dst(到仓), sku, qty
- spare 检修领用:  复用已发布的 spare 检修事件，再在 payload 上补 wh(出库仓)
                   与 sku(料号)；业务时刻取 spare.time，ID 取事件 ID。

spare 纳入规则（仓/料号两项）
----------------------------
- 两项都有：作为实物出库流水；
- 两项都没有：旧领用，不纳入本账（既不出库也不报错）；
- 只有一项：数据残缺，整批拒绝（ValueError）。
非 spare 事件不产生流水；spare 被修订成非 spare 事件时，旧出库随之撤销。

记账规则
--------
- 库存从空账起算，所有数量必须为正整数。
- 三类流水按 (整数业务时刻, 流水 ID 字典序) 统一排序后逐条记账。
- 仓内同料号先到先出（FIFO）；“先到”以本次到仓的先后为准
  （收货为入账时刻，调拨为到仓时刻）。
- 调拨保留实际扣出的原收货批号：同一笔调拨里的货按扣出先后到达到仓，
  即同一仓/料号队列内本笔调入的若干批按扣出顺序排到队尾。
- 任何一条流水库存不足（调拨起仓、领用出库仓的同料号余量不够），
  整批拒绝并指出缺货流水；已发布版本保持不变，查询仍见上一版。

修订协议（与检修事件同一套规则，每个 ID 每批至多一条）
----------------------------------------------------
- revision 正整数且严格递增；同号同内容重送 -> NOOP；
  同号异内容 -> RevisionConflict；低修订号 -> RevisionRejected；
  高修订 -> 整份替换（允许跨类型替换：收货改调拨、spare 改非 spare 等）。
- 修订按业务时刻生效：早期流水变动后，后续领用按更正后的货流重新归批。
- 增量边界：日常更正只重读被改流水的“新旧料号及仓群”明细。对每个涉及
  料号，以更正前后的调拨往来合计计算仓群（以仓为点、调拨为无向边的连通
  分量），只重放被改流水新旧端点所在的仓群；其他料号/仓群完全不读取、
  不变更。proposal.accessed_ids 可审计。
- 按批号查询直接读已物化的余量与领用明细，不重放物流历史。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .engine import MaintenanceFactEngine, PublishFailed
from .events import (
    ACCEPT,
    NOOP,
    SPARE,
    Event,
    EventStore,
    RevisionConflict,
    RevisionRejected,
    content_hash,
)

# 流水类型（spare 复用检修事件的类型常量 SPARE）
RECEIPT = "receipt"
TRANSFER = "transfer"
RECALL_FLOW_TYPES = (RECEIPT, TRANSFER, SPARE)


class InsufficientStock(Exception):
    """流水执行时库存不足：整批拒绝，已发布版本保持不变。

    flow_id : 缺货流水 ID；time 为其业务时刻；
    kind    : TRANSFER（起仓不足）或 SPARE（出库仓不足）；
    wh/sku  : 缺货的仓与料号；needed/available 为需要量与当时可用量。
    """

    def __init__(self, flow_id: str, time: int, kind: str,
                 wh: str, sku: str, needed: int, available: int) -> None:
        self.flow_id = flow_id
        self.time = time
        self.kind = kind
        self.wh = wh
        self.sku = sku
        self.needed = needed
        self.available = available
        action = "调拨起仓" if kind == TRANSFER else "领用出库仓"
        super().__init__(
            f"流水 {flow_id}（t={time}）库存不足：{action} {wh} 的料号 {sku} "
            f"需要 {needed}，实际可用 {available}；整批拒绝，已发布版本不变"
        )


# ============================ 流水模型与修订存储 ============================

@dataclass(frozen=True)
class FlowRecord:
    """收货 / 调拨流水（spare 流水复用检修 Event，不在这里建模）。"""

    flow_id: str
    revision: int
    ftype: str
    time: int
    payload: dict

    def __post_init__(self) -> None:
        if not isinstance(self.flow_id, str) or not self.flow_id:
            raise ValueError("流水 ID 必须为非空字符串")
        if not isinstance(self.revision, int) or self.revision <= 0:
            raise ValueError("revision 必须为正整数")
        if self.ftype not in (RECEIPT, TRANSFER):
            raise ValueError(f"FlowRecord 类型必须为 {RECEIPT!r} 或 {TRANSFER!r}")
        if not isinstance(self.time, int):
            raise ValueError("业务时刻 time 必须为整数")
        p = self.payload
        if not isinstance(p.get("sku"), str) or not p["sku"]:
            raise ValueError("流水需要非空字符串 sku（料号）")
        if not isinstance(p.get("qty"), int) or p["qty"] <= 0:
            raise ValueError("qty 必须为正整数")
        if self.ftype == RECEIPT:
            if not isinstance(p.get("wh"), str) or not p["wh"]:
                raise ValueError("收货流水需要非空字符串 wh（仓）")
            if not isinstance(p.get("lot"), str) or not p["lot"]:
                raise ValueError("收货流水需要非空字符串 lot（批号）")
        else:
            if not isinstance(p.get("src"), str) or not p["src"]:
                raise ValueError("调拨流水需要非空字符串 src（起仓）")
            if not isinstance(p.get("dst"), str) or not p["dst"]:
                raise ValueError("调拨流水需要非空字符串 dst（到仓）")
            if p["src"] == p["dst"]:
                raise ValueError("调拨的起仓与到仓不能相同")

    @staticmethod
    def receipt(flow_id: str, revision: int, time: int,
                wh: str, sku: str, lot: str, qty: int) -> "FlowRecord":
        return FlowRecord(flow_id, revision, RECEIPT, time,
                          {"wh": wh, "sku": sku, "lot": lot, "qty": qty})

    @staticmethod
    def transfer(flow_id: str, revision: int, time: int,
                 src: str, dst: str, sku: str, qty: int) -> "FlowRecord":
        return FlowRecord(flow_id, revision, TRANSFER, time,
                          {"src": src, "dst": dst, "sku": sku, "qty": qty})

    @property
    def sku(self) -> str:
        return self.payload["sku"]

    @property
    def qty(self) -> int:
        return self.payload["qty"]

    def _canonical(self) -> list:
        return [self.ftype, self.time, self.payload]


@dataclass(frozen=True)
class _Flow:
    """重放用的归一化流水视图（三类同构）。"""

    flow_id: str
    ftype: str
    time: int
    sku: str
    qty: int
    src: str | None = None    # TRANSFER 起仓 / SPARE 出库仓
    dst: str | None = None    # RECEIPT 收货仓 / TRANSFER 到仓
    lot: str | None = None    # 仅 RECEIPT 带原批号
    asset: str | None = None  # 仅 SPARE 带资产

    @staticmethod
    def from_record(rec: FlowRecord) -> "_Flow":
        p = rec.payload
        if rec.ftype == RECEIPT:
            return _Flow(rec.flow_id, RECEIPT, rec.time, p["sku"], p["qty"],
                         dst=p["wh"], lot=p["lot"])
        return _Flow(rec.flow_id, TRANSFER, rec.time, p["sku"], p["qty"],
                     src=p["src"], dst=p["dst"])

    @staticmethod
    def from_spare(event: Event) -> "_Flow":
        p = event.payload
        return _Flow(event.eid, SPARE, p["time"], p["sku"], p["qty"],
                     src=p["wh"], asset=p["asset"])


def _spare_flow(event: Event) -> _Flow | None:
    """从已发布检修事件提取 spare 实物流水。

    wh/sku 两项都有 -> 出库流水；两项都没有 -> 旧领用，不纳入；
    恰有一项 -> 数据残缺，抛 ValueError；非 spare 事件 -> None。
    """
    if event.etype != SPARE:
        return None
    has_wh = "wh" in event.payload
    has_sku = "sku" in event.payload
    if not has_wh and not has_sku:
        return None
    if not has_wh or not has_sku:
        missing = "wh（出库仓）" if not has_wh else "sku（料号）"
        raise ValueError(
            f"spare 事件 {event.eid} 的 wh/sku 必须同时提供或同时省略，"
            f"缺少 {missing}；整批拒绝"
        )
    wh = event.payload["wh"]
    sku = event.payload["sku"]
    if not isinstance(wh, str) or not wh or not isinstance(sku, str) or not sku:
        raise ValueError(f"spare 事件 {event.eid} 的 wh/sku 必须为非空字符串")
    return _Flow.from_spare(event)


class FlowStore:
    """按稳定 ID 保存收货/调拨流水的最新接受版本；规则与 EventStore 同构。"""

    def __init__(self) -> None:
        self._records: dict[str, FlowRecord] = {}

    def validate(self, rec: FlowRecord) -> str:
        old = self._records.get(rec.flow_id)
        if old is None:
            return ACCEPT
        if rec.revision < old.revision:
            raise RevisionRejected(
                f"{rec.flow_id}: 修订 {rec.revision} 低于已存修订 {old.revision}"
            )
        if rec.revision == old.revision:
            if content_hash(rec._canonical()) == content_hash(old._canonical()):
                return NOOP
            raise RevisionConflict(
                f"{rec.flow_id}: 同修订号 {rec.revision} 内容不一致，拒绝"
            )
        return ACCEPT

    def apply(self, rec: FlowRecord) -> None:
        self._records[rec.flow_id] = rec

    def submit(self, rec: FlowRecord) -> str:
        verdict = self.validate(rec)
        if verdict == ACCEPT:
            self.apply(rec)
        return verdict

    def get(self, flow_id: str) -> FlowRecord | None:
        return self._records.get(flow_id)

    def all_records(self) -> list[FlowRecord]:
        return list(self._records.values())

    def copy(self) -> "FlowStore":
        clone = FlowStore()
        clone._records = dict(self._records)
        return clone


# ============================ 重放（朴素物化内核）============================

@dataclass(frozen=True)
class _Layer:
    """仓内同料号 FIFO 队列中的一层：某收货批在该仓的一批余量。"""

    receipt_id: str
    lot: str
    qty: int


@dataclass
class _ReplayResult:
    # (sku, lot) -> {仓: 余量}（只含非零）
    balances: dict[tuple[str, str], dict[str, int]]
    # (sku, lot) -> 领用明细列表（重放顺序即业务顺序）
    usages: dict[tuple[str, str], list[dict]]
    # (sku, lot) -> 该批曾出现过的全部仓（含途经仓，供增量定位旧账）
    lot_whs: dict[tuple[str, str], set[str]]
    accessed_ids: set[str]


def _replay(flows: Iterable[_Flow]) -> _ReplayResult:
    """把一组已按 (time, ID) 排好序的流水从空账起逐条物化。

    库存不足时抛 InsufficientStock（调用方负责整批拒绝、不动已发布状态）。
    """
    queues: dict[tuple[str, str], list[_Layer]] = {}
    balances: dict[tuple[str, str], dict[str, int]] = {}
    usages: dict[tuple[str, str], list[dict]] = {}
    lot_whs: dict[tuple[str, str], set[str]] = {}
    accessed: set[str] = set()

    def add(wh: str, sku: str, receipt_id: str, lot: str, qty: int) -> None:
        key = (sku, wh)
        # 同收货批的相邻层合并；不同收货批（含调拨拆出的多批）保持分层，
        # 层序即到仓先后，天然实现“以本次到仓为准”的 FIFO。
        if queues.get(key) and queues[key][-1].receipt_id == receipt_id \
                and queues[key][-1].lot == lot:
            last = queues[key][-1]
            queues[key][-1] = _Layer(receipt_id, lot, last.qty + qty)
        else:
            queues.setdefault(key, []).append(_Layer(receipt_id, lot, qty))
        lot_key = (sku, lot)
        wh_map = balances.setdefault(lot_key, {})
        wh_map[wh] = wh_map.get(wh, 0) + qty
        lot_whs.setdefault(lot_key, set()).add(wh)

    def take(flow: _Flow, wh: str, qty: int) -> list[tuple[str, str, int]]:
        """从 (料号, 仓) 队列按 FIFO 扣 qty，返回 [(收货ID, 批号, 数量), ...]。"""
        key = (flow.sku, wh)
        queue = queues.get(key)
        available = sum(layer.qty for layer in queue or ())
        if available < qty:
            raise InsufficientStock(
                flow.flow_id, flow.time, flow.ftype, wh, flow.sku, qty, available
            )
        out: list[tuple[str, str, int]] = []
        remain = qty
        while remain:
            layer = queue[0]
            if layer.qty <= remain:
                out.append((layer.receipt_id, layer.lot, layer.qty))
                remain -= layer.qty
                lot_key = (flow.sku, layer.lot)
                wh_map = balances[lot_key]
                wh_map[wh] -= layer.qty
                if wh_map[wh] == 0:
                    del wh_map[wh]
                if not wh_map:
                    del balances[lot_key]
                queue.pop(0)
            else:
                out.append((layer.receipt_id, layer.lot, remain))
                lot_key = (flow.sku, layer.lot)
                balances[lot_key][wh] -= remain
                queue[0] = _Layer(layer.receipt_id, layer.lot, layer.qty - remain)
                remain = 0
        return out

    for flow in sorted(flows, key=lambda f: (f.time, f.flow_id)):
        accessed.add(flow.flow_id)
        if flow.ftype == RECEIPT:
            add(flow.dst, flow.sku, flow.flow_id, flow.lot, flow.qty)
        elif flow.ftype == TRANSFER:
            # 先按扣出先后取齐，再按同一次序入到仓：
            # 同一笔调拨里的货按扣出先后到仓。
            pulled = take(flow, flow.src, flow.qty)
            for receipt_id, lot, piece_qty in pulled:
                add(flow.dst, flow.sku, receipt_id, lot, piece_qty)
        else:  # SPARE：实物出库，只看出库仓与料号，与工单归属无关
            pulled = take(flow, flow.src, flow.qty)
            for receipt_id, lot, piece_qty in pulled:
                usages.setdefault((flow.sku, lot), []).append({
                    "event_id": flow.flow_id,
                    "asset": flow.asset,
                    "qty": piece_qty,
                    "time": flow.time,
                })

    return _ReplayResult(balances, usages, lot_whs, accessed)


def _all_flows(flow_store: FlowStore, event_store: EventStore) -> list[_Flow]:
    """汇总两个存储中的全部有效流水（统一 ID 空间），未排序。"""
    flows = [_Flow.from_record(rec) for rec in flow_store.all_records()]
    for event in event_store.all_events():
        spare = _spare_flow(event)
        if spare is not None:
            flows.append(spare)
    return flows


def _flow_index_by_sku(flow_store: FlowStore,
                       event_store: EventStore,
                       skus: Iterable[str] | None = None) -> dict[str, list[_Flow]]:
    """按料号建立流水索引：sku -> 该料号的全部归一化流水（未排序）。

    供增量更正只取受影响料号的流水，避免随全局历史全量扫描。
    只索引带实物出入库语义的流水：收货/调拨全部纳入；
    spare 仅纳入带 wh+sku 的实物流水（无 wh/sku 的旧领用不在账内）。
    skus 给定时只构建这些料号的索引（提交/提交后增量维护用）；
    为 None 时构建全量索引（bootstrap 用）。
    """
    wanted = None if skus is None else set(skus)
    index: dict[str, list[_Flow]] = {}
    for rec in flow_store.all_records():
        if wanted is None or rec.sku in wanted:
            index.setdefault(rec.sku, []).append(_Flow.from_record(rec))
    for event in event_store.all_events():
        spare = _spare_flow(event)
        if spare is not None and (wanted is None or spare.sku in wanted):
            index.setdefault(spare.sku, []).append(spare)
    return index


def _transfer_components(flows: Iterable[_Flow], sku: str) -> dict[str, frozenset[str]]:
    """以仓为点、该料号的调拨为无向边求连通分量：仓 -> 所在仓群。

    只统计指定料号的调拨往来（“对每个涉及料号计算连通范围”）；
    未参与该料号调拨的仓不在返回值中，调用方按孤立仓 {wh} 处理。
    """
    adj: dict[str, set[str]] = {}
    for flow in flows:
        if flow.ftype != TRANSFER or flow.sku != sku:
            continue
        adj.setdefault(flow.src, set()).add(flow.dst)
        adj.setdefault(flow.dst, set()).add(flow.src)
    comp_of: dict[str, frozenset[str]] = {}
    for start in adj:
        if start in comp_of:
            continue
        seen = {start}
        stack = [start]
        while stack:
            node = stack.pop()
            for nxt in adj.get(node, ()):  # type: ignore[union-attr]
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        group = frozenset(seen)
        for wh in seen:
            comp_of[wh] = group
    return comp_of


# ============================ 查询结果 ============================

@dataclass(frozen=True)
class LotBalance:
    """某批号在某仓的非零余量。"""

    wh: str
    qty: int


@dataclass(frozen=True)
class UsageLine:
    """该批货的一条领用明细：事件 ID、资产、出自该批的数量。"""

    event_id: str
    asset: str
    qty: int
    time: int


@dataclass(frozen=True)
class RecallReport:
    """按 (料号, 批号) 的召回盘点结果；查询只读，不重放物流历史。"""

    sku: str
    lot: str
    version: int
    balances: tuple[LotBalance, ...]
    usages: tuple[UsageLine, ...]

    @property
    def on_hand_total(self) -> int:
        """在库余量合计（途经仓不留账，天然不重复计入）。"""
        return sum(b.qty for b in self.balances)

    @property
    def used_total(self) -> int:
        """已发给设备的该批数量合计。"""
        return sum(u.qty for u in self.usages)


# ============================ 提案与账本 ============================

@dataclass
class RecallProposal:
    """一批更正的暂存结果；提交前不影响已发布版本。"""

    items: tuple           # 本批原始条目（FlowRecord / Event）
    verdicts: dict         # ID -> ACCEPT / NOOP
    version_before: int
    affected_skus: list[str]
    affected_groups: dict  # sku -> 受影响仓群列表（每个 frozenset 一个仓群）
    accessed_ids: frozenset
    noop_only: bool

    def describe(self) -> str:
        lines = [
            f"召回账更正批：{len(self.items)} 条，当前已发布版本 v{self.version_before}"
        ]
        if self.noop_only:
            lines.append("  全部为同号同内容重送（NOOP），余量与领用明细均不变。")
            return "\n".join(lines)
        lines.append(f"  涉及料号: {', '.join(self.affected_skus)}")
        for sku in self.affected_skus:
            groups = self.affected_groups.get(sku, [])
            lines.append(f"    料号 {sku} 受影响仓群 {len(groups)} 个: "
                         + "；".join("{" + ",".join(sorted(g)) + "}" for g in groups))
        lines.append(f"  本次重读流水 {len(self.accessed_ids)} 条: "
                     f"{sorted(self.accessed_ids)}")
        return "\n".join(lines)


class RecallLedger:
    """批号召回账：收货/调拨/领用三类流水的物化库存与只读批号查询。

    source 为已发布检修事件的来源（MaintenanceFactEngine / EventStore /
    Event 可迭代）；其中只有带 wh+sku 的 spare 事件产生实物出库流水。
    收货/调拨流水通过 bootstrap / submit 进入本账自己的 FlowStore。
    """

    def __init__(
        self,
        source: "MaintenanceFactEngine | EventStore | Iterable[Event] | None" = None,
    ) -> None:
        if source is None:
            events: list[Event] = []
        elif isinstance(source, MaintenanceFactEngine):
            events = source.store.all_events()
        elif isinstance(source, EventStore):
            events = source.all_events()
        else:
            events = list(source)
        for event in events:
            if not isinstance(event, Event):
                raise TypeError("事件来源必须只含 Event")

        self.event_store: EventStore = EventStore()
        for event in events:
            # 残缺 spare（wh/sku 只缺一项）在入口即拒绝
            _spare_flow(event)
            self.event_store.submit(event)

        self.flow_store: FlowStore = FlowStore()
        self.version: int = 0
        # (sku, lot) -> {仓: 非零余量}；按批嵌套，批号查询只访问该批
        self._balances: dict[tuple[str, str], dict[str, int]] = {}
        self._usages: dict[tuple[str, str], list[dict]] = {}
        self._lot_whs: dict[tuple[str, str], set[str]] = {}
        # 增量索引：按料号组织的现行流水与已物化批号，提交时只取受影响料号
        self._flow_index: dict[str, list[_Flow]] = {}
        self._lot_index: dict[str, set[tuple[str, str]]] = {}
        self.proposal: RecallProposal | None = None
        self._staged_flows: FlowStore | None = None
        self._staged_events: EventStore | None = None
        self._staged_result: _ReplayResult | None = None
        self._staged_old_keys: set[tuple[str, str]] | None = None
        self._staged_flow_index: dict[str, list[_Flow]] | None = None

    # ================= 初始装载（全量，仅启动一次）=================
    def bootstrap(self, records: Iterable[FlowRecord]) -> None:
        records = list(records)
        # 三类流水共用稳定 ID 空间：收货/调拨 ID 不得与任何已发布事件 ID 冲突
        # （其中只有带 wh+sku 的 spare 事件才真正产生实物流水）。
        event_ids = {e.eid for e in self.event_store.all_events()}
        for rec in records:
            if rec.flow_id in event_ids:
                raise ValueError(
                    f"流水 ID {rec.flow_id} 与已发布事件 ID 冲突："
                    f"三类流水共用稳定 ID 空间"
                )
            self.flow_store.submit(rec)
        result = _replay(_all_flows(self.flow_store, self.event_store))
        self._balances = result.balances
        self._usages = result.usages
        self._lot_whs = result.lot_whs
        self._rebuild_indexes()
        self.version = 1

    def _rebuild_indexes(self) -> None:
        """全量重建按料号索引（仅 bootstrap 调用）。"""
        self._flow_index = _flow_index_by_sku(self.flow_store, self.event_store)
        lot_index: dict[str, set[tuple[str, str]]] = {}
        for key in self._lot_whs:
            lot_index.setdefault(key[0], set()).add(key)
        # 已发尽、只剩领用明细的批也纳入索引
        for key in self._usages:
            lot_index.setdefault(key[0], set()).add(key)
        self._lot_index = lot_index

    def _derive_staged_flow_index(self, items: list,
                                  skus: set[str]) -> dict[str, list[_Flow]]:
        """从现行料号索引增量派生“暂存后”的受影响料号索引。

        只触碰受影响料号的索引桶与本批条目，不遍历全局存储：
        对每个受影响料号，取现行桶后剔除被改 ID 的旧条目，再补入暂存新条目。
        """
        changed_ids = {
            item.flow_id if isinstance(item, FlowRecord) else item.eid
            for item in items
        }
        index: dict[str, list[_Flow]] = {
            sku: [f for f in self._flow_index.get(sku, ())
                  if f.flow_id not in changed_ids]
            for sku in skus
        }
        for item in items:
            if isinstance(item, FlowRecord):
                flow = _Flow.from_record(item)
            else:
                flow = _spare_flow(item)  # 非 spare / 无 wh+sku 返回 None
            if flow is not None and flow.sku in skus:
                index.setdefault(flow.sku, []).append(flow)
        return index

    # ================= 更正批（两阶段）=================
    def submit(self, items: Iterable["FlowRecord | Event"]) -> RecallProposal:
        """校验并暂存一批新增/更正；每 ID 至多一条。失败抛异常且不改已发布版。"""
        if self.proposal is not None:
            raise RuntimeError("存在未决提案，请先 commit 或 rollback")
        items = list(items)
        if not items:
            raise ValueError("更正批不能为空")

        # 批内 ID 唯一（每个 ID 至多一条）
        seen: set[str] = set()
        for item in items:
            item_id = item.flow_id if isinstance(item, FlowRecord) else item.eid
            if item_id in seen:
                raise ValueError(f"同一更正批中 ID {item_id} 出现多条，每个 ID 至多一条")
            seen.add(item_id)

        staged_flows = self.flow_store.copy()
        staged_events = self.event_store.copy()
        verdicts: dict[str, str] = {}

        for item in items:
            if isinstance(item, FlowRecord):
                # 跨命名空间冲突：流水 ID 不得占用 spare 事件 ID
                if staged_events.get(item.flow_id) is not None:
                    raise ValueError(
                        f"流水 ID {item.flow_id} 与已发布 spare 事件 ID 冲突："
                        f"三类流水共用稳定 ID 空间"
                    )
                verdicts[item.flow_id] = staged_flows.validate(item)
                staged_flows.apply(item)
            elif isinstance(item, Event):
                if staged_flows.get(item.eid) is not None:
                    raise ValueError(
                        f"spare 事件 ID {item.eid} 与已存收货/调拨流水 ID 冲突："
                        f"三类流水共用稳定 ID 空间"
                    )
                # 残缺 spare 在此抛出（整批拒绝）；非 spare 事件只做修订协议校验
                _spare_flow(item)
                verdicts[item.eid] = staged_events.validate(item)
                staged_events.apply(item)
            else:
                raise TypeError("更正批只接受 FlowRecord 或 Event")

        if all(v == NOOP for v in verdicts.values()):
            proposal = RecallProposal(
                items=tuple(items), verdicts=verdicts, version_before=self.version,
                affected_skus=[], affected_groups={}, accessed_ids=frozenset(),
                noop_only=True,
            )
            self.proposal = proposal
            self._staged_flows = staged_flows
            self._staged_events = staged_events
            return proposal

        # ---- 增量边界：只取被改流水的新旧料号及仓群 ----
        # 按料号分别收集被改流水的新旧端点（仓）。
        endpoints_by_sku: dict[str, set[str]] = {}
        for item in items:
            if isinstance(item, FlowRecord):
                old = self.flow_store.get(item.flow_id)
                if old is not None:
                    endpoints_by_sku.setdefault(old.sku, set()).update(self._endpoints(old))
                endpoints_by_sku.setdefault(item.sku, set()).update(self._endpoints(item))
            else:
                old = self.event_store.get(item.eid)
                if old is not None:
                    old_flow = _spare_flow(old)
                    if old_flow is not None:
                        endpoints_by_sku.setdefault(old_flow.sku, set()).add(old_flow.src)
                new_flow = _spare_flow(item)
                if new_flow is not None:
                    endpoints_by_sku.setdefault(new_flow.sku, set()).add(new_flow.src)

        affected_skus = set(endpoints_by_sku)

        # 只取受影响料号的现行流水（按料号索引），不扫描其他料号/仓群。
        old_scoped = [f for sku in affected_skus
                      for f in self._flow_index.get(sku, ())]
        # 暂存料号索引由现行索引 + 本批变更增量派生，不遍历全局存储
        staged_flow_index = self._derive_staged_flow_index(items, affected_skus)
        new_scoped = [f for sku in affected_skus
                      for f in staged_flow_index.get(sku, ())]

        # 对每个涉及料号：以更正前后该料号的调拨往来合计求连通范围，
        # 只保留被改流水新旧端点所在的仓群。
        affected_groups: dict[str, list[frozenset[str]]] = {}
        group_sets: dict[str, set[frozenset[str]]] = {}
        for sku in affected_skus:
            old_comp = _transfer_components(old_scoped, sku)
            new_comp = _transfer_components(new_scoped, sku)
            chosen: set[frozenset[str]] = set()
            for wh in endpoints_by_sku[sku]:
                chosen.add(old_comp.get(wh, frozenset({wh})))
                chosen.add(new_comp.get(wh, frozenset({wh})))
            affected_groups[sku] = sorted(chosen, key=lambda g: sorted(g))
            group_sets[sku] = chosen

        # 旧账中落在受影响仓群内的 (sku, lot) 键。
        # _lot_whs 记录每个批曾出现过的全部仓（含已发尽、只剩领用明细的批），
        # 故按历史仓与受影响仓群相交即可定位全部需要整键替换的批。
        # 经 _lot_index 只遍历受影响料号的批，不扫全局批号。
        old_lot_keys: set[tuple[str, str]] = set()
        for sku, groups in group_sets.items():
            touched_whs = set().union(*groups) if groups else set()
            for key in self._lot_index.get(sku, set()):
                if self._lot_whs.get(key, set()) & touched_whs:
                    old_lot_keys.add(key)

        # ---- 按 (料号 × 仓群) 限定重放；缺货在此抛 InsufficientStock ----
        scoped: list[_Flow] = []
        for flow in new_scoped:
            groups = group_sets.get(flow.sku, set())
            if not groups:
                continue
            whs = set().union(*groups)
            if self._flow_touches(flow, whs):
                scoped.append(flow)
        scoped_result = _replay(scoped)

        # 审计集：重放实际读到的流水
        accessed = set(scoped_result.accessed_ids)

        proposal = RecallProposal(
            items=tuple(items),
            verdicts=verdicts,
            version_before=self.version,
            affected_skus=sorted(affected_skus),
            affected_groups={sku: affected_groups[sku] for sku in sorted(affected_groups)},
            accessed_ids=frozenset(accessed),
            noop_only=False,
        )
        self.proposal = proposal
        self._staged_flows = staged_flows
        self._staged_events = staged_events
        # 暂存重放结果与暂存料号索引，commit 时合并/复用
        self._staged_result = scoped_result
        self._staged_old_keys = old_lot_keys
        self._staged_flow_index = staged_flow_index
        return proposal

    def commit(self) -> int:
        """正式发布暂存提案，版本号 +1；NOOP 不升版。"""
        proposal = self._require_proposal()
        if proposal.noop_only:
            self._clear_proposal()
            return self.version

        result: _ReplayResult = self._staged_result
        old_keys = self._staged_old_keys

        # 受影响 (sku, lot) 键整键替换：先撤旧账，再写新账，其他键完全不触碰。
        # 三层结构均以 (sku, lot) 二元组为键，与 old_keys 同构。
        new_balances = {
            key: dict(wh_map) for key, wh_map in self._balances.items()
            if key not in old_keys
        }
        for key, wh_map in result.balances.items():
            if wh_map:  # 重放结果只含非零余量，空批不落账
                new_balances[key] = dict(wh_map)
        new_usages = {k: v for k, v in self._usages.items() if k not in old_keys}
        for key, lines in result.usages.items():
            new_usages[key] = lines
        new_lot_whs = {k: set(v) for k, v in self._lot_whs.items() if k not in old_keys}
        for key, whs in result.lot_whs.items():
            new_lot_whs[key] = set(whs)

        # 最后一步才切换对外状态
        self.flow_store = self._staged_flows
        self.event_store = self._staged_events
        self._balances = new_balances
        self._usages = new_usages
        self._lot_whs = new_lot_whs
        # 增量维护索引：只替换受影响料号的流水桶，其他料号索引原样保留；
        # 批号索引按新物化结果整桶重建（仅限受影响料号）。
        # 受影响料号直接取提案口径，不依赖是否还有批号落账（覆盖“整批撤销”场景）。
        affected_skus = set(proposal.affected_skus)
        for sku in affected_skus:
            self._flow_index[sku] = self._staged_flow_index.get(sku, [])
            lot_set = {k for k in self._lot_whs if k[0] == sku}
            lot_set |= {k for k in self._usages if k[0] == sku}
            if lot_set:
                self._lot_index[sku] = lot_set
            else:
                self._lot_index.pop(sku, None)
        self.version += 1
        self._clear_proposal()
        return self.version

    def rollback(self) -> None:
        """放弃提案，已发布版本维持旧版。"""
        self._clear_proposal()

    def publish(self, fail: bool = False) -> int:
        """模拟发布：fail=True 时发布失败并回滚，已发布版本仍为旧版。"""
        proposal = self._require_proposal()
        if fail:
            version = self.version
            self.rollback()
            raise PublishFailed(
                f"召回账更正批发布失败，提案已撤回，已发布版本仍为 v{version}"
            )
        return self.commit()

    def _clear_proposal(self) -> None:
        self.proposal = None
        self._staged_flows = None
        self._staged_events = None
        self._staged_result = None
        self._staged_old_keys = None
        self._staged_flow_index = None

    def _require_proposal(self) -> RecallProposal:
        if self.proposal is None:
            raise RuntimeError("没有待发布的提案")
        return self.proposal

    @staticmethod
    def _endpoints(rec: FlowRecord) -> list[str]:
        if rec.ftype == RECEIPT:
            return [rec.payload["wh"]]
        return [rec.payload["src"], rec.payload["dst"]]

    @staticmethod
    def _flow_touches(flow: _Flow, whs: set[str]) -> bool:
        if flow.ftype == RECEIPT:
            return flow.dst in whs
        if flow.ftype == TRANSFER:
            return flow.src in whs or flow.dst in whs
        return flow.src in whs

    # ================= 只读查询（不重放物流历史）=================
    def query(self, sku: str, lot: str) -> RecallReport:
        """给定料号与批号：最新成功发布的非零分仓余量 + 该批领用明细。

        直接读已物化的该批嵌套账，只访问该批的分仓与领用，不扫描其他批、
        不重放物流历史，成本与全局流水总量无关。
        """
        if not isinstance(sku, str) or not sku:
            raise ValueError("料号 sku 必须为非空字符串")
        if not isinstance(lot, str) or not lot:
            raise ValueError("批号 lot 必须为非空字符串")
        key = (sku, lot)
        balances = tuple(sorted(
            (LotBalance(wh, qty) for wh, qty in self._balances.get(key, {}).items()
             if qty > 0),
            key=lambda b: b.wh,
        ))
        usages = tuple(sorted(
            (UsageLine(line["event_id"], line["asset"], line["qty"], line["time"])
             for line in self._usages.get(key, ())),
            key=lambda u: (u.time, u.event_id, u.asset),
        ))
        return RecallReport(sku, lot, self.version, balances, usages)

    def lots(self, sku: str | None = None) -> list[tuple[str, str]]:
        """已物化的全部 (料号, 批号)，供盘点列举（只读）。"""
        keys = set(self._lot_whs) | set(self._usages)
        return sorted(k for k in keys if sku is None or k[0] == sku)
