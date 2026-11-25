"""批号召回账手算例与端到端验证（全部数据在内存构造，不读取外部文件）。

业务时刻为整数；三类流水（收货 receipt / 调拨 transfer / 检修领用 spare）
共用稳定 ID 空间，按 (时刻, ID 字典序) 记账，仓内同料号先到先出。
运行: python3 demo_recall.py
"""

from maint_facts import (
    SPARE,
    Event,
    FlowRecord,
    InsufficientStock,
    LotBalance,
    PublishFailed,
    RecallLedger,
    RevisionConflict,
    RevisionRejected,
    UsageLine,
)


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(f"手算核对失败: {msg}")
    print(f"  [OK] {msg}")


def show(ledger: RecallLedger, sku: str, lot: str) -> None:
    rep = ledger.query(sku, lot)
    print(f"  批号 {sku}/{lot}（版本 v{rep.version}）:")
    for b in rep.balances:
        print(f"    在库 {b.wh}: {b.qty}")
    for u in rep.usages:
        print(f"    领用 事件={u.event_id} 资产={u.asset} 该批数量={u.qty} (t={u.time})")
    print(f"    合计：在库 {rep.on_hand_total}，已发 {rep.used_total}")


# --------------------------- 例1：召回盘点全流程 ---------------------------
def example_1_basic_recall() -> None:
    print("=" * 78)
    print("例1 收货/调拨/领用 -> 按批号盘点在库与已发备件")
    print("=" * 78)
    print(
        "手算设定（料号 P，批号 L1 收 10 件入 W1）:\n"
        "  t=1 r1 收货 W1 P/L1 10\n"
        "  t=3 t1 调拨 W1->W2 4（整批出自 L1）\n"
        "  t=5 s1 设备 A1 从 W1 领 3（wh=W1, sku=P；与工单归属无关）\n"
        "  t=6 s-old 设备 A2 旧领用 9（无 wh/sku，不纳入本账）\n"
        "手算: W1 剩 10-4-3=3；W2 有 4；已发设备 3；守恒 3+4+3=10。"
    )
    ledger = RecallLedger([
        Event("s1", 1, SPARE,
              {"asset": "A1", "time": 5, "qty": 3, "wh": "W1", "sku": "P"}),
        Event("s-old", 1, SPARE, {"asset": "A2", "time": 6, "qty": 9}),
    ])
    ledger.bootstrap([
        FlowRecord.receipt("r1", 1, 1, "W1", "P", "L1", 10),
        FlowRecord.transfer("t1", 1, 3, "W1", "W2", "P", 4),
    ])
    show(ledger, "P", "L1")

    rep = ledger.query("P", "L1")
    check(rep.balances == (LotBalance("W1", 3), LotBalance("W2", 4)),
          "非零分仓余量：W1=3, W2=4（按仓名排序）")
    check(rep.usages == (UsageLine("s1", "A1", 3, 5),),
          "领用明细：事件 ID、资产、该批数量")
    check(rep.on_hand_total == 7 and rep.used_total == 3, "在库 7、已发 3")
    check(rep.on_hand_total + rep.used_total == 10, "数量守恒：在库+已发=收货 10")
    check(ledger.query("P", "不存在").balances == ()
          and ledger.query("P", "不存在").usages == (),
          "未知批号返回空账，不报错")
    check("s-old" not in {u.event_id for lot in ledger.lots()
                          for u in ledger.query(*lot).usages},
          "无仓无料号的旧领用不纳入本账")


