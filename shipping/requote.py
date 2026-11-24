"""重新报价:加急与商议赔付对剩余舱位、已兑现数量及当前净成本的影响。

每份提议都能追到:剩余套数、最后可行班次、既成运输和赔付的回单。
"""

from __future__ import annotations

from dataclasses import dataclass

from .ledger import Ledger
from .reconcile import Reconciliation
from .readiness import LotForecast
from .settlement import OrderSettlement
from .sources import Dataset


@dataclass(frozen=True)
class Proposal:
    order: str
    name: str  # 加急补发 / 普通补发 / 商议赔付 / 维持现状
    unit: str  # 套 / 件(计量粒度)
    sets_covered: int  # 提议落地后累计可兑现量
    remaining_sets: int  # 剩余量
    last_feasible_sailing: str | None  # 最后可行班次
    capacity_used: tuple[tuple[str, int], ...]  # 占用的剩余舱位
    new_costs: tuple[tuple[str, float], ...]  # 新发生费用 (项目, 金额)
    sunk_costs: tuple[tuple[str, str, float], ...]  # 既成费用回单
    receipts: tuple[str, ...]  # 既成运输和赔付的回单号
    net_cost: float  # 当前净成本 = 既成 + 新发生 + 残余赔付
    notes: tuple[str, ...]


def proposals(
    ds: Dataset,
    ledger: Ledger,
    recon: Reconciliation,
    forecasts: dict[str, LotForecast],
    settlements: list[OrderSettlement],
    now_h: float,
) -> list[Proposal]:
    out: list[Proposal] = []
    for st in settlements:
        order = st.order
        fulfillment = recon.order(order)
        if not st.owed_pieces:
            continue
        responses = [r for r in ds.customer_responses if r.order == order]
        receipts = tuple(
            [c[0] for c in st.sunk_costs] + [r.response for r in responses]
        )
        sunk_total = sum(amount for _, _, amount in st.sunk_costs)
        max_hour = min(
            [r.max_hour for r in responses if r.max_hour is not None], default=None
        )
        routes = [r for r in st.routes if r.feasible]
        last_feasible = max(routes, key=lambda r: r.depart_h).sailing if routes else None
        if fulfillment.granularity == "整套":
            unit = "套"
            done = fulfillment.departed_sets or 0
            owed = st.owed_sets or 0
        else:
            unit = "件"
            done = sum(lot.shipped for lot in fulfillment.lots)
            owed = sum(short for _, short in st.owed_pieces)

        for route in routes:
            sailing = ledger.sailings[route.sailing]
            pieces = sum(p for _, p in route.pieces_by_lot)
            supplement_estimate = sum(
                amount for _, kind, amount in st.predicted_costs
                if kind == "补发预估"
            )
            residual_debit = 0.0 if route.cures_late else st.debit_confirmed
            notes = []
            if route.cures_late:
                notes.append(
                    f"{route.depart_h}h 发,期限 {max_hour}h 内,补发部分逾期责任可弥补"
                )
            else:
                notes.append(
                    f"{route.depart_h}h 发,晚于期限 {max_hour}h,已确认扣款 "
                    f"{st.debit_confirmed:.0f} 不免"
                )
            out.append(Proposal(
                order=order,
                name=f"普通补发 {route.sailing}",
                unit=unit,
                sets_covered=done + owed,
                remaining_sets=0,
                last_feasible_sailing=last_feasible,
                capacity_used=((route.sailing, pieces),),
                new_costs=(("补发运费", supplement_estimate),),
                sunk_costs=st.sunk_costs,
                receipts=receipts,
                net_cost=sunk_total + supplement_estimate + residual_debit,
                notes=tuple(notes),
            ))
            out.append(Proposal(
                order=order,
                name=f"加急补发 {route.sailing}",
                unit=unit,
                sets_covered=done + owed,
                remaining_sets=0,
                last_feasible_sailing=last_feasible,
                capacity_used=((route.sailing, pieces),),
                new_costs=(
                    ("补发运费", supplement_estimate),
                    ("加急费", sailing.emergency_fee),
                ),
                sunk_costs=st.sunk_costs,
                receipts=receipts,
                net_cost=(
                    sunk_total + supplement_estimate
                    + sailing.emergency_fee + residual_debit
                ),
                notes=tuple(["加急占同一班次舱位,费用更高,时点不变"] + notes),
            ))

        # 商议赔付:不动舱位,按已确认扣款与赔付敞口了结
        out.append(Proposal(
            order=order,
            name="商议赔付",
            unit=unit,
            sets_covered=done,
            remaining_sets=owed,
            last_feasible_sailing=last_feasible,
            capacity_used=(),
            new_costs=(
                ("已确认扣款", st.debit_confirmed),
                ("剩余赔付敞口", st.penalty_exposure - st.debit_confirmed
                 if st.penalty_exposure > st.debit_confirmed else 0.0),
            ),
            sunk_costs=st.sunk_costs,
            receipts=receipts,
            net_cost=sunk_total + max(st.debit_confirmed, st.penalty_exposure),
            notes=(
                f"不再补发,剩余 {owed} {unit}以赔付了结;"
                f"剩余舱位不受影响",
            ),
        ))
    return out


def format_proposals(props: list[Proposal]) -> str:
    out = ["== 重新报价提议 =="]
    for prop in props:
        out.append(f"\n[{prop.order}] {prop.name}")
        out.append(
            f"  兑现 {prop.sets_covered} {prop.unit},剩余 {prop.remaining_sets} {prop.unit};"
            f"最后可行班次 {prop.last_feasible_sailing or '无'}"
        )
        if prop.capacity_used:
            used = ",".join(f"{s} {p} 件" for s, p in prop.capacity_used)
            out.append(f"  占用剩余舱位:{used}")
        for label, amount in prop.new_costs:
            out.append(f"  新发生:{label} {amount:.0f}")
        if prop.sunk_costs:
            sunk = ",".join(f"{cid}:{amount:.0f}" for cid, _, amount in prop.sunk_costs)
            out.append(f"  既成费用:{sunk}")
        out.append(f"  回单:{','.join(prop.receipts)}")
        out.append(f"  当前净成本 {prop.net_cost:.0f}")
        for note in prop.notes:
            out.append(f"  ! {note}")
    return "\n".join(out)
