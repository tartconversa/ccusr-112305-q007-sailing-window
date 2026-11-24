"""数据接入:把 data/ 下的 JSON 表读成带类型的记录,并做口径校验。

口径要点(见 data/README.md):
- 历史 planned/actual 为从工序应开始计的天数,空 actual 代表截至可见周未知。
- 当前运单以排运日零时后的小时计;预计就绪不等于通过。
- 整套按所有规定分包都能随同一航班装齐计算;可拆单 min_gap_h 为两批发船时点最短间隔。
- 舱位 cap_pieces 以装进该箱型的实际件数计;箱型与目的港绑定,同港可合箱。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DATA = Path(__file__).resolve().parents[1] / "data"

# 就绪状态
DONE = "完工"
REWORK = "返检中"  # 返工尚未结束:有实时估计,区别于完工时间未知的旧单
PENDING_QC = "待出检"

# 货代回执状态
ACCEPTED = "接受"
REJECTED = "拒绝"
PENDING_REPLY = "待答复"

# 装箱记录种类
STUFFED = "入柜"  # 入柜不等于离港
RESENT = "重发报送"  # 换舱重复抄送同一箱,不是新增实物
TERMINAL_LOADED = "码头实装"  # 判断实际运输的事实


@dataclass(frozen=True)
class OrderLine:
    order: str
    bundle: str  # 整套 / 可拆
    lot: str
    pieces: int
    split_min_batch: int | None
    min_gap_h: int
    separate_sail: bool
    product: str
    destination: str
    penalty: float


@dataclass(frozen=True)
class Sailing:
    sailing: str
    destination: str
    cut_h: float  # 此后不能新装
    depart_h: float
    cap_pieces: int
    box: str
    cancel_h: float  # 取消免等待或换舱接受前的最后时点;超过仍可能接受但需计费
    cancel_fee: float
    emergency_fee: float


@dataclass(frozen=True)
class HistoryRow:
    lot: str
    order: str
    week: str
    process: str
    desk: str
    planned: float
    actual: float | None  # 空 = 截至可见周未知(右删失,不是零延误)
    inspection_result: str
    wait_hours: float
    available: str
    dispatch_group: str


@dataclass(frozen=True)
class Material:
    lot: str
    desk: str
    ready_state: str  # 完工 / 返检中 / 待出检
    inspected: str
    estimated_ready: float
    loaded: bool  # 已装车不可自行撤回,需货代解除或拒绝回执
    ready_at: float | None


@dataclass(frozen=True)
class CarrierEvent:
    event: str  # 预订 / 撤改 / 反馈
    booking: str
    order: str
    lots: tuple[str, ...]
    sailing: str
    hour: float
    state: str  # 接受 / 拒绝 / 待答复


@dataclass(frozen=True)
class LoadEvent:
    record: str
    box: str  # 实物箱唯一标识
    lot: str
    booking: str
    sailing: str
    kind: str  # 入柜 / 重发报送 / 码头实装
    hour: float
    pieces: int
    references: str | None


@dataclass(frozen=True)
class CustomerResponse:
    response: str
    order: str
    kind: str  # 部分接受 / 要求补发 / 可按分批兑现 / 逾期扣款确认
    time: float
    accepted_sets: int
    agreement: str
    max_hour: float
    debit_cny: float


@dataclass(frozen=True)
class DockCost:
    cost: str
    sailing: str
    box: str
    kind: str
    amount: float
    known_at: float
    actual: bool  # True = 已发生,不和预测补发成本相抵


@dataclass(frozen=True)
class Evidence:
    lot: str
    hour: float
    inspected: str
    new_ready: float
    loaded: bool


@dataclass
class Dataset:
    orders: list[OrderLine]
    sailings: list[Sailing]
    history: list[HistoryRow]
    materials: list[Material]
    carrier_events: list[CarrierEvent]
    load_events: list[LoadEvent]
    customer_responses: list[CustomerResponse]
    dock_costs: list[DockCost]
    evidence: list[Evidence]
    warnings: list[str] = field(default_factory=list)

    def order_lines(self, order: str) -> list[OrderLine]:
        return [line for line in self.orders if line.order == order]

    def order_names(self) -> list[str]:
        seen: dict[str, None] = {}
        for line in self.orders:
            seen.setdefault(line.order)
        return list(seen)

    def sailing(self, name: str) -> Sailing:
        return next(s for s in self.sailings if s.sailing == name)

    def material(self, lot: str) -> Material | None:
        return next((m for m in self.materials if m.lot == lot), None)

    def lot_pieces(self, lot: str) -> int:
        return sum(line.pieces for line in self.orders if line.lot == lot)


def _read_table(data_dir: Path, name: str) -> list[dict]:
    path = data_dir / f"{name}.json"
    with path.open(encoding="utf-8") as stream:
        records = json.load(stream)
    if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
        raise ValueError(f"{path.name} 应为 JSON 对象数组")
    return records


def load_dataset(data_dir: str | Path = DEFAULT_DATA) -> Dataset:
    """读取全部来源并做跨表口径校验;校验问题记入 warnings 而不是静默通过。"""
    data_dir = Path(data_dir)
    ds = Dataset(
        orders=[OrderLine(**r) for r in _read_table(data_dir, "orders")],
        sailings=[Sailing(**r) for r in _read_table(data_dir, "sailings")],
        history=[HistoryRow(**r) for r in _read_table(data_dir, "history")],
        materials=[Material(**r) for r in _read_table(data_dir, "materials")],
        carrier_events=[
            CarrierEvent(**{**r, "lots": tuple(r["lots"])})
            for r in _read_table(data_dir, "carrier_events")
        ],
        load_events=[LoadEvent(**r) for r in _read_table(data_dir, "load_events")],
        customer_responses=[
            CustomerResponse(**r) for r in _read_table(data_dir, "customer_responses")
        ],
        dock_costs=[DockCost(**r) for r in _read_table(data_dir, "dock_costs")],
        evidence=[Evidence(**r) for r in _read_table(data_dir, "new_evidence")],
    )
    _validate(ds)
    return ds


def append_carrier_event(data_dir: str | Path, event: CarrierEvent) -> None:
    """把排运员确认的预订/撤改回执写回台账,使后续测算核到真实占舱。"""
    data_dir = Path(data_dir)
    records = _read_table(data_dir, "carrier_events")
    records.append({
        "event": event.event,
        "booking": event.booking,
        "order": event.order,
        "lots": list(event.lots),
        "sailing": event.sailing,
        "hour": event.hour,
        "state": event.state,
    })
    path = data_dir / "carrier_events.json"
    with path.open("w", encoding="utf-8") as stream:
        json.dump(records, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def _validate(ds: Dataset) -> None:
    lots = {line.lot for line in ds.orders}
    sailings = {s.sailing for s in ds.sailings}
    for event in ds.carrier_events:
        unknown = set(event.lots) - lots
        if unknown:
            ds.warnings.append(f"回执 {event.booking} 引用未知分包 {sorted(unknown)}")
        if event.sailing not in sailings:
            ds.warnings.append(f"回执 {event.booking} 引用未知船班 {event.sailing}")
    for row in ds.load_events:
        if row.lot not in lots:
            ds.warnings.append(f"装箱记录 {row.record} 引用未知分包 {row.lot}")
    for line in ds.orders:
        if line.bundle == "可拆":
            if line.split_min_batch is None or line.split_min_batch <= 0:
                ds.warnings.append(f"{line.order}/{line.lot} 可拆但缺少最小批量")
        if line.bundle == "整套" and line.separate_sail:
            ds.warnings.append(f"{line.order}/{line.lot} 整套合同不允许分船")
    # 工厂报完工与码头实收不符在 reconcile 模块专门核对,这里只提示缺资料
    for m in ds.materials:
        if m.lot not in lots:
            ds.warnings.append(f"分包进度 {m.lot} 不在订单中")