# --------------------------- 例2：同刻序、FIFO 与批号保留 ---------------------------
def example_2_fifo_and_lot_preservation() -> None:
    print("\n" + "=" * 78)
    print("例2 同刻按 ID 字典序；仓内 FIFO；调拨保留原收货批号、按扣出先后到仓")
    print("=" * 78)
    print(
        "手算设定（仓 W，料号 K）:\n"
        "  t=1 r-a 收 B1 批 3；t=2 r-b 收 B2 批 5 -> 队列 [B1x3, B2x5]\n"
        "  t=3 t1 调拨 W->W2 4：先扣 B1x3 再扣 B2x1，到仓次序同为 B1 后 B2\n"
        "  t=5 同刻两条：收货 r-c（B3 批 2）与领用 s2（2 件），ID 字典序 r-c < s2\n"
        "手算: t5 先收 B3x2 再领 2：W 队列剩 B2x4,B3x2，FIFO 领的 2 件出自 B2；\n"
        "      W2 队列 [B1x3, B2x1]；B1 已全部离 W，W 不再有 B1 余量。"
    )
    ledger = RecallLedger([
        Event("s2", 1, SPARE,
              {"asset": "A2", "time": 5, "qty": 2, "wh": "W", "sku": "K"}),
    ])
    ledger.bootstrap([
        FlowRecord.receipt("r-a", 1, 1, "W", "K", "B1", 3),
        FlowRecord.receipt("r-b", 1, 2, "W", "K", "B2", 5),
        FlowRecord.transfer("t1", 1, 3, "W", "W2", "K", 4),
        FlowRecord.receipt("r-c", 1, 5, "W", "K", "B3", 2),
    ])
    show(ledger, "K", "B1")
    show(ledger, "K", "B2")

    check(ledger.query("K", "B1").balances == (LotBalance("W2", 3),),
          "B1 整批随调拨到 W2，原批号保留，W 无 B1 残留（途经仓不重复计）")
    check(ledger.query("K", "B2").balances
          == (LotBalance("W", 2), LotBalance("W2", 1)),
          "B2：W2 有调拨拆出的 1 件，W 余 5-1-2=2")
    check(ledger.query("K", "B3").balances == (LotBalance("W", 2),),
          "B3 同刻先入账，未被领用（r-c < s2）")
    check(ledger.query("K", "B2").usages == (UsageLine("s2", "A2", 2, 5),),
          "s2 的 2 件按 FIFO 归到 B2（不是新到的 B3）")
    total_in = 3 + 5 + 2
    total_out = sum(ledger.query(s, l).used_total for s, l in ledger.lots())
    total_hold = sum(ledger.query(s, l).on_hand_total for s, l in ledger.lots())
    check(total_in == total_hold + total_out, "守恒：10 = 在库 8 + 已发 2")


# --------------------------- 例3：spare 纳入规则与实物独立性 ---------------------------
def example_3_spare_admission() -> None:
    print("\n" + "=" * 78)
    print("例3 wh/sku 两项都无 -> 不纳入；只缺一项 -> 整批报错；实物出库不看工单")
    print("=" * 78)
    print(
        "手算设定:\n"
        "  s-ok   带 wh=W1, sku=P（还挂了工单 wo=WX，账上不看工单归属）\n"
        "  s-legacy 无 wh 无 sku -> 旧领用，不纳入\n"
        "  s-bad1 只有 wh；s-bad2 只有 sku -> 残缺，整批拒绝"
    )
    ledger = RecallLedger([
        Event("s-ok", 1, SPARE,
              {"asset": "A1", "time": 3, "qty": 2, "wo": "WX",
               "wh": "W1", "sku": "P"}),
        Event("s-legacy", 1, SPARE, {"asset": "A2", "time": 4, "qty": 7}),
    ])
    ledger.bootstrap([FlowRecord.receipt("r1", 1, 1, "W1", "P", "L1", 5)])
    check(ledger.query("P", "L1").usages == (UsageLine("s-ok", "A1", 2, 3),),
          "带工单的领用照样按出库仓/料号实物出库，工单不影响归批")
    check(ledger.query("P", "L1").on_hand_total == 3, "5 收 2 领，余 3")

    for payload, miss in (
        ({"asset": "A9", "time": 1, "qty": 1, "wh": "W1"}, "sku"),
        ({"asset": "A9", "time": 1, "qty": 1, "sku": "P"}, "wh"),
    ):
        try:
            RecallLedger([Event("s-bad", 1, SPARE, payload)])
            raise AssertionError("残缺 spare 应拒绝")
        except ValueError as exc:
            check(miss in str(exc), f"只缺 {miss} 的 spare 整批拒绝并指出缺项")

    # 更正批里混入残缺 spare：同样整批拒绝，已发布版本不变
    before = ledger.version
    try:
        ledger.submit([Event("s-bad1", 1, SPARE,
                             {"asset": "A9", "time": 1, "qty": 1, "wh": "W1"})])
        raise AssertionError("残缺 spare 应拒绝")
    except ValueError:
        ledger.rollback()
    check(ledger.version == before and ledger.proposal is None,
          "残缺更正被拒，已发布版本与提案状态不变")


