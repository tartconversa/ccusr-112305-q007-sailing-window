"""业务复盘:为什么上一班没选、客户实际收到的配套量、延误风险有多少变成了事实。"""

from __future__ import annotations

from .ledger import Ledger
from .reconcile import reconcile
from .readiness import DeskModel, forecast_lots, joint_prob_ready
from .settlement import settle
from .sources import Dataset


def _sailing_choice_review(
    ds: Dataset, model: DeskModel, order: str, book_h: float, lots: list[str]
) -> list[str]:
    """以订舱之前可见的信息,重算当时各班的联合准时概率与舱位。"""
    lines = [line for line in ds.order_lines(order) if line.lot in set(lots)]
    pieces = sum(line.pieces for line in lines)
    destination = lines[0].destination if lines else ds.order_lines(order)[0].destination
    # 订舱时点之前的台账(不含本次预订本身)
    ledger_then = Ledger.build(ds.sailings, ds.orders, ds.carrier_events,
                               until_h=book_h - 1e-9)
    forecasts_then = forecast_lots(ds.materials, ds.evidence, model, book_h, lots=lots)
    out = []
    for sailing in ds.sailings:
        if sailing.destination != destination:
            continue
        prob = joint_prob_ready(
            forecasts_then, lots, sailing.cut_h,
            groups={lot: sailing.sailing for lot in lots},
        )
        left = ledger_then.remaining(sailing.sailing)
        fit = "够" if left >= pieces else f"不足(余 {left} 件需 {pieces} 件)"
        out.append(
            f"    当时 {sailing.sailing}:截 {sailing.cut_h}h 联合准时 {prob:.0%},舱位{fit}"
        )
    return out


def review(ds: Dataset, now_h: float) -> str:
    model = DeskModel.fit(ds.history)
    ledger = Ledger.build(ds.sailings, ds.orders, ds.carrier_events, until_h=now_h)
    recon = reconcile(ds.load_events, ledger, ds.orders, ds.materials)
    forecasts = forecast_lots(ds.materials, ds.evidence, model, now_h)
    settlements = settle(ds, ledger, recon, forecasts, now_h)

    out = [f"== 业务复盘(截至 {now_h}h)=="]
    for order in ds.order_names():
        lines = ds.order_lines(order)
        fulfillment = recon.order(order)
        out.append(f"\n[{order}] {lines[0].bundle} → {lines[0].destination}")

        # 预订经过(为什么上一班没选/没成)
        bookings = [e for e in ds.carrier_events if e.order == order]
        for event in sorted(bookings, key=lambda e: e.hour):
            out.append(
                f"  {event.hour}h {event.event} {event.booking} → {event.sailing}:{event.state}"
            )
        first = min(bookings, key=lambda e: e.hour, default=None)
        if first is not None:
            out.append("  订舱时点的班次对比(为什么上一班没选):")
            out.extend(
                _sailing_choice_review(ds, model, order, first.hour, list(first.lots))
            )
        for line in ledger.log:
            if any(b.booking in line for b in bookings):
                out.append(f"  台账:{line}")

        # 客户实际收到的配套量
        if fulfillment.granularity == "整套":
            out.append(
                f"  实际离港可成套 {fulfillment.departed_sets}/"
                f"{fulfillment.sets_target} 套"
            )
            for lot in fulfillment.lots:
                extra = (
                    f",半套余量 {lot.surplus} 件(不能转作别的客户的整套)"
                    if lot.surplus else ""
                )
                out.append(
                    f"    {lot.lot}:需 {lot.required} 装 {lot.shipped} 欠 {lot.short}{extra}"
                )
        else:
            for lot in fulfillment.lots:
                out.append(
                    f"    {lot.lot}:需 {lot.required} 装 {lot.shipped} 欠 {lot.short}"
                )
        responses = [r for r in ds.customer_responses if r.order == order]
        for r in sorted(responses, key=lambda r: r.time):
            out.append(
                f"  客户 {r.time}h {r.kind}:接受 {r.accepted_sets} 套,"
                f"期限 {r.max_hour}h,扣款 {r.debit_cny:.0f}({r.agreement})"
            )

        # 延误风险变现
        st = next(s for s in settlements if s.order == order)
        net_sup = "—" if st.net_if_supplement is None else f"{st.net_if_supplement:.0f}"
        out.append(
            f"  结算:决定 {st.decision};既成费用 "
            f"{sum(a for _, _, a in st.sunk_costs):.0f},净成本(补){net_sup}"
            f" / (不补){st.net_if_not:.0f}"
        )
        for lot in [line.lot for line in lines]:
            fc = forecasts[lot]
            evidence = [e for e in ds.evidence if e.lot == lot]
            if evidence:
                last = max(evidence, key=lambda e: e.hour)
                out.append(
                    f"  风险 {lot}:当前预测就绪 {fc.mu:.0f}h({fc.basis});"
                    f"最新证据 {last.hour}h → {last.new_ready}h({last.inspected})"
                )
    # 风险变现汇总:订舱时低概率的班次后来是否真的误点
    out.append("\n== 延误风险变现 ==")
    for event in sorted(ds.carrier_events, key=lambda e: e.hour):
        if event.event != "预订" or event.state != "接受":
            continue
        ledger_then = Ledger.build(ds.sailings, ds.orders, ds.carrier_events,
                                   until_h=event.hour)
        forecasts_then = forecast_lots(ds.materials, ds.evidence, model, event.hour,
                                       lots=list(event.lots))
        sailing = ds.sailing(event.sailing)
        prob = joint_prob_ready(
            forecasts_then, list(event.lots), sailing.cut_h,
            groups={lot: event.sailing for lot in event.lots},
        )
        facts = []
        for lot in event.lots:
            later = [e for e in ds.evidence if e.lot == lot and e.hour > event.hour]
            if later:
                last = max(later, key=lambda e: e.hour)
                missed = "误班" if last.new_ready > sailing.cut_h else "赶及"
                facts.append(f"{lot} 实际就绪 {last.new_ready}h({missed}截 {sailing.cut_h}h)")
        if facts:
            out.append(
                f"  {event.booking} → {event.sailing}:订时联合准时 {prob:.0%};"
                + ";".join(facts)
            )
    return "\n".join(out)
