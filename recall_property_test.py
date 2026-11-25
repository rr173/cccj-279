"""批号召回账随机化性质测试。

不变式
------
1. 每次成功发布后，增量账与“从全部现行流水朴素全量重算”的参考账逐批一致
   （分仓余量、领用明细、批号集合）。
2. 数量守恒：每个 (料号, 批号) 的在库合计 + 已发合计 == 该批收货总量。
3. 访问边界：提案 accessed_ids 恰为受影响料号 × 端点仓群内的现行流水，
   远隔料号/仓群零访问（独立复算期望集，不引用模块内部实现）。
4. 任何拒绝（修订协议/残缺 spare/缺货/发布失败）后，查询结果与拒绝前完全相同。

运行: python3 recall_property_test.py
"""

import random

from maint_facts import (
    DOWN,
    SPARE,
    Event,
    FlowRecord,
    InsufficientStock,
    Interval,
    PublishFailed,
    RecallLedger,
    RevisionConflict,
    RevisionRejected,
)

WHS = [f"W{i}" for i in range(6)]
SKUS = ["P0", "P1", "P2"]
ASSETS = ["A1", "A2", "A3"]


def normalize(ledger: RecallLedger):
    balances, usages = [], []
    for sku, lot in ledger.lots():
        rep = ledger.query(sku, lot)
        for b in rep.balances:
            balances.append((sku, lot, b.wh, b.qty))
        for u in rep.usages:
            usages.append((sku, lot, u.event_id, u.asset, u.qty, u.time))
    return sorted(balances), sorted(usages), sorted(ledger.lots())


def snapshot(ledger: RecallLedger):
    return normalize(ledger), ledger.version


def build_reference(state: dict) -> RecallLedger:
    """朴素参考：从全部现行流水从零全量重算。"""
    events = [item for item in state.values() if isinstance(item, Event)]
    records = [item for item in state.values() if isinstance(item, FlowRecord)]
    ref = RecallLedger(events)
    ref.bootstrap(records)
    return ref


def endpoints_of(item):
    """返回 (料号集合, 端点仓列表)；非实物流水返回 (空, 空)。"""
    if isinstance(item, FlowRecord):
        if item.ftype == "receipt":
            return {item.sku}, [item.payload["wh"]]
        return {item.sku}, [item.payload["src"], item.payload["dst"]]
    if item.etype == SPARE and "wh" in item.payload and "sku" in item.payload:
        return {item.payload["sku"]}, [item.payload["wh"]]
    return set(), []