# --------------------------- 例4：修订协议 ---------------------------
def example_4_revision_protocol() -> None:
    print("\n" + "=" * 78)
    print("例4 同号重送 NOOP / 异内容冲突 / 低修订拒绝 / 高修订整份替换")
    print("=" * 78)
    ledger = RecallLedger()
    rec = FlowRecord.receipt("r1", 1, 1, "W1", "P", "L1", 5)
    ledger.bootstrap([rec])

    prop = ledger.submit([FlowRecord.receipt("r1", 1, 1, "W1", "P", "L1", 5)])
    check(prop.noop_only and ledger.publish() == 1, "同号同内容重送 -> NOOP，不升版")

    try:
        ledger.submit([FlowRecord.receipt("r1", 1, 1, "W1", "P", "L1", 6)])
        raise AssertionError("应冲突")
    except RevisionConflict:
        ledger.rollback()
        check(True, "同号异内容 -> RevisionConflict")

    try:
        ledger.submit([FlowRecord.receipt("r1", 0, 1, "W1", "P", "L1", 6)])
        raise AssertionError("应拒绝")
    except (RevisionRejected, ValueError):
        ledger.rollback()
        check(True, "低/非法修订号 -> 拒绝（revision 必须正整数且严格递增）")

    # 高修订整份替换：收货改调拨（跨类型替换）。先单独替换 -> 起仓无货，
    # submit 阶段即缺货拒绝，已发布版本维持收货旧账。
    try:
        ledger.submit([FlowRecord.transfer("r1", 2, 1, "W1", "W2", "P", 4)])
        raise AssertionError("应缺货")
    except InsufficientStock as exc:
        check(exc.flow_id == "r1", f"整份替换后按新内容重放：{exc.flow_id} 起仓无货")
    check(ledger.version == 1 and ledger.proposal is None,
          "缺货在提交阶段整批拒绝，无残留提案，版本不变")
    check(ledger.query("P", "L1").balances == (LotBalance("W1", 5),),
          "拒绝后查询仍见旧版收货账")

    # 补足起仓库存后，同一高修订替换可以发布
    ledger.submit([
        FlowRecord.receipt("r0", 1, 0, "W1", "P", "L0", 4),
        FlowRecord.transfer("r1", 2, 1, "W1", "W2", "P", 4),
    ])
    ledger.publish()
    check(ledger.query("P", "L0").balances == (LotBalance("W2", 4),),
          "高修订整份替换生效：r1 已变为调拨，L0 的 4 件到 W2")
    check(ledger.query("P", "L1").balances == (),
          "原 L1 收货随 r1 整份替换而消失")

    # 每个 ID 每批至多一条
    try:
        ledger.submit([
            FlowRecord.receipt("z1", 1, 9, "W1", "P", "Lz", 1),
            FlowRecord.receipt("z1", 1, 9, "W1", "P", "Lz", 2),
        ])
        raise AssertionError("应拒绝")
    except ValueError:
        ledger.rollback()
        check(True, "同一批中同一 ID 两条 -> 拒绝")


# --------------------------- 例5：库存不足整批拒绝，查询仍见旧版 ---------------------------
def example_5_insufficient_stock() -> None:
    print("\n" + "=" * 78)
    print("例5 任一流水库存不足 -> 整批拒绝并指出缺货流水；查询仍见上一版")
    print("=" * 78)
    print(
        "手算设定: W1 收 P/L1 3、P/L2 4；t=4 领用 6（FIFO 扣 L1x3+L2x3）。\n"
        "  更正：把 L2 收货量从 4 改成 2 -> t4 只有 3+2=5，领 6 缺 1，\n"
        "  缺货流水是领用 s1（不是被改的收货），整批拒绝。"
    )
    ledger = RecallLedger([
        Event("s1", 1, SPARE,
              {"asset": "A1", "time": 4, "qty": 6, "wh": "W1", "sku": "P"}),
    ])
    ledger.bootstrap([
        FlowRecord.receipt("r1", 1, 1, "W1", "P", "L1", 3),
        FlowRecord.receipt("r2", 1, 2, "W1", "P", "L2", 4),
    ])
    snap_bal = {lot: ledger.query("P", lot).balances for lot in ("L1", "L2")}
    snap_ver = ledger.version

    try:
        ledger.submit([FlowRecord.receipt("r2", 2, 2, "W1", "P", "L2", 2)])
        ledger.publish()
        raise AssertionError("应缺货")
    except InsufficientStock as exc:
        check(exc.flow_id == "s1" and exc.needed == 6 and exc.available == 5,
              "指出缺货流水 s1：需 6 仅有 5")
        print(f"    错误信息: {exc}")
        ledger.rollback()

    check(ledger.version == snap_ver, "拒绝后版本不变")
    check(all(ledger.query("P", lot).balances == snap_bal[lot] for lot in ("L1", "L2")),
          "查询仍见上一版余量")

    # 发布失败同样回滚
    ledger.submit([FlowRecord.receipt("r2", 2, 2, "W1", "P", "L2", 5)])
    try:
        ledger.publish(fail=True)
    except PublishFailed:
        pass
    check(ledger.version == snap_ver and ledger.proposal is None,
          "publish(fail=True) 回滚提案，已发布版本维持旧版")


