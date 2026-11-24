"""短装结算:是否补发、补了能否弥补逾期责任、既成船费,顺着同一次测算打通。

口径:
- 客户可以在期限内(max_hour)确认收下部分,或按订货条款要求后来这班补齐。
- 接受的已交成套数有效,但不能视作余量免付或逾期责任免除。
- dock_costs 里 actual=True 是已发生的既成费用,单列,不和预测补发成本相抵。
"""

from __future__ import annotations

from dataclasses import dataclass

from .ledger import Ledger
from .reconcile import OrderFulfillment, Reconciliation
from .readiness import LotForecast, joint_prob_ready
from .sources import Dataset, DockCost


@dataclass(frozen=True)
class SupplementRoute:
    sailing: str
    depart_h: float
    pieces_by_lot: tuple[tuple[str, int], ...]
    feasible: bool
    cures_late: bool  # 补发到达不超过客户期限,可弥补逾期责任
    ready_prob: float
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class OrderSettlement:
    order: str
    departed_sets: int | None
    accepted_sets: int
    owed_sets: int | None  # 仍欠客户的成套数
    owed_pieces: tuple[tuple[str, int], ...]  # 分包欠件
    debit_confirmed: float  # 已确认扣款
    penalty_exposure: float  # 合同赔付敞口
    routes: tuple[SupplementRoute, ...]
    decision: str  # 补发 / 不补 / 需人工
    decision_reasons: tuple[str, ...]
    sunk_costs: tuple[tuple[str, str, float], ...]  # (cost id, kind, amount) 既成
    predicted_costs: tuple[tuple[str, str, float], ...]  # 预估,不与既成相抵
    net_if_supplement: float | None
    net_if_not: float


def _order_costs(ds: Dataset, order: str) -> tuple[list[DockCost], list[DockCost]]:
    """按 箱→分包→订单 归集费用;返回 (既成, 预估)。"""
    lots = {line.lot for line in ds.order_lines(order)}
    boxes = {
        event.box for event in ds.load_events if event.lot in lots
    }
    sunk: list[DockCost] = []
    predicted: list[DockCost] = []
    for cost in ds.dock_costs:
        if cost.box not in boxes:
            continue
        (sunk if cost.actual else predicted).append(cost)
    return sunk, predicted


def supplement_routes(
    ds: Dataset,
    ledger: Ledger,
    forecasts: dict[str, LotForecast],
    fulfillment: OrderFulfillment,
    now_h: float,
    max_hour: float | None,
) -> list[SupplementRoute]:
    """欠件的候选补发班次;max_hour 内发运才算弥补逾期责任。

    可拆单的补发仍要保住客户约定的最小批量、先后批次间隔及非同船限制。
    """
    lines = ds.order_lines(fulfillment.order)
    destination = lines[0].destination
    owed = [(lot.lot, lot.short) for lot in fulfillment.lots if lot.short > 0]
    if not owed:
        return []
    pieces = sum(short for _, short in owed)
    separate = any(line.separate_sail for line in lines)
    min_gap = max((line.min_gap_h for line in lines), default=0)
    min_batch = min(
        (line.split_min_batch or line.pieces for line in lines), default=0
    )
    existing = [
        (sailing, pieces)
        for _, sailing, pieces in fulfillment.batches if sailing
    ]
    routes: list[SupplementRoute] = []
    for sailing in ds.sailings:
        if sailing.destination != destination:
            continue
        if now_h > sailing.cut_h:
            continue
        reasons: list[str] = []
        feasible = True
        left = ledger.remaining(sailing.sailing)
        if left < pieces:
            feasible = False
            reasons.append(f"余舱 {left} 件 < 欠件 {pieces} 件")
        if fulfillment.granularity == "可拆":
            if pieces < min_batch:
                feasible = False
                reasons.append(f"补发 {pieces} 件 < 最小批量 {min_batch} 件")
            if separate and any(sailing.sailing == s for s, _ in existing):
                feasible = False
                reasons.append("违反非同船限制:该班已有本单批次")
            for existing_sailing, _ in existing:
                gap = abs(sailing.depart_h - ledger.sailings[existing_sailing].depart_h)
                if sailing.sailing != existing_sailing and gap < min_gap:
                    feasible = False
                    reasons.append(
                        f"与 {existing_sailing} 批次间隔 {gap:.0f}h < 约定 {min_gap}h"
                    )
        lots = [lot for lot, _ in owed]
        prob = joint_prob_ready(
            forecasts, lots, sailing.cut_h, groups={lot: sailing.sailing for lot in lots}
        )
        cures = max_hour is not None and sailing.depart_h <= max_hour
        if max_hour is not None and not cures:
            reasons.append(
                f"发船 {sailing.depart_h}h 晚于客户期限 {max_hour}h,补了也不能弥补逾期责任"
            )
        routes.append(
            SupplementRoute(
                sailing=sailing.sailing,
                depart_h=sailing.depart_h,
                pieces_by_lot=tuple(owed),
                feasible=feasible,
                cures_late=cures,
                ready_prob=prob,
                reasons=tuple(reasons),
            )
        )
    return routes


