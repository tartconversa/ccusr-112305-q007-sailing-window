"""周会方案比较:装哪一班、等哪个分包、拆哪一票。

每个方案给出:
- 联合准时概率(同一工位/同一运输批次关联,不把每包当独立);
- 约束分包(等哪个分包)与不可行原因;
- 四种费用情景:缺货赔付、候补用途、改舱费、加急运输;
- 决定后接下来几班真正能用的舱位。

费用口径:
- 缺货赔付 = 订单赔付额 × 本班及所有后续可行班次都赶不上的联合概率;
- 候补 = 舱位不足时的候补方案,按给定兑现概率折算赔付暴露(假设参数,需人工确认);
- 改舱费 = 已订班次过了 cancel_h 再挪舱,货代仍可能接受但需计费;
- 加急 = 付 emergency_fee,把有效截装从 cut_h 延到 depart_h(晚到急装)。
"""

from __future__ import annotations

from dataclasses import dataclass

from .ledger import Ledger
from .readiness import LotForecast, independent_prob_ready, joint_prob_ready
from .sources import Dataset, OrderLine, Sailing

# 候补兑现概率:假设参数,周会上需人工确认
DEFAULT_WAITLIST_CLEARANCE = 0.5


@dataclass(frozen=True)
class CostBreakdown:
    penalty: float = 0.0  # 缺货赔付期望
    waitlist: float = 0.0  # 候补用途(未兑现部分的赔付暴露)
    rebook: float = 0.0  # 改舱费
    emergency: float = 0.0  # 加急运输

    @property
    def total(self) -> float:
        return self.penalty + self.waitlist + self.rebook + self.emergency


@dataclass(frozen=True)
class Option:
    order: str
    label: str
    assignments: tuple[tuple[str, str], ...]  # (lot, sailing)
    ready_prob: float  # 联合(相关)
    ready_prob_independent: float  # 对照:独立假设
    binding_lot: str | None  # 等哪个分包
    feasible: bool
    reasons: tuple[str, ...]
    cost: CostBreakdown
    projected_remaining: tuple[tuple[str, int], ...] = ()  # 决定后各班余舱


def _lines(ds: Dataset, order: str) -> list[OrderLine]:
    return ds.order_lines(order)


def feasible_sailings(
    ds: Dataset,
    ledger: Ledger,
    order: str,
    pieces: int,
    now_h: float,
    emergency: bool = False,
) -> list[tuple[Sailing, str]]:
    """当前可占舱的班次(目的港、截装、余舱);emergency 时截装延到 depart_h。"""
    destination = ds.order_lines(order)[0].destination
    out: list[tuple[Sailing, str]] = []
    for sailing in ds.sailings:
        if sailing.destination != destination:
            continue
        deadline = sailing.depart_h if emergency else sailing.cut_h
        if now_h > deadline:
            continue
        left = ledger.remaining(sailing.sailing)
        if left < pieces:
            out.append((sailing, f"余舱 {left} 件 < {pieces} 件"))
            continue
        out.append((sailing, ""))
    return out


def _miss_chain(
    ds: Dataset,
    ledger: Ledger,
    forecasts: dict[str, LotForecast],
    lines: list[OrderLine],
    chosen: Sailing,
    now_h: float,
) -> float:
    """选中班及所有后续可行班次都赶不上的联合概率。"""
    lots = [line.lot for line in lines]
    miss = 1.0 - joint_prob_ready(
        forecasts, lots, chosen.cut_h, groups={lot: chosen.sailing for lot in lots}
    )
    pieces = sum(line.pieces for line in lines)
    for sailing in ds.sailings:
        if sailing.destination != chosen.destination:
            continue
        if sailing.depart_h <= chosen.depart_h:
            continue
        if now_h > sailing.cut_h:
            continue
        if ledger.remaining(sailing.sailing) < pieces:
            continue
        miss *= 1.0 - joint_prob_ready(
            forecasts, lots, sailing.cut_h, groups={lot: sailing.sailing for lot in lots}
        )
    return miss


