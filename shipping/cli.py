"""命令行入口:python3 -m shipping <命令> [--data 目录] [--now 小时]"""

from __future__ import annotations

import argparse

from .backtest import backtest, format_backtest
from .ledger import BOOK, MODIFY, Ledger
from .plan import check_booking_constraints, format_weekly, weekly_options
from .readiness import DeskModel, forecast_lots
from .reconcile import reconcile
from .replan import evaluate_evidence, evaluate_replan
from .requote import format_proposals, proposals
from .review import review
from .settlement import settle
from .sources import (
    ACCEPTED,
    PENDING_REPLY,
    CarrierEvent,
    DEFAULT_DATA,
    append_carrier_event,
    load_dataset,
)


def _latest_hour(ds) -> float:
    hours = [0.0]
    hours += [e.hour for e in ds.carrier_events]
    hours += [e.hour for e in ds.load_events]
    hours += [e.hour for e in ds.evidence]
    hours += [r.time for r in ds.customer_responses]
    hours += [c.known_at for c in ds.dock_costs]
    return max(hours)


def _context(args):
    ds = load_dataset(args.data)
    now = args.now if args.now is not None else _latest_hour(ds)
    model = DeskModel.fit(ds.history)
    ledger = Ledger.build(ds.sailings, ds.orders, ds.carrier_events, until_h=now)
    forecasts = forecast_lots(ds.materials, ds.evidence, model, now)
    return ds, now, model, ledger, forecasts


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="shipping", description="排运测算")
    parser.add_argument("--data", default=str(DEFAULT_DATA), help="资料目录")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_now(p):
        p.add_argument("--now", type=float, default=None,
                       help="当前时点(排运日零时后小时),默认取资料中最晚时点")

    p_weekly = sub.add_parser("weekly", help="周会方案比较")
    add_now(p_weekly)
    p_weekly.add_argument("--waitlist", type=float, default=0.5, help="候补兑现概率假设")

    p_replan = sub.add_parser("replan", help="换舱判定")
    p_replan.add_argument("order")
    p_replan.add_argument("target", nargs="?", default=None)
    add_now(p_replan)

    p_evidence = sub.add_parser("evidence", help="新质检证据触发的换舱判定")
    p_evidence.add_argument("lot")
    add_now(p_evidence)

    p_recon = sub.add_parser("reconcile", help="接货核实")
    add_now(p_recon)

    p_settle = sub.add_parser("settle", help="短装结算")
    add_now(p_settle)

    p_requote = sub.add_parser("requote", help="重新报价")
    add_now(p_requote)

    p_back = sub.add_parser("backtest", help="往季重跑")
    p_back.add_argument("--capacity", type=int, default=None,
                        help="每运输批次可发票数(紧张舱位)")
    p_back.add_argument("--level", type=float, default=0.8, help="准时区间置信水平")

    p_review = sub.add_parser("review", help="业务复盘")
    add_now(p_review)

    p_book = sub.add_parser("book", help="排运员确认预订,占用真实箱位并写回台账")
    p_book.add_argument("order")
    p_book.add_argument("sailing")
    p_book.add_argument("booking", help="预订号")
    p_book.add_argument("--lots", default=None, help="分包,逗号分隔;默认整单")
    add_now(p_book)

    p_modify = sub.add_parser("modify", help="登记撤改申请(待答复),原舱保持占舱")
    p_modify.add_argument("booking", help="新撤改单号")
    p_modify.add_argument("order")
    p_modify.add_argument("sailing")
    p_modify.add_argument("--lots", default=None, help="分包,逗号分隔;默认整单")
    add_now(p_modify)

    args = parser.parse_args(argv)

    if args.command == "backtest":
        ds = load_dataset(args.data)
        report = backtest(ds.history, level=args.level, capacity_per_group=args.capacity)
        print(format_backtest(report))
        return

    ds, now, model, ledger, forecasts = _context(args)

    if args.command == "weekly":
        options = weekly_options(ds, ledger, forecasts, now,
                                 waitlist_clearance=args.waitlist)
        print(format_weekly(ds, ledger, options, now))
    elif args.command == "replan":
        verdict = evaluate_replan(ds, ledger, forecasts, args.order, args.target, now)
        _print_verdict(verdict)
    elif args.command == "evidence":
        verdicts = evaluate_evidence(ds, ledger, forecasts, args.lot, now)
        if not verdicts:
            print(f"{args.lot} 没有需要换舱的在持预订")
        for verdict in verdicts:
            _print_verdict(verdict)
    elif args.command == "reconcile":
        recon = reconcile(ds.load_events, ledger, ds.orders, ds.materials)
        print(_format_recon(recon))
    elif args.command == "settle":
        recon = reconcile(ds.load_events, ledger, ds.orders, ds.materials)
        for st in settle(ds, ledger, recon, forecasts, now):
            print(_format_settlement(st))
    elif args.command == "requote":
        recon = reconcile(ds.load_events, ledger, ds.orders, ds.materials)
        settlements = settle(ds, ledger, recon, forecasts, now)
        print(format_proposals(proposals(ds, ledger, recon, forecasts, settlements, now)))
    elif args.command == "review":
        print(review(ds, now))
    elif args.command == "book":
        lots = tuple(args.lots.split(",")) if args.lots else tuple(
            line.lot for line in ds.order_lines(args.order)
        )
        problems = check_booking_constraints(ds, ledger, args.order, lots, args.sailing)
        if problems:
            for problem in problems:
                print(f"不能确认:{problem}")
            return
        # 确认的预订立即占用真实箱位;占不了就不写回
        try:
            ledger.confirm(args.booking, args.order, lots, args.sailing, now)
        except ValueError as exc:
            print(exc)
            return
        append_carrier_event(args.data, CarrierEvent(
            event=BOOK, booking=args.booking, order=args.order, lots=lots,
            sailing=args.sailing, hour=now, state=ACCEPTED,
        ))
        print(f"{args.booking} 已确认,占 {args.sailing} "
              f"{sum(ds.lot_pieces(lot) for lot in lots)} 件;"
              f"余舱 {ledger.remaining_table()}")
    elif args.command == "modify":
        lots = tuple(args.lots.split(",")) if args.lots else tuple(
            line.lot for line in ds.order_lines(args.order)
        )
        verdict = evaluate_replan(ds, ledger, forecasts, args.order, args.sailing, now)
        _print_verdict(verdict)
        if verdict.blocks:
            print("撤改未登记:先处理阻断项")
            return
        # 持证申请:登记待答复撤改单,原舱不动,货代接受才生效
        event = CarrierEvent(
            event=MODIFY, booking=args.booking, order=args.order, lots=lots,
            sailing=args.sailing, hour=now, state=PENDING_REPLY,
        )
        append_carrier_event(args.data, event)
        print(f"{args.booking} 撤改申请已登记(待答复),原舱保持占舱")


