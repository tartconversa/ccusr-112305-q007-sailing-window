"""重排约束:取消截止附近的换舱判定。

规则:
- 重排时不能先放掉已预订的最后一个可行班次再去试更优班次——
  一律"持证申请":原舱保留,撤改被接受才换,被拒则原舱不动。
- cancel_h 之后货代仍可能接受但需计费;cut_h 之后不能新装。
- 已装车的分包不可自行撤回,需以货代解除或拒绝回执为据。
- 同一箱(同一船班)上的其他订单还要能成套,不能只挪走自己。
- 无法同时保住这些承诺时,保留原状态,列出需人工确认的差额。
"""

from __future__ import annotations

from dataclasses import dataclass

from .ledger import Ledger
from .readiness import LotForecast, joint_prob_ready
from .sources import Dataset


@dataclass(frozen=True)
class ReplanVerdict:
    order: str
    target: str | None
    action: str  # 持证申请撤改 / 保持原状 / 直接预订
    keep_original: bool
    fees: dict[str, float]
    blocks: tuple[str, ...] = ()
    manual_confirm: tuple[str, ...] = ()  # 需人工确认的差额
    notes: tuple[str, ...] = ()


def _release_evidence(ledger: Ledger, lot: str) -> bool:
    """已装车分包的撤回凭据:货代解除或拒绝回执覆盖该分包。"""
    for view in ledger.dead.values():
        if lot in view.lots:
            return True
    return False