# --------------------------- 例6：更正按业务时刻生效，后续领用重新归批 ---------------------------
def example_6_correction_re_attribution() -> None:
    print("\n" + "=" * 78)
    print("例6 早期流水更正后，后续领用按更正后的货流重新归批；spare 更正可撤销出库")
    print("=" * 78)
    print(
        "手算设定: t1 收 L1x4，t2 收 L2x6，t3 调拨 7（W1->W2），t4 领用 2。\n"
        "  初版：t3 扣 L1x4+L2x3；t4 W1 只剩 L2，领的 2 件归 L2。\n"
        "  更正一：s1 时刻 t4 -> t2（调拨之前），t2 队列 [L1x4,L2x6]，\n"
        "          领的 2 件改归 L1；t3 调拨随之改为扣 L1x2+L2x5。\n"
        "  更正二：s1 改为无 wh/sku 的旧领用 -> 旧出库撤销，余量回补。"
    )
    ledger = RecallLedger([
        Event("s1", 1, SPARE,
              {"asset": "A1", "time": 4, "qty": 2, "wh": "W1", "sku": "P"}),
    ])
    ledger.bootstrap([
        FlowRecord.receipt("r1", 1, 1, "W1", "P", "L1", 4),
        FlowRecord.receipt("r2", 1, 2, "W1", "P", "L2", 6),
        FlowRecord.transfer("t1", 1, 3, "W1", "W2", "P", 7),
    ])
    check(ledger.query("P", "L2").usages == (UsageLine("s1", "A1", 2, 4),),
          "初版：领用 2 件归 L2")
    check(ledger.query("P", "L1").balances == (LotBalance("W2", 4),),
          "初版：L1 全部 4 件调入 W2")

    ledger.submit([Event("s1", 2, SPARE,
                         {"asset": "A1", "time": 2, "qty": 2, "wh": "W1", "sku": "P"})])
    ledger.publish()
    check(ledger.query("P", "L1").usages == (UsageLine("s1", "A1", 2, 2),),
          "更正后：领用按新业务时刻 t2 重归 L1")
    check(ledger.query("P", "L2").usages == (), "L2 不再承担该领用")
    check(ledger.query("P", "L1").balances == (LotBalance("W2", 2),),
          "调拨货流随之变化：W2 的 L1 变为 2")
    check(ledger.query("P", "L2").balances
          == (LotBalance("W1", 1), LotBalance("W2", 5)),
          "L2：共 6 件，调拨扣 5 入 W2，W1 留 1（领用已不再吃 L2）")

    ledger.submit([Event("s1", 3, SPARE, {"asset": "A1", "time": 2, "qty": 2})])
    ledger.publish()
    check(ledger.query("P", "L1").usages == () and ledger.query("P", "L2").usages == (),
          "spare 更正为无仓无料号记录：旧出库撤销")
    check(ledger.query("P", "L1").on_hand_total + ledger.query("P", "L2").on_hand_total == 10,
          "撤销后在库回补为总收货 10")