def _rebook_fee(ds: Dataset, ledger: Ledger, order: str, target: str, now_h: float) -> float:
    """从已订班次挪到 target 的改舱费:原班过了 cancel_h 才计。"""
    current = ledger.order_booking(order)
    if current is None or current.sailing == target:
        return 0.0
    sailing = ledger.sailings[current.sailing]
    return sailing.cancel_fee if now_h > sailing.cancel_h else 0.0


def _whole_set_options(
    ds: Dataset,
    ledger: Ledger,
    forecasts: dict[str, LotForecast],
    order: str,
    now_h: float,
    waitlist_clearance: float,
) -> list[Option]:
    lines = _lines(ds, order)
    lots = [line.lot for line in lines]
    pieces = sum(line.pieces for line in lines)
    penalty = lines[0].penalty
    current = ledger.order_booking(order)
    options: list[Option] = []
    for sailing in ds.sailings:
        if sailing.destination != lines[0].destination:
            continue
        if now_h > sailing.depart_h:
            continue
        groups = {lot: sailing.sailing for lot in lots}
        prob = joint_prob_ready(forecasts, lots, sailing.cut_h, groups=groups)
        prob_indep = independent_prob_ready(forecasts, lots, sailing.cut_h)
        binding = min(lots, key=lambda lot: forecasts[lot].prob_ready_by(sailing.cut_h))
        reasons: list[str] = []
        emergency = 0.0
        if now_h > sailing.cut_h:
            # 已过截装:只能加急晚到
            emergency = sailing.emergency_fee
            prob = joint_prob_ready(forecasts, lots, sailing.depart_h, groups=groups)
            prob_indep = independent_prob_ready(forecasts, lots, sailing.depart_h)
            reasons.append(
                f"已过截装 {sailing.cut_h}h,加急晚到有效截装 {sailing.depart_h}h"
            )
        # 本单在该班的已订舱位对自己可用
        own = current.pieces if current and current.sailing == sailing.sailing else 0
        left = ledger.remaining(sailing.sailing) + own
        waitlist = 0.0
        if left < pieces:
            # 候补情景:按兑现概率折算
            waitlist = penalty * (1.0 - waitlist_clearance) * prob
            reasons.append(
                f"余舱 {left} 件 < {pieces} 件,只能候补(兑现概率 "
                f"{waitlist_clearance:.0%} 为假设,需人工确认)"
            )
            label = f"候补 {sailing.sailing}"
        elif own:
            label = f"留 {sailing.sailing}(已订 {current.booking})"
        else:
            label = f"加急 {sailing.sailing}" if emergency else f"装 {sailing.sailing}"
        miss = _miss_chain(ds, ledger, forecasts, lines, sailing, now_h)
        rebook = _rebook_fee(ds, ledger, order, sailing.sailing, now_h)
        cost = CostBreakdown(
            penalty=penalty * miss,
            waitlist=waitlist,
            rebook=rebook,
            emergency=emergency,
        )
        if prob < 0.5:
            reasons.append(f"联合准时 {prob:.0%} 偏低,约束分包 {binding}")
        if rebook:
            reasons.append(f"挪离已订班次已过取消截止,改舱费 {rebook:.0f}")
        projected = ledger.remaining_table()
        if left >= pieces:
            projected[sailing.sailing] = left - pieces
        options.append(
            Option(
                order=order,
                label=label,
                assignments=tuple((lot, sailing.sailing) for lot in lots),
                ready_prob=prob,
                ready_prob_independent=prob_indep,
                binding_lot=binding,
                feasible=True,
                reasons=tuple(reasons),
                cost=cost,
                projected_remaining=tuple(sorted(projected.items())),
            )
        )
    return options