def evaluate_replan(
    ds: Dataset,
    ledger: Ledger,
    forecasts: dict[str, LotForecast],
    order: str,
    target_sailing: str | None,
    now_h: float,
    min_ready_prob: float = 0.5,
) -> ReplanVerdict:
    """评估把 order 挪到 target_sailing(None 表示只评估现状能撑多久)。"""
    lines = ds.order_lines(order)
    lots = [line.lot for line in lines]
    pieces = sum(line.pieces for line in lines)
    current = ledger.order_booking(order)
    blocks: list[str] = []
    manual: list[str] = []
    notes: list[str] = []
    fees: dict[str, float] = {}

    # 整套合同不能拆着挪
    if lines[0].bundle == "整套" and target_sailing is not None:
        notes.append("整套合同只允许整套发运,按全部分包同时就绪评估")

    # 已装车分包的可撤回性
    for line in lines:
        material = ds.material(line.lot)
        if material and material.loaded and not _release_evidence(ledger, line.lot):
            blocks.append(
                f"{line.lot} 已装车,不可自行撤回,需货代解除或拒绝回执"
            )

    if current is None:
        if target_sailing is None:
            return ReplanVerdict(order, None, "保持原状", True, fees,
                                 notes=tuple(["订单尚未订舱"] + notes))
        target = ledger.sailings[target_sailing]
        if target.destination != lines[0].destination:
            return ReplanVerdict(
                order, target_sailing, "保持原状", True, fees,
                blocks=(f"{target_sailing} 目的港 {target.destination} "
                        f"与订单 {lines[0].destination} 不符",),
                notes=tuple(notes),
            )
        ok, reason = ledger.can_hold(target_sailing, pieces, now_h)
        if not ok:
            return ReplanVerdict(order, target_sailing, "保持原状", True, fees,
                                 blocks=(reason,), notes=tuple(notes))
        return ReplanVerdict(order, target_sailing, "直接预订", False, fees,
                             notes=tuple(notes))

    current_sailing = ledger.sailings[current.sailing]

    # 取消截止:超过后货代仍可能接受但需计费
    if now_h > current_sailing.cancel_h:
        fees["改舱费"] = current_sailing.cancel_fee
        manual.append(
            f"已过 {current.sailing} 取消截止 {current_sailing.cancel_h}h,"
            f"货代接受仍可能但需计费 {current_sailing.cancel_fee:.0f}"
        )
    if now_h > current_sailing.cut_h:
        blocks.append(f"{current.sailing} 已过截装 {current_sailing.cut_h}h,原舱已不可调整")

    if target_sailing is None:
        return ReplanVerdict(order, None, "保持原状", True, fees,
                             blocks=tuple(blocks), manual_confirm=tuple(manual),
                             notes=tuple(notes))

    target = ledger.sailings[target_sailing]
    if target.destination != current_sailing.destination:
        blocks.append(f"{target_sailing} 目的港 {target.destination} 与订单不符")
    if now_h > target.cut_h:
        blocks.append(f"{target_sailing} 已过截装 {target.cut_h}h,不能新装")
    left = ledger.remaining(target_sailing)
    if left < pieces:
        blocks.append(f"{target_sailing} 余舱 {left} 件 < 需要 {pieces} 件")
    prob = joint_prob_ready(
        forecasts, lots, target.cut_h, groups={lot: target_sailing for lot in lots}
    )
    if prob < min_ready_prob:
        manual.append(
            f"换到 {target_sailing} 联合准时 {prob:.0%} 低于 {min_ready_prob:.0%},"
            f"差额需人工确认"
        )

    # 同一船班上其他订单的成套:挪走本单不得让别人缺件
    for view in ledger.active.values():
        if view.order == order or view.sailing != current.sailing:
            continue
        other_lines = ds.order_lines(view.order)
        if any(line.bundle == "整套" for line in other_lines):
            notes.append(
                f"{current.sailing} 上另有 {view.order} 成套发运,挪舱后其余舱"
                f" {left + pieces} 件需重新核对"
            )

    # 最后一个可行班次的保护:判断原舱是不是当前唯一可行
    from .plan import feasible_sailings

    alternatives = [
        sailing.sailing
        for sailing, reason in feasible_sailings(ds, ledger, order, pieces, now_h)
        if not reason and sailing.sailing != current.sailing
    ]
    last_feasible = not alternatives
    if last_feasible:
        notes.append(
            f"{current.sailing} 是当前最后一个可行班次,撤改被接受前原舱必须保留"
        )

    if blocks:
        return ReplanVerdict(
            order, target_sailing, "保持原状", True, fees,
            blocks=tuple(blocks), manual_confirm=tuple(manual), notes=tuple(notes),
        )
    # 一律持证申请:原舱保留到撤改被接受
    return ReplanVerdict(
        order, target_sailing, "持证申请撤改", True, fees,
        manual_confirm=tuple(manual),
        notes=tuple(
            [f"原舱 {current.booking}({current.sailing})保留,撤改 {target_sailing} "
             f"被接受才换,被拒则原舱不动"] + notes
        ),
    )


def evaluate_evidence(
    ds: Dataset,
    ledger: Ledger,
    forecasts: dict[str, LotForecast],
    lot: str,
    now_h: float,
) -> list[ReplanVerdict]:
    """快到取消截止时收到新质检证据:对每个持有该分包预订的订单,
    判断现有班次是否还赶得上,赶不上则对每个候选班次出换舱判定。"""
    verdicts: list[ReplanVerdict] = []
    for view in list(ledger.active.values()):
        if lot not in view.lots:
            continue
        order = view.order
        lines = ds.order_lines(order)
        lots = [line.lot for line in lines]
        sailing = ledger.sailings[view.sailing]
        prob = joint_prob_ready(
            forecasts, lots, sailing.cut_h, groups={l: sailing.sailing for l in lots}
        )
        if prob >= 0.5:
            verdicts.append(ReplanVerdict(
                order, None, "保持原状", True, {},
                notes=(f"新证据下 {sailing.sailing} 联合准时 {prob:.0%},无需换舱",),
            ))
            continue
        # 赶不上:对每个候选班次出判定
        for candidate in ds.sailings:
            if candidate.sailing == view.sailing:
                continue
            if candidate.destination != sailing.destination:
                continue
            if now_h > candidate.depart_h:
                continue
            verdicts.append(
                evaluate_replan(ds, ledger, forecasts, order, candidate.sailing, now_h)
            )
    return verdicts
