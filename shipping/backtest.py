"""往季重跑:按发货日重跑已经知道结果的往季。

做法:对每个生产周,只用该周之前的历史拟合工位模型,给当周生产的票
(以发货周 available 为发货日)出准时区间;再与已知的实际完工对比。
紧张舱位情景:每个运输批次(dispatch_group)可发票数有限,
装不下的票被挤到下一周,用来观察紧张舱位下的误判。

删失口径与 readiness 一致:末道工序 actual 为空才算完工时间未知,
中间工序缺失但已装箱的票按已知完工处理。
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from .readiness import DeskModel
from .sources import HistoryRow

WEEK_DAYS = 7.0  # 生产周长度:完工不超过它才赶得上周末发船


@dataclass(frozen=True)
class TicketResult:
    order: str
    week: str  # 生产周
    ship_week: str  # 发货日(发货周)
    desk: str
    mu: float
    lo: float
    hi: float
    actual: float | None  # None = 完工时间未知的旧单(删失)
    covered: bool | None  # 实际是否落在准时区间内
    predicted_ready: bool
    actual_ready: bool | None
    bumped: bool  # 紧张舱位下被挤到下一周
    misjudgment_loose: bool | None  # 舱位宽松下的误判
    misjudgment_tight: bool | None  # 紧张舱位下的误判


@dataclass(frozen=True)
class BacktestReport:
    results: tuple[TicketResult, ...]
    capacity_per_group: int | None
    level: float
    coverage: float  # 已知结果中落入准时区间的比例
    misjudgments_loose: int
    misjudgments_tight: int
    censored: int


@dataclass
class _Ticket:
    order: str
    week: str
    ship_week: str
    group: str
    desk: str
    planned: float
    actual: float | None


def _tickets(history: list[HistoryRow]) -> list[_Ticket]:
    by_order: dict[str, list[HistoryRow]] = {}
    for row in history:
        by_order.setdefault(row.order, []).append(row)
    out = []
    for order, rows in by_order.items():
        final = max(rows, key=lambda r: r.planned)
        known = [r.actual for r in rows if r.actual is not None]
        actual = None if final.actual is None else max(known)
        out.append(_Ticket(
            order=order,
            week=rows[0].week,
            ship_week=rows[0].available,
            group=rows[0].dispatch_group,
            desk=rows[0].desk,
            planned=max(r.planned for r in rows),
            actual=actual,
        ))
    return sorted(out, key=lambda t: t.order)


def backtest(
    history: list[HistoryRow],
    level: float = 0.8,
    capacity_per_group: int | None = None,
) -> BacktestReport:
    tickets = _tickets(history)
    weeks = sorted({row.week for row in history})
    predicted: list[tuple[_Ticket, float, float, float]] = []  # ticket, mu, lo, hi
    for week in weeks[1:]:  # 第一周没有更早历史,无法拟合
        model = DeskModel.fit(history, before_week=week)
        for ticket in tickets:
            if ticket.week != week:
                continue
            stats = model.desk(ticket.desk)
            mu = ticket.planned * (1.0 + stats.ratio_mean)
            sigma = ticket.planned * max(stats.ratio_std, 0.1)
            dist = statistics.NormalDist(mu, sigma)
            tail = (1.0 - level) / 2.0
            predicted.append((
                ticket, mu, dist.inv_cdf(tail), dist.inv_cdf(1.0 - tail),
            ))

    # 紧张舱位:同一运输批次内只留 mu 最小的前 N 票,其余挤到下一周
    bumped: set[str] = set()
    if capacity_per_group is not None:
        by_group: dict[str, list[tuple[_Ticket, float, float, float]]] = {}
        for item in predicted:
            by_group.setdefault(item[0].group, []).append(item)
        for group in by_group.values():
            group.sort(key=lambda item: item[1])  # mu 小的先装
            for ticket, *_ in group[capacity_per_group:]:
                bumped.add(ticket.order)

    results: list[TicketResult] = []
    for ticket, mu, lo, hi in predicted:
        predicted_ready = mu <= WEEK_DAYS
        actual_ready = None if ticket.actual is None else ticket.actual <= WEEK_DAYS
        is_bumped = ticket.order in bumped
        covered = (
            None if ticket.actual is None else lo <= ticket.actual <= hi
        )
        mis_loose = (
            None if actual_ready is None else predicted_ready != actual_ready
        )
        plan_tight = predicted_ready and not is_bumped
        mis_tight = None if actual_ready is None else plan_tight != actual_ready
        results.append(TicketResult(
            order=ticket.order,
            week=ticket.week,
            ship_week=ticket.ship_week,
            desk=ticket.desk,
            mu=mu, lo=lo, hi=hi, actual=ticket.actual,
            covered=covered,
            predicted_ready=predicted_ready,
            actual_ready=actual_ready,
            bumped=is_bumped,
            misjudgment_loose=mis_loose,
            misjudgment_tight=mis_tight,
        ))

    known = [r for r in results if r.actual is not None]
    coverage = sum(1 for r in known if r.covered) / len(known) if known else 0.0
    return BacktestReport(
        results=tuple(results),
        capacity_per_group=capacity_per_group,
        level=level,
        coverage=coverage,
        misjudgments_loose=sum(1 for r in results if r.misjudgment_loose),
        misjudgments_tight=sum(1 for r in results if r.misjudgment_tight),
        censored=sum(1 for r in results if r.actual is None),
    )


def format_backtest(report: BacktestReport) -> str:
    cap = (
        f"每运输批次限 {report.capacity_per_group} 票"
        if report.capacity_per_group is not None
        else "舱位宽松"
    )
    out = [f"== 往季重跑(准时区间 {report.level:.0%},{cap})=="]
    for r in report.results:
        actual = f"{r.actual:.0f}d" if r.actual is not None else "完工未知"
        flags = []
        if r.bumped:
            flags.append("被挤班")
        if r.misjudgment_loose:
            flags.append("宽松误判")
        if r.misjudgment_tight:
            flags.append("紧张误判")
        out.append(
            f"  {r.order} 生产周 {r.week} 发货 {r.ship_week} 工位 {r.desk}"
            f" 预测 {r.mu:.1f}d 区间 [{r.lo:.1f},{r.hi:.1f}] 实际 {actual}"
            f" {' '.join(flags)}"
        )
    out.append(
        f"覆盖率 {report.coverage:.0%},宽松误判 {report.misjudgments_loose} 票,"
        f"紧张误判 {report.misjudgments_tight} 票,完工未知 {report.censored} 票"
    )
    return "\n".join(out)