def _print_verdict(verdict) -> None:
    target = f" → {verdict.target}" if verdict.target else ""
    print(f"[{verdict.order}]{target} {verdict.action}(保留原状态:{verdict.keep_original})")
    for key, amount in verdict.fees.items():
        print(f"  费用:{key} {amount:.0f}")
    for block in verdict.blocks:
        print(f"  阻断:{block}")
    for item in verdict.manual_confirm:
        print(f"  需人工确认:{item}")
    for note in verdict.notes:
        print(f"  说明:{note}")


def _format_recon(recon) -> str:
    out = ["== 接货核实(以码头实装为准)=="]
    for box in recon.boxes:
        stuffed = (
            f"入柜 {box.stuffed_pieces} 件" if box.stuffed_pieces else "无入柜记录"
        )
        state = (
            f"已离港 {box.sailing} {box.departed_pieces} 件"
            if box.departed else "未离港"
        )
        out.append(
            f"  箱 {box.box}({box.lot}){stuffed},{state}"
            + (f",短装 {box.short_load} 件" if box.short_load else "")
        )
        for void in box.void_reports:
            out.append(f"    作废抄送 {void}")
        for conflict in box.conflicts:
            out.append(f"    冲突:{conflict}")
    for order, fulfillment in recon.orders.items():
        if fulfillment.granularity == "整套":
            out.append(
                f"  {order} 整套:离港可成套 {fulfillment.departed_sets}/"
                f"{fulfillment.sets_target} 套"
            )
        else:
            out.append(f"  {order} 可拆(按件/批计量):")
        for lot in fulfillment.lots:
            extra = f",半套余量 {lot.surplus} 件不可转单" if lot.surplus else ""
            out.append(
                f"    {lot.lot} 需 {lot.required} 装 {lot.shipped} 欠 {lot.short}{extra}"
            )
    for warning in recon.warnings:
        out.append(f"  ! {warning}")
    return "\n".join(out)


def _format_settlement(st) -> str:
    out = [f"[{st.order}] 决定:{st.decision}"]
    if st.departed_sets is not None:
        out.append(f"  已离港 {st.departed_sets} 套,客户接受 {st.accepted_sets} 套,"
                   f"仍欠 {st.owed_sets} 套")
    if st.owed_pieces:
        owed = ",".join(f"{lot} {n} 件" for lot, n in st.owed_pieces)
        out.append(f"  欠件:{owed}")
    for route in st.routes:
        cures = "可弥补逾期" if route.cures_late else "不能弥补逾期"
        mark = "可行" if route.feasible else "不可行"
        out.append(
            f"  补发 {route.sailing}:{route.depart_h}h 发 [{mark}],{cures},"
            f"联合准时 {route.ready_prob:.0%}"
        )
        for reason in route.reasons:
            out.append(f"    ! {reason}")
    for reason in st.decision_reasons:
        out.append(f"  依据:{reason}")
    if st.sunk_costs:
        sunk = ",".join(f"{cid}:{a:.0f}" for cid, _, a in st.sunk_costs)
        out.append(f"  既成费用(不相抵):{sunk}")
    if st.predicted_costs:
        pred = ",".join(f"{cid}:{a:.0f}" for cid, _, a in st.predicted_costs)
        out.append(f"  预估费用:{pred}")
    if st.net_if_supplement is not None:
        out.append(f"  净成本:补 {st.net_if_supplement:.0f} / 不补 {st.net_if_not:.0f}")
    else:
        out.append(f"  净成本:不补 {st.net_if_not:.0f}")
    return "\n".join(out)


if __name__ == "__main__":
    main()