def settle(
    ds: Dataset,
    ledger: Ledger,
    recon: Reconciliation,
    forecasts: dict[str, LotForecast],
    now_h: float,
) -> list[OrderSettlement]:
    settlements: list[OrderSettlement] = []
    for order in ds.order_names():
        fulfillment = recon.order(order)
        responses = [r for r in ds.customer_responses if r.order == order]
        accepted = max((r.accepted_sets for r in responses), default=0)
        debits = [r for r in responses if r.kind == "逾期扣款确认"]
        debit = sum(r.debit_cny for r in debits)
        demands = [r for r in responses if r.kind == "要求补发"]
        max_hour = min(
            [r.max_hour for r in responses if r.max_hour is not None], default=None
        )
        penalty = ds.order_lines(order)[0].penalty

        if fulfillment.granularity == "整套":
            owed_sets = (fulfillment.sets_target or 0) - (fulfillment.departed_sets or 0)
        else:
            owed_sets = None
        owed_pieces = tuple(
            (lot.lot, lot.short) for lot in fulfillment.lots if lot.short > 0
        )
        sunk, predicted = _order_costs(ds, order)
        sunk_total = sum(c.amount for c in sunk)
        supplement_estimate = sum(
            c.amount for c in predicted if c.kind == "补发预估"
        )

        routes = supplement_routes(
            ds, ledger, forecasts, fulfillment, now_h, max_hour
        )
        feasible_routes = [r for r in routes if r.feasible]
        curing = [r for r in feasible_routes if r.cures_late]
        must_supplement = bool(demands)

        # 净成本:既成费用单列;补发则加预估补发费,不补则承担扣款与赔付敞口
        net_if_supplement = (
            sunk_total + supplement_estimate if feasible_routes else None
        )
        net_if_not = sunk_total + debit + (0.0 if owed_sets == 0 else penalty)

        reasons: list[str] = []
        if not owed_pieces:
            decision = "不补"
            reasons.append("无欠件")
        elif curing and (must_supplement or supplement_estimate <= debit + penalty):
            decision = "补发"
            best = min(curing, key=lambda r: r.depart_h)
            reasons.append(
                f"{best.sailing} {best.depart_h}h 发,期限 {max_hour}h 内,可弥补逾期责任"
            )
            if must_supplement:
                reasons.append("客户已按订货条款要求补齐")
        elif feasible_routes:
            decision = "需人工"
            reasons.append("可行班次都晚于客户期限,补了也不能弥补逾期责任,需与客户商议")
        elif routes:
            decision = "需人工"
            reasons.append("候选班次受舱位、批次间隔或非同船限制,需人工协调")
        else:
            decision = "需人工"
            reasons.append("无可行补发班次,需人工协调舱位或改商议赔付")

        settlements.append(
            OrderSettlement(
                order=order,
                departed_sets=fulfillment.departed_sets,
                accepted_sets=accepted,
                owed_sets=owed_sets,
                owed_pieces=owed_pieces,
                debit_confirmed=debit,
                penalty_exposure=penalty if owed_pieces else 0.0,
                routes=tuple(routes),
                decision=decision,
                decision_reasons=tuple(reasons),
                sunk_costs=tuple((c.cost, c.kind, c.amount) for c in sunk),
                predicted_costs=tuple((c.cost, c.kind, c.amount) for c in predicted),
                net_if_supplement=net_if_supplement,
                net_if_not=net_if_not,
            )
        )
    return settlements