# --------------------------- 例7：增量只重读受影响料号与仓群 ---------------------------
def example_7_incremental_scope() -> None:
    print("\n" + "=" * 78)
    print("例7 日常更正只重读被改流水新旧料号及端点所在仓群；其他料号/仓群零访问")
    print("=" * 78)
    print(
        "手算设定:\n"
        "  料号 P 的仓群 {W1,W2}（W1<->W2 有调拨）；料号 Q 独立仓 W9；\n"
        "  另有与 P 完全不通的仓 W3（料号 P 但孤立）。\n"
        "  更正 W1 上一笔 P 的收货 -> 只重读 P 在 {W1,W2} 仓群的流水，\n"
        "  W9/Q 与 W3/P 均不读取、不变更。"
    )
    ledger = RecallLedger([
        Event("sp", 1, SPARE,
              {"asset": "A1", "time": 2, "qty": 1, "wh": "W1", "sku": "P"}),
        Event("sq", 1, SPARE,
              {"asset": "A2", "time": 2, "qty": 1, "wh": "W9", "sku": "Q"}),
    ])
    ledger.bootstrap([
        FlowRecord.receipt("p1", 1, 1, "W1", "P", "LP1", 5),
        FlowRecord.transfer("pt", 1, 3, "W1", "W2", "P", 2),
        FlowRecord.receipt("p3", 1, 1, "W3", "P", "LP3", 7),
        FlowRecord.receipt("q1", 1, 1, "W9", "Q", "LQ1", 9),
    ])
    q_before = ledger.query("Q", "LQ1")
    p3_before = ledger.query("P", "LP3")

    prop = ledger.submit([FlowRecord.receipt("p1", 2, 1, "W1", "P", "LP1", 6)])
    print("  " + prop.describe().replace("\n", "\n  "))
    touched_whs = set()
    for groups in prop.affected_groups.values():
        for g in groups:
            touched_whs |= g
    check(touched_whs == {"W1", "W2"}, "受影响仓群恰为 {W1,W2}")
    check(prop.accessed_ids <= {"p1", "pt", "sp"},
          f"只重读该仓群内 P 的流水，实际 {sorted(prop.accessed_ids)}")
    check("q1" not in prop.accessed_ids and "sq" not in prop.accessed_ids,
          "另一料号 Q 的流水零访问")
    check("p3" not in prop.accessed_ids, "同料号但孤立仓 W3 的流水零访问")
    ledger.publish()

    check((ledger.query("Q", "LQ1").balances, ledger.query("Q", "LQ1").usages)
          == (q_before.balances, q_before.usages), "Q/W9 账面完全不变")
    check((ledger.query("P", "LP3").balances, ledger.query("P", "LP3").usages)
          == (p3_before.balances, p3_before.usages), "孤立仓 W3 的 P 账面完全不变")
    check(ledger.query("P", "LP1").balances
          == (LotBalance("W1", 3), LotBalance("W2", 2)),
          "LP1 按更正后货流：收 6、领 1、调 2 -> W1 留 3、W2 存 2（全仓在库 5）")
    check(ledger.query("P", "LP1").used_total == 1, "已发设备仍为 1，归批不变")

    # 与朴素全量重算逐批对比
    ref = RecallLedger([
        Event("sp", 1, SPARE,
              {"asset": "A1", "time": 2, "qty": 1, "wh": "W1", "sku": "P"}),
        Event("sq", 1, SPARE,
              {"asset": "A2", "time": 2, "qty": 1, "wh": "W9", "sku": "Q"}),
    ])
    ref.bootstrap([
        FlowRecord.receipt("p1", 2, 1, "W1", "P", "LP1", 6),
        FlowRecord.transfer("pt", 1, 3, "W1", "W2", "P", 2),
        FlowRecord.receipt("p3", 1, 1, "W3", "P", "LP3", 7),
        FlowRecord.receipt("q1", 1, 1, "W9", "Q", "LQ1", 9),
    ])
    for sku, lot in ledger.lots():
        got, want = ledger.query(sku, lot), ref.query(sku, lot)
        check(got.balances == want.balances and got.usages == want.usages,
              f"增量结果与全量重算一致：{sku}/{lot}")


def main() -> None:
    example_1_basic_recall()
    example_2_fifo_and_lot_preservation()
    example_3_spare_admission()
    example_4_revision_protocol()
    example_5_insufficient_stock()
    example_6_correction_re_attribution()
    example_7_incremental_scope()
    print("\n" + "=" * 78)
    print("全部批号召回账手算例核对通过。")
    print("=" * 78)


if __name__ == "__main__":
    main()
