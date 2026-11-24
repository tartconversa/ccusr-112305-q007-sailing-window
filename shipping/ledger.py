"""舱位台账:货代回执驱动的真实箱位占用。

口径(见 data/README.md):
- 预订接受才正式占舱;待答复不占,拒绝不占。
- 收到撤改单不代表替代已生效:撤改被接受前,原预订继续占舱;
  撤改被拒绝,原预订不动;撤改被接受,原预订同时释放、新预订占舱。
- 已失效(拒绝)的预订不能恢复。
- cancel_h 之后仍可能由货代接受但需计费;cut_h 之后不能新装。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .sources import (
    ACCEPTED,
    PENDING_REPLY,
    REJECTED,
    CarrierEvent,
    OrderLine,
    Sailing,
)

# 回执事件种类
BOOK = "预订"
MODIFY = "撤改"
FEEDBACK = "反馈"


@dataclass(frozen=True)
class BookingView:
    booking: str
    order: str
    lots: tuple[str, ...]
    sailing: str
    state: str  # 生效中 / 待答复 / 已失效
    kind: str
    hour: float
    pieces: int


@dataclass
class Ledger:
    """按小时顺序应用回执后的舱位状态。"""

    sailings: dict[str, Sailing]
    order_lines: list[OrderLine]
    active: dict[str, BookingView] = field(default_factory=dict)  # 生效中
    pending: dict[str, BookingView] = field(default_factory=dict)  # 待答复
    dead: dict[str, BookingView] = field(default_factory=dict)  # 已失效
    log: list[str] = field(default_factory=list)

    @classmethod
    def build(
        cls,
        sailings: list[Sailing],
        order_lines: list[OrderLine],
        events: list[CarrierEvent],
        until_h: float | None = None,
    ) -> "Ledger":
        ledger = cls(
            sailings={s.sailing: s for s in sailings},
            order_lines=list(order_lines),
        )
        for event in sorted(events, key=lambda e: e.hour):
            if until_h is not None and event.hour > until_h:
                break
            ledger.apply(event)
        return ledger

    def _pieces(self, lots: tuple[str, ...]) -> int:
        return sum(
            line.pieces for line in self.order_lines if line.lot in set(lots)
        )

    def apply(self, event: CarrierEvent) -> None:
        if event.booking in self.dead:
            # 已失效的预订不能恢复
            self.log.append(
                f"{event.hour}h 忽略 {event.booking}:预订已失效,不能恢复"
            )
            return
        pieces = self._pieces(event.lots)
        view = BookingView(
            booking=event.booking,
            order=event.order,
            lots=event.lots,
            sailing=event.sailing,
            state="",
            kind=event.event,
            hour=event.hour,
            pieces=pieces,
        )
        if event.state == PENDING_REPLY:
            self.pending[event.booking] = BookingView(**{**view.__dict__, "state": "待答复"})
            self.log.append(f"{event.hour}h {event.booking} {event.event}待答复,原预订不动")
            return
        if event.state == REJECTED:
            self.pending.pop(event.booking, None)
            self.dead[event.booking] = BookingView(**{**view.__dict__, "state": "已失效"})
            self.log.append(
                f"{event.hour}h {event.booking} {event.event}被拒,原预订保持占舱"
            )
            return
        if event.state == ACCEPTED:
            self.pending.pop(event.booking, None)
            if event.event == MODIFY:
                # 撤改生效:释放同一订单这些分包的旧预订,再占新舱
                for key, old in list(self.active.items()):
                    if old.order == event.order and set(old.lots) & set(event.lots):
                        del self.active[key]
                        self.log.append(
                            f"{event.hour}h {event.booking} 撤改生效,释放 {old.booking}"
                            f"({old.sailing})"
                        )
            self.active[event.booking] = BookingView(**{**view.__dict__, "state": "生效中"})
            self.log.append(
                f"{event.hour}h {event.booking} {event.event}接受,占 {event.sailing} "
                f"{pieces} 件"
            )
            left = self.remaining(event.sailing)
            if left < 0:
                # 回执是事实,但超卖要在台账里显形
                self.log.append(
                    f"{event.hour}h 警告:{event.sailing} 超卖 {-left} 件,需与货代核对"
                )
            return
        raise ValueError(f"未知回执状态 {event.state}")

    # ---- 查询 ----

    def occupancy(self) -> dict[str, int]:
        used = {name: 0 for name in self.sailings}
        for view in self.active.values():
            used[view.sailing] = used.get(view.sailing, 0) + view.pieces
        return used

    def remaining(self, sailing: str) -> int:
        return self.sailings[sailing].cap_pieces - self.occupancy().get(sailing, 0)

    def remaining_table(self) -> dict[str, int]:
        return {name: self.remaining(name) for name in self.sailings}

    def order_booking(self, order: str) -> BookingView | None:
        """订单当前生效中的预订(每单同时至多一个)。"""
        for view in self.active.values():
            if view.order == order:
                return view
        return None

    def can_hold(self, sailing: str, pieces: int, at_h: float) -> tuple[bool, str]:
        """at_h 时点能否在 sailing 新占 pieces 件舱位。"""
        s = self.sailings[sailing]
        if at_h > s.cut_h:
            return False, f"{sailing} 已过截装 {s.cut_h}h,不能新装"
        left = self.remaining(sailing)
        if left < pieces:
            return False, f"{sailing} 余舱 {left} 件 < 需要 {pieces} 件"
        return True, ""

    def confirm(
        self, booking: str, order: str, lots: tuple[str, ...], sailing: str, hour: float
    ) -> CarrierEvent:
        """排运员确认的预订:登记一条接受回执,立即占用真实箱位。"""
        pieces = self._pieces(lots)
        ok, reason = self.can_hold(sailing, pieces, hour)
        if not ok:
            raise ValueError(f"不能确认 {booking}:{reason}")
        event = CarrierEvent(
            event=BOOK, booking=booking, order=order, lots=tuple(lots),
            sailing=sailing, hour=hour, state=ACCEPTED,
        )
        self.apply(event)
        return event

    def copy(self) -> "Ledger":
        import copy as _copy

        return _copy.deepcopy(self)
