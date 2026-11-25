"""随机化性质测试：增量修订结果必须始终等价于朴素全量重算。

运行: python3 property_test.py
"""

import random

from maint_facts import (
    DOWN,
    LOC,
    SPARE,
    WO,
    Agg,
    Event,
    EventStore,
    Interval,
    MaintenanceFactEngine,
)
from maint_facts.engine import _entry_id

ASSETS = ["A1", "A2", "A3"]
LOCS = ["X", "Y", "Z"]
WOS = ["W1", "W2", "W3"]


def naive_full(events: list[Event]):
    """朴素参考实现：每次从全部事件全量重建。"""
    ref = MaintenanceFactEngine()
    ref.bootstrap(events)
    return ref


def normalize_facts(engine: MaintenanceFactEngine):
    return sorted(
        ((k.asset, k.wo, k.loc), (v.labor, v.down, v.spare_qty))
        for k, v in engine.facts.items()
        if v.nonzero()
    )


def _freeze(value):
    if isinstance(value, dict):
        return tuple(sorted((k, _freeze(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


def normalize_ledger(engine: MaintenanceFactEngine):
    # 去掉引擎内部 entry_id，按内容比较；detail 全部冻结为可哈希结构
    return sorted(
        (
            e.asset,
            e.bucket,
            e.reason,
            (e.interval.lo, e.interval.hi),
            (e.amount.labor, e.amount.down, e.amount.spare_qty),
            e.wo,
            _freeze(e.detail),
        )
        for e in engine.ledger
    )


def random_event(rng, eid: str, revision: int) -> Event:
    etype = rng.choice([LOC, WO, DOWN, SPARE])
    asset = rng.choice(ASSETS)
    if etype == SPARE:
        payload = {"asset": asset, "time": rng.randrange(0, 40), "qty": rng.randrange(1, 6)}
        if rng.random() < 0.4:
            payload["wo"] = rng.choice(WOS)
    else:
        lo = rng.randrange(0, 38)
        hi = rng.randrange(lo + 1, lo + 8)
        iv = Interval(lo, hi)
        if etype == LOC:
            payload = {"asset": asset, "loc": rng.choice(LOCS), "interval": iv}
        elif etype == WO:
            payload = {
                "asset": asset,
                "wo": rng.choice(WOS),
                "interval": iv,
                "priority": rng.randrange(1, 4),
            }
        else:
            payload = {"asset": asset, "interval": iv}
    return Event(eid, revision, etype, payload)


def main() -> None:
    rng = random.Random(20261125)

    # 稳定事件 ID -> 已接受内容与修订号
    state: dict[str, Event] = {}
    engine = MaintenanceFactEngine()
    checkpoints = 0

    def make_event(eid: str, revision: int) -> Event:
        return random_event(rng, eid, revision)

    for step in range(400):
        eid = f"E{rng.randrange(0, 40):02d}"
        old = state.get(eid)
        if old is None:
            event = make_event(eid, 1)
            try:
                engine.submit(event)
                engine.publish()
                state[eid] = event
            except (ValueError, RuntimeError):
                continue
        else:
            mode = rng.random()
            if mode < 0.25:
                # 同号重送：同内容 NOOP / 异内容拒绝
                if rng.random() < 0.5:
                    event = old  # 同内容
                else:
                    event = make_event(eid, old.revision)
                try:
                    proposal = engine.submit(event)
                    engine.publish()
                    assert proposal.verdict == "NOOP"
                except Exception as exc:  # noqa: BLE001 - 冲突/非法内容均预期
                    assert type(exc).__name__ in {"RevisionConflict", "ValueError", "RuntimeError"}
                    engine.rollback() if engine.proposal is not None else None
            else:
                # 高修订整份替换（少量非法修订号探测）
                revision = old.revision + 1 if rng.random() < 0.9 else old.revision
                event = make_event(eid, revision)
                try:
                    engine.submit(event)
                    engine.publish()
                    state[eid] = event
                except Exception as exc:  # noqa: BLE001
                    assert type(exc).__name__ in {"RevisionRejected", "RevisionConflict", "ValueError", "RuntimeError"}
                    if engine.proposal is not None:
                        engine.rollback()

        # 每 20 步与全量参考实现对比
        if step % 20 == 19:
            checkpoints += 1
            ref = naive_full(list(state.values()))
            assert normalize_facts(engine) == normalize_facts(ref), \
                f"step {step}: 事实不一致\n增量={normalize_facts(engine)}\n全量={normalize_facts(ref)}"
            assert normalize_ledger(engine) == normalize_ledger(ref), \
                f"step {step}: 备查账不一致"
            # 守恒：工时总量 = 各工单区间长度（位置歧义除外，歧义工时进待核）
            total_labor = sum(v.labor for v in engine.facts.values()) + sum(
                e.amount.labor for e in engine.ledger
            )
            wo_seconds = sum(
                e.interval().length for e in state.values() if e.etype == WO
            )
            assert total_labor == wo_seconds, f"step {step}: 工时不守恒 {total_labor}!={wo_seconds}"
            print(f"  checkpoint {checkpoints} (step {step}): 事实/备查账/工时守恒 全部一致，"
                  f"事件数={len(state)}，版本=v{engine.version}")

    # 确定性 ID 稳定性：同内容两条目 ID 相同
    iv = Interval(1, 2)
    a = _entry_id("A", "待核", "r", iv, Agg(down=1), {"k": 1}, None)
    b = _entry_id("A", "待核", "r", iv, Agg(down=1), {"k": 1}, None)
    assert a == b
    print(f"\n400 轮随机修订 × {checkpoints} 个检查点全部通过；确定性条目 ID 稳定。")


if __name__ == "__main__":
    main()