def _split_options(
    ds: Dataset,
    ledger: Ledger,
    forecasts: dict[str, LotForecast],
    order: str,
    now_h: float,
) -> list[Option]:
    """可拆单:分批保住最小批量、批次间隔与非同船限制。"""
    lines = _lines(ds, order)
    penalty = lines[0].penalty
    min_batch = min(line.split_min_batch or line.pieces for line in lines)
    min_gap = max(line.min_gap_h for line in lines)
    separate = any(line.separate_sail for line in lines)
    options: list[Option] = []
    sailings = [
        s for s in ds.sailings
        if s.destination == lines[0].destination and now_h <= s.cut_h
    ]
    for line in lines:
        if line.pieces < min_batch:
            options.append(
                Option(
                    order=order,
                    label=f"拆 {line.lot}",
                    assignments=(),
                    ready_prob=0.0,
                    ready_prob_independent=0.0,
                    binding_lot=line.lot,
                    feasible=False,
                    reasons=(
                        f"{line.lot} {line.pieces} 件 < 最小批量 {min_batch} 件,不能单独成批",
                    ),
                    cost=CostBreakdown(penalty=penalty),
                )
            )
    # 逐分包选班:枚举每包一个班次,检查间隔与非同船
    assignments: list[tuple[tuple[str, str], ...]] = [()]
    for line in lines:
        assignments = [
            prefix + ((line.lot, s.sailing),)
            for prefix in assignments
            for s in sailings
        ]
    for assignment in assignments:
        reasons = []
        feasible = True
        used: dict[str, int] = {}
        for lot, sailing_name in assignment:
            used[sailing_name] = used.get(sailing_name, 0) + ds.lot_pieces(lot)
        if separate and len({s for _, s in assignment}) < len(assignment):
            feasible = False
            reasons.append("违反非同船限制:分批必须分船")
        departs = sorted(
            ledger.sailings[s].depart_h for _, s in assignment
        )
        for earlier, later in zip(departs,departs[1:]):
            if later - earlier < min_gap:
                feasible = False
                reasons.append(
                    f"两批发船间隔 {later - earlier:.0f}h < 约定 {min_gap}h"
                )
        for sailing_name, need in used.items():
            left = ledger.remaining(sailing_name)
            if left < need:
                feasible = False
                reasons.append(f"{sailing_name} 余舱 {left} 件 < {need} 件")
        # 同一班次的分包按同批联合,跨班相乘
        prob = 1.0
        prob_indep = 1.0
        by_sailing: dict[str, list[str]] = {}
        for lot, sailing_name in assignment:
            by_sailing.setdefault(sailing_name, []).append(lot)
        for sailing_name, sailing_lots in by_sailing.items():
            sailing = ledger.sailings[sailing_name]
            groups = {lot: sailing_name for lot in sailing_lots}
            prob *= joint_prob_ready(
                forecasts, sailing_lots, sailing.cut_h, groups=groups
            )
            for lot in sailing_lots:
                prob_indep *= forecasts[lot].prob_ready_by(sailing.cut_h)
        binding = min(
            (lot for lot, _ in assignment),
            key=lambda lot: forecasts[lot].prob_ready_by(
                ledger.sailings[dict(assignment)[lot]].cut_h
            ),
        )
        miss = 1.0 - prob
        label = "拆:" + ",".join(f"{lot}→{s}" for lot, s in assignment)
        options.append(
            Option(
                order=order,
                label=label,
                assignments=assignment,
                ready_prob=prob,
                ready_prob_independent=prob_indep,
                binding_lot=binding,
                feasible=feasible,
                reasons=tuple(reasons),
                cost=CostBreakdown(penalty=penalty * miss),
            )
        )
    # 周会阅读顺序:可行在前,费用低的在前
    options.sort(key=lambda o: (not o.feasible, o.cost.total, o.label))
    return options