def components(flows, sku):
    adj = {}
    for f in flows:
        if f.ftype == "transfer" and f.sku == sku:
            adj.setdefault(f.src, set()).add(f.dst)
            adj.setdefault(f.dst, set()).add(f.src)
    out = {}
    for start in adj:
        if start in out:
            continue
        seen, stack = {start}, [start]
        while stack:
            node = stack.pop()
            for nxt in adj.get(node, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        g = frozenset(seen)
        for wh in seen:
            out[wh] = g
    return out


def expected_accessed(pre_state, post_state, changed_ids):
    """独立复算增量提案应重读的流水 ID 集。"""
    affected_skus, endpoints = set(), set()
    for cid in changed_ids:
        for state in (pre_state, post_state):
            item = state.get(cid)
            if item is None:
                continue
            skus, eps = endpoints_of(item)
            affected_skus |= skus
            endpoints |= {(s, w) for s in skus for w in eps}
    eps_by_sku = {}
    for sku, wh in endpoints:
        eps_by_sku.setdefault(sku, set()).add(wh)
    pre_views = _views(pre_state)
    post_views = _views(post_state)
    groups_by_sku = {}
    for sku in affected_skus:
        old_c = components(pre_views, sku)
        new_c = components(post_views, sku)
        groups = set()
        for wh in eps_by_sku.get(sku, ()):
            groups.add(old_c.get(wh, frozenset({wh})))
            groups.add(new_c.get(wh, frozenset({wh})))
        groups_by_sku[sku] = set().union(*groups) if groups else set()
    expected = set()
    for item in post_state.values():
        skus, eps = endpoints_of(item)
        if not skus:
            continue
        sku = next(iter(skus))
        if sku in affected_skus and any(w in groups_by_sku[sku] for w in eps):
            expected.add(item.flow_id if isinstance(item, FlowRecord) else item.eid)
    return expected


class _View:
    def __init__(self, ftype, sku, src, dst):
        self.ftype = ftype
        self.sku = sku
        self.src = src
        self.dst = dst


def _views(state):
    out = []
    for item in state.values():
        if isinstance(item, FlowRecord) and item.ftype == "transfer":
            p = item.payload
            out.append(_View("transfer", item.sku, p["src"], p["dst"]))
    return out


def rand_receipt(rng, fid, revision, time=None):
    return FlowRecord.receipt(
        fid, revision,
        rng.randrange(0, 30) if time is None else time,
        rng.choice(WHS), rng.choice(SKUS), f"lot-{fid}", rng.randrange(6, 18),
    )


def rand_transfer(rng, fid, revision):
    src, dst = rng.sample(WHS, 2)
    return FlowRecord.transfer(
        fid, revision, rng.randrange(0, 30), src, dst,
        rng.choice(SKUS), rng.randrange(1, 4),
    )


def rand_spare(rng, eid, revision, force_mode=None):
    mode = force_mode or rng.choices(
        ["full", "legacy", "broken"], weights=[8, 2, 1], k=1
    )[0]
    payload = {"asset": rng.choice(ASSETS), "time": rng.randrange(0, 30),
               "qty": rng.randrange(1, 4)}
    if mode == "full":
        payload["wh"] = rng.choice(WHS)
        payload["sku"] = rng.choice(SKUS)
    elif mode == "broken":
        payload["wh"] = rng.choice(WHS)  # 只给 wh，故意残缺
    # legacy: 两项都不给
    return Event(eid, revision, SPARE, payload)


def main() -> None:
    rng = random.Random(20261125)

    # 预热：每个 (仓, 料号) 先铺一笔充足收货，保证后续随机调拨/领用大多有货可扣；
    # 参考状态与被测账从同一批种子流水起步。
    seed_records = []
    for wi, wh in enumerate(WHS):
        for si, sku in enumerate(SKUS):
            fid = f"seed-{wi}-{si}"
            seed_records.append(
                FlowRecord.receipt(fid, 1, rng.randrange(0, 5), wh, sku,
                                   f"lot-{fid}", rng.randrange(12, 24))
            )
    ledger = RecallLedger()
    ledger.bootstrap(seed_records)
    # state: ID -> 当前已接受条目；revs: ID -> 当前修订号
    state: dict = {rec.flow_id: rec for rec in seed_records}
    revs: dict[str, int] = {rec.flow_id: 1 for rec in seed_records}
    id_counter = 0
    checkpoints = 0
    successes = 0
    next_checkpoint = 20
    rejects_observed = {"revision": 0, "broken": 0, "shortage": 0, "publish_fail": 0}

    def fresh_id(prefix: str) -> str:
        nonlocal id_counter
        cid = f"{prefix}-{id_counter:04d}"
        id_counter += 1
        return cid

    for step in range(300):
        batch_ids = []
        batch = []
        for _ in range(rng.randint(1, 3)):
            choose_event = rng.random() < 0.45
            prefix = "e" if choose_event else "f"
            existing = [i for i in state if i.startswith(f"{prefix}-")]
            if existing and rng.random() < 0.7:
                cid = rng.choice(existing)
                old = state[cid]
                mode = rng.random()
                if mode < 0.15:
                    batch.append(old)  # 同号同内容 -> NOOP
                elif mode < 0.25:
                    # 同号异内容 -> 冲突
                    if isinstance(old, FlowRecord):
                        bogus = FlowRecord.receipt(cid, old.revision, 1, WHS[0], SKUS[0], "x", 9)
                    else:
                        bogus = Event(cid, old.revision, SPARE,
                                      {"asset": "A1", "time": 1, "qty": 1})
                    batch.append(bogus)
                else:
                    rev = old.revision + 1
                    if isinstance(old, FlowRecord):
                        item = (rand_receipt(rng, cid, rev) if rng.random() < 0.5
                                else rand_transfer(rng, cid, rev))
                    else:
                        if rng.random() < 0.1:
                            # spare 整份替换成 down -> 旧出库撤销
                            item = Event(cid, rev, DOWN,
                                         {"asset": rng.choice(ASSETS),
                                          "interval": Interval(rng.randrange(0, 10),
                                                               rng.randrange(11, 20))})
                        else:
                            item = rand_spare(rng, cid, rev)
                    batch.append(item)
                batch_ids.append(cid)
            else:
                if choose_event:
                    cid = fresh_id("e")
                    batch.append(rand_spare(rng, cid, 1))
                else:
                    cid = fresh_id("f")
                    batch.append(rand_receipt(rng, cid, 1) if rng.random() < 0.65
                                 else rand_transfer(rng, cid, 1))
                batch_ids.append(cid)

        pre_snap = snapshot(ledger)
        pre_state = dict(state)
        try:
            proposal = ledger.submit(batch)
            if rng.random() < 0.05:
                try:
                    ledger.publish(fail=True)
                    raise AssertionError("应抛出 PublishFailed")
                except PublishFailed:
                    rejects_observed["publish_fail"] += 1
                    assert snapshot(ledger) == pre_snap, "发布失败后账目不允许变化"
                    continue
            ledger.publish()
        except (RevisionRejected, RevisionConflict) as exc:
            rejects_observed["revision"] += 1
            ledger.rollback() if ledger.proposal is not None else None
            assert snapshot(ledger) == pre_snap, f"修订拒绝后账目不允许变化: {exc}"
            continue
        except ValueError as exc:
            # 残缺 spare / 非法内容 / ID 冲突
            rejects_observed["broken"] += 1
            ledger.rollback() if ledger.proposal is not None else None
            assert snapshot(ledger) == pre_snap, f"数据残缺拒绝后账目不允许变化: {exc}"
            continue
        except InsufficientStock as exc:
            rejects_observed["shortage"] += 1
            assert ledger.proposal is None, "缺货在 submit 阶段整批拒绝，无残留提案"
            assert snapshot(ledger) == pre_snap, f"缺货拒绝后查询必须仍见旧版: {exc}"
            continue

        # 成功发布：接受条目进入参考状态
        if not proposal.noop_only:
            successes += 1
            for item in batch:
                cid = item.flow_id if isinstance(item, FlowRecord) else item.eid
                state[cid] = item
                revs[cid] = item.revision
            # 访问边界：独立复算期望集
            expected = expected_accessed(pre_state, state, batch_ids)
            assert proposal.accessed_ids == frozenset(expected), (
                f"step {step}: 访问集不一致\n"
                f"实际={sorted(proposal.accessed_ids)}\n期望={sorted(expected)}"
            )

        if successes >= next_checkpoint:
            next_checkpoint += 20
            checkpoints += 1
            ref = build_reference(state)
            got, want = normalize(ledger), normalize(ref)
            assert got == want, (
                f"step {step}: 增量账与全量重算不一致\n增量={got}\n全量={want}"
            )
            # 守恒：按 (sku, lot) 汇总，在库 + 已发 == 收货总量
            received = {}
            for item in state.values():
                if isinstance(item, FlowRecord) and item.ftype == "receipt":
                    key = (item.sku, item.payload["lot"])
                    received[key] = received.get(key, 0) + item.qty
            for key, total in received.items():
                rep = ledger.query(*key)
                assert rep.on_hand_total + rep.used_total == total, (
                    f"step {step}: {key} 不守恒 "
                    f"{rep.on_hand_total}+{rep.used_total}!={total}"
                )
            print(f"  checkpoint {checkpoints} (step {step}): 增量==全量、"
                  f"守恒、访问边界 全部一致，版本=v{ledger.version}")

    print(f"\n拒绝场景统计: {rejects_observed}")
    assert all(v > 0 for v in rejects_observed.values()), "各类拒绝路径都应被随机覆盖"
    # 跨命名空间 ID 冲突的确定性检查
    led = RecallLedger([Event("x", 1, SPARE,
                              {"asset": "A", "time": 1, "qty": 1, "wh": "W", "sku": "P"})])
    try:
        led.bootstrap([FlowRecord.receipt("x", 1, 1, "W", "P", "L", 1)])
        raise AssertionError("ID 冲突必须拒绝")
    except ValueError:
        pass
    print(f"300 轮随机更正 × {checkpoints} 个检查点全部通过；"
          f"拒绝后旧版可读、跨命名空间 ID 冲突被拦截。")


if __name__ == "__main__":
    main()
