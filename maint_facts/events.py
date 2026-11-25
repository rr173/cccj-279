"""事件模型与修订协议。

修订协议（对同一稳定事件 ID）
-----------------------------
- revision 必须为正整数且严格递增；低于/等于已存修订号的重送按规则处理。
- 同号且内容哈希一致  -> NOOP（重送无操作）。
- 同号但内容不同      -> REJECT（拒绝，调用方需改用新修订号）。
- 较高修订            -> ACCEPT，整份替换旧内容。

事件四类（type 字段）
---------------------
- loc   : 资产位置区间      payload {asset, loc, interval}
- wo    : 工单区间+整数优先级 payload {asset, wo, interval, priority}
- down  : 停机区间          payload {asset, interval}
- spare : 备件领用(时刻/数量, 可选工单) payload {asset, time, qty, wo?}
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .intervals import Interval

LOC = "loc"
WO = "wo"
DOWN = "down"
SPARE = "spare"
TYPES = (LOC, WO, DOWN, SPARE)

# 提交结果
ACCEPT = "ACCEPT"
NOOP = "NOOP"
REJECT = "REJECT"


class RevisionRejected(Exception):
    """修订号未严格递增（低于当前修订号）。"""


class RevisionConflict(Exception):
    """同修订号但内容不一致（异内容拒绝）。"""


def _canonical(payload: dict[str, Any]) -> str:
    def default(o: Any) -> Any:
        if isinstance(o, Interval):
            return {"__interval__": [o.lo, o.hi]}
        raise TypeError(f"不可序列化的对象: {type(o)!r}")

    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=default)


def content_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class Event:
    eid: str
    revision: int
    etype: str
    payload: dict[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.revision, int) or self.revision <= 0:
            raise ValueError("revision 必须为正整数")
        if self.etype not in TYPES:
            raise ValueError(f"未知事件类型: {self.etype}")
        self._validate()

    def _validate(self) -> None:
        p = self.payload
        if not isinstance(p.get("asset"), str):
            raise ValueError("payload.asset 必须为字符串")
        if self.etype == LOC:
            Interval(**p["interval"]) if isinstance(p["interval"], dict) else p["interval"]
            if not isinstance(p.get("loc"), str):
                raise ValueError("loc 事件需要 loc 字符串")
        elif self.etype == WO:
            Interval(**p["interval"]) if isinstance(p["interval"], dict) else p["interval"]
            if not isinstance(p.get("wo"), str):
                raise ValueError("wo 事件需要 wo 字符串")
            if not isinstance(p.get("priority"), int):
                raise ValueError("priority 必须为整数")
        elif self.etype == DOWN:
            Interval(**p["interval"]) if isinstance(p["interval"], dict) else p["interval"]
        elif self.etype == SPARE:
            if not isinstance(p.get("time"), int):
                raise ValueError("spare 事件需要整数 time")
            if not isinstance(p.get("qty"), int) or p["qty"] <= 0:
                raise ValueError("qty 必须为正整数")
            if "wo" in p and not isinstance(p["wo"], str):
                raise ValueError("spare 的 wo 必须为字符串或省略")

    @property
    def asset(self) -> str:
        return self.payload["asset"]

    def interval(self) -> Interval:
        iv = self.payload["interval"]
        return Interval(**iv) if isinstance(iv, dict) else iv


class EventStore:
    """按稳定 ID 保存各事件的最新接受版本。纯事件层，不持有派生事实。"""

    def __init__(self) -> None:
        self._events: dict[str, Event] = {}

    # ---- 校验（不产生变更，供事务两阶段提交使用）----
    def validate(self, event: Event) -> str:
        old = self._events.get(event.eid)
        if old is None:
            return ACCEPT
        if event.revision < old.revision:
            raise RevisionRejected(
                f"{event.eid}: 修订 {event.revision} 低于已存修订 {old.revision}"
            )
        if event.revision == old.revision:
            if content_hash(event.payload) == content_hash(old.payload):
                return NOOP
            raise RevisionConflict(
                f"{event.eid}: 同修订号 {event.revision} 内容不一致，拒绝"
            )
        return ACCEPT

    # ---- 正式落库（假定已通过 validate）----
    def apply(self, event: Event) -> None:
        self._events[event.eid] = event

    def submit(self, event: Event) -> str:
        """单事件直接提交：返回 ACCEPT / NOOP，冲突时抛异常。"""
        verdict = self.validate(event)
        if verdict == ACCEPT:
            self.apply(event)
        return verdict

    def get(self, eid: str) -> Event | None:
        return self._events.get(eid)

    def all_events(self) -> list[Event]:
        return list(self._events.values())

    def copy(self) -> "EventStore":
        clone = EventStore()
        clone._events = dict(self._events)
        return clone