def check_booking_constraints(
    ds: Dataset, ledger: Ledger, order: str, lots: tuple[str, ...], sailing_name: str
) -> list[str]:
    """排运员确认预订前的合同约束校验;返回违例说明,空列表表示可订。"""
    lines = ds.order_lines(order)
    if not lines:
        return [f"未知订单 {order}"]
    sailing = ledger.sailings[sailing_name]
    problems: list[str] = []
    if sailing.destination != lines[0].destination:
        problems.append(
            f"{sailing_name} 目的港 {sailing.destination} 与订单 {lines[0].destination} 不符"
        )
    order_lots = {line.lot for line in lines}
    unknown = set(lots) - order_lots
    if unknown:
        problems.append(f"分包 {sorted(unknown)} 不属于 {order}")
    if lines[0].bundle == "整套":
        # 合同只允许整套发运:预订必须覆盖全部必要配套件
        if set(lots) != order_lots:
            problems.append(
                f"整套合同必须全部分包同船:缺 {sorted(order_lots - set(lots))}"
            )
    else:
        min_batch = min(line.split_min_batch or line.pieces for line in lines)
        min_gap = max(line.min_gap_h for line in lines)
        separate = any(line.separate_sail for line in lines)
        pieces = sum(ds.lot_pieces(lot) for lot in lots)
        if pieces < min_batch:
            problems.append(f"本批 {pieces} 件 < 最小批量 {min_batch} 件")
        for view in ledger.active.values():
            if view.order != order:
                continue
            other = ledger.sailings[view.sailing]
            if separate and view.sailing == sailing_name:
                problems.append("违反非同船限制:该班已有本单批次")
            gap = abs(sailing.depart_h - other.depart_h)
            if view.sailing != sailing_name and gap < min_gap:
                problems.append(
                    f"与 {view.sailing} 批次间隔 {gap:.0f}h < 约定 {min_gap}h"
                )
    return problems


def weekly_options(
    ds: Dataset,
    ledger: Ledger,
    forecasts: dict[str, LotForecast],
    now_h: float,
    waitlist_clearance: float = DEFAULT_WAITLIST_CLEARANCE,
) -> list[Option]:
    options: list[Option] = []
    for order in ds.order_names():
        lines = _lines(ds, order)
        if lines[0].bundle == "整套":
            options.extend(
                _whole_set_options(ds, ledger, forecasts, order, now_h, waitlist_clearance)
            )
        else:
            options.extend(_split_options(ds, ledger, forecasts, order, now_h))
    return options


def format_weekly(
    ds: Dataset, ledger: Ledger, options: list[Option], now_h: float
) -> str:
    """周会材料:逐票方案对比与决定后真正能用的舱位。"""
    out = [f"== 周会排运方案(排运日零时后 {now_h}h)=="]
    for order in ds.order_names():
        lines = ds.order_lines(order)
        bundle = lines[0].bundle
        pieces = sum(line.pieces for line in lines)
        out.append(f"\n[{order}] {bundle} {pieces} 件 → {lines[0].destination}"
                   f"(赔付 {lines[0].penalty:.0f})")
        if bundle == "整套":
            out.append("  合同只允许整套发运,不能拆票;所有必要配套件同时就绪为准")
        current = ledger.order_booking(order)
        if current:
            out.append(f"  已订:{current.booking} → {current.sailing}(生效占舱)")
        for opt in [o for o in options if o.order == order]:
            mark = "可行" if opt.feasible else "不可行"
            out.append(
                f"  - {opt.label} [{mark}] 联合准时 {opt.ready_prob:.0%}"
                f"(独立假设 {opt.ready_prob_independent:.0%} 仅对照)"
            )
            if opt.binding_lot:
                out.append(f"      等分包:{opt.binding_lot}")
            out.append(
                f"      费用:赔付 {opt.cost.penalty:.0f} 候补 {opt.cost.waitlist:.0f}"
                f" 改舱 {opt.cost.rebook:.0f} 加急 {opt.cost.emergency:.0f}"
                f" 合计 {opt.cost.total:.0f}"
            )
            for reason in opt.reasons:
                out.append(f"      ! {reason}")
    out.append("\n== 决定后接下来几班真正能用的舱位 ==")
    used = ledger.occupancy()
    for name, sailing in sorted(
        ((n, ledger.sailings[n]) for n in ledger.sailings),
        key=lambda item: item[1].depart_h,
    ):
        left = sailing.cap_pieces - used.get(name, 0)
        out.append(
            f"  {name} → {sailing.destination} 截 {sailing.cut_h}h 发 {sailing.depart_h}h"
            f" 余 {left}/{sailing.cap_pieces} 件"
        )
    return "\n".join(out)
