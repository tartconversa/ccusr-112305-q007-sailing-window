"""接货核实:按实际装箱与码头装船分别核实,以约定计量粒度判断可兑现量。

口径:
- 箱号是实物箱唯一标识;码头实装是判断实际运输的事实,入柜和重复报送均不等于离港。
- 换舱可能重复抄送同一箱(重发报送):若对应预订已被拒绝,该抄送作废,
  认哪条船的装载以实际码头凭据为准,不能恢复已失效的预订。
- 整套订单按成套计量:离港可成套数与仍可补发的欠件分别说清;
  多余的半套不能当成可满足另一个客户的整套。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .ledger import Ledger
from .sources import (
    RESENT,
    STUFFED,
    TERMINAL_LOADED,
    LoadEvent,
    Material,
    OrderLine,
)


@dataclass(frozen=True)
class BoxAccount:
    box: str
    lot: str
    stuffed_pieces: int  # 入柜件数
    departed: bool
    sailing: str | None  # 码头凭据认定的船班
    departed_pieces: int
    short_load: int  # 入柜但未随船离港
    void_reports: tuple[str, ...]  # 作废的重复报送记录号
    conflicts: tuple[str, ...]


@dataclass(frozen=True)
class LotFulfillment:
    lot: str
    required: int
    shipped: int  # 码头实装合计
    short: int  # 欠件 = 仍可补发
    surplus: int  # 已离港但配不上套的半套余量,不能转给别的客户


@dataclass(frozen=True)
class OrderFulfillment:
    order: str
    granularity: str  # 整套 / 可拆
    sets_target: int | None  # 整套订单的约定套数
    departed_sets: int | None  # 已离港的可成套数
    lots: tuple[LotFulfillment, ...]
    batches: tuple[tuple[str, str, int], ...]  # (lot, sailing, pieces) 分批实况


@dataclass
class Reconciliation:
    boxes: list[BoxAccount]
    orders: dict[str, OrderFulfillment]
    warnings: list[str] = field(default_factory=list)

    def order(self, name: str) -> OrderFulfillment:
        return self.orders[name]


def reconcile(
    load_events: list[LoadEvent],
    ledger: Ledger,
    order_lines: list[OrderLine],
    materials: list[Material] | None = None,
) -> Reconciliation:
    warnings: list[str] = []
    by_box: dict[str, list[LoadEvent]] = {}
    for event in load_events:
        by_box.setdefault(event.box, []).append(event)

    boxes: list[BoxAccount] = []
    for box, rows in sorted(by_box.items()):
        rows.sort(key=lambda r: r.hour)
        stuffed = [r for r in rows if r.kind == STUFFED]
        resent = [r for r in rows if r.kind == RESENT]
        terminal = [r for r in rows if r.kind == TERMINAL_LOADED]
        void: list[str] = []
        conflicts: list[str] = []
        for copy in resent:
            # 重复抄送不新增实物;对应预订已失效则抄送作废
            if copy.booking in ledger.dead:
                void.append(copy.record)
                warnings.append(
                    f"{copy.record} 箱 {box} 随已失效预订 {copy.booking} 抄送到 "
                    f"{copy.sailing},作废;以码头凭据为准"
                )
            else:
                warnings.append(
                    f"{copy.record} 箱 {box} 重复抄送({copy.sailing}),不重复计件"
                )
        if len({t.sailing for t in terminal}) > 1:
            conflicts.append(
                f"箱 {box} 在多条船有码头实装记录,需人工核对码头凭据"
            )
        fact = terminal[0] if terminal else None
        if fact is not None and fact.booking in ledger.dead:
            conflicts.append(
                f"箱 {box} 码头实装挂在已失效预订 {fact.booking},需人工核对"
            )
        stuffed_pieces = stuffed[-1].pieces if stuffed else 0
        departed_pieces = fact.pieces if fact else 0
        boxes.append(
            BoxAccount(
                box=box,
                lot=rows[0].lot,
                stuffed_pieces=stuffed_pieces,
                departed=fact is not None,
                sailing=fact.sailing if fact else None,
                departed_pieces=departed_pieces,
                short_load=max(0, stuffed_pieces - departed_pieces),
                void_reports=tuple(void),
                conflicts=tuple(conflicts),
            )
        )

    shipped_by_lot: dict[str, int] = {}
    lot_sailing: dict[str, str] = {}
    for account in boxes:
        if account.departed:
            shipped_by_lot[account.lot] = (
                shipped_by_lot.get(account.lot, 0) + account.departed_pieces
            )
            lot_sailing[account.lot] = account.sailing or ""

    orders: dict[str, OrderFulfillment] = {}
    for name in dict.fromkeys(line.order for line in order_lines):
        lines = [line for line in order_lines if line.order == name]
        if lines[0].bundle == "整套":
            sets_target = min(line.pieces for line in lines)
            raw: list[tuple[OrderLine, int, float]] = []
            departed_sets: int | None = None
            for line in lines:
                shipped = shipped_by_lot.get(line.lot, 0)
                per_set = line.pieces / sets_target
                sets_here = int(shipped // per_set)
                departed_sets = (
                    sets_here if departed_sets is None else min(departed_sets, sets_here)
                )
                raw.append((line, shipped, per_set))
            lots = tuple(
                LotFulfillment(
                    lot=line.lot,
                    required=line.pieces,
                    shipped=shipped,
                    short=max(0, line.pieces - shipped),
                    # 已离港但配不上套的半套余量,不能转给别的客户
                    surplus=shipped - int((departed_sets or 0) * per_set),
                )
                for line, shipped, per_set in raw
            )
            orders[name] = OrderFulfillment(
                order=name,
                granularity="整套",
                sets_target=sets_target,
                departed_sets=departed_sets,
                lots=lots,
                batches=(),
            )
        else:
            lots = tuple(
                LotFulfillment(
                    lot=line.lot,
                    required=line.pieces,
                    shipped=shipped_by_lot.get(line.lot, 0),
                    short=max(0, line.pieces - shipped_by_lot.get(line.lot, 0)),
                    surplus=0,
                )
                for line in lines
            )
            batches = tuple(
                (line.lot, lot_sailing.get(line.lot, ""), shipped_by_lot.get(line.lot, 0))
                for line in lines
                if shipped_by_lot.get(line.lot, 0) > 0
            )
            orders[name] = OrderFulfillment(
                order=name,
                granularity="可拆",
                sets_target=None,
                departed_sets=None,
                lots=lots,
                batches=batches,
            )

    # 工厂报完工/已装车与码头实收不符的提示
    for material in materials or []:
        shipped = shipped_by_lot.get(material.lot, 0)
        required = sum(line.pieces for line in order_lines if line.lot == material.lot)
        if material.ready_state == "完工" and required and shipped < required:
            warnings.append(
                f"{material.lot} 工厂报完工 {required} 件,码头实装 {shipped} 件,"
                f"差 {required - shipped} 件未到码头"
            )

    return Reconciliation(boxes=boxes, orders=orders, warnings=warnings)
