"""就绪预估:用共享工位的历史完工与返工经历估计分包就绪时间。

两条口径要求:
1. 返工尚未结束(返检中)要区别于完工时间未知的旧单——前者有实时估计,
   后者是历史里的右删失记录(actual 为空),只能作为尾部证据,不能当作零延误。
2. 同一工位或同一运输批次的分包延误相关,联合准时概率不能把每包当成相互独立。

模型:每个工位从历史票(同一订单的三道工序)拟合完工超出比例
(completion-planned)/planned 的均值与标准差;经历过返检(需返检)的票单独
拟合返工子样本。当前分包的估计按状态取不同子样本,联合概率按工位/批次聚类,
对公共冲击做高斯求积,而不是把边际概率直接相乘。
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

from .sources import DONE, PENDING_QC, REWORK, Dataset, Evidence, HistoryRow, Material

# 公共冲击载荷:同一工位的分包共享约一半延误来源,同一运输批次共享约三成。
RHO_DESK = 0.5
RHO_GROUP = 0.3

# 高斯求积点数(标准正态分位点等权)
_QUAD_N = 24
_STD_NORMAL = statistics.NormalDist()
_QUAD_NODES = [_STD_NORMAL.inv_cdf((k + 0.5) / _QUAD_N) for k in range(_QUAD_N)]

# 无进度资料的分包按完工时间未知处理:给一个宽分布并在结果里标明状态。
UNKNOWN_MU_H = 72.0
UNKNOWN_SIGMA_H = 36.0

# 完工超出比例标准差的下限:约等于全局历史分散度,
# 防止小样本工位给出过窄的准时区间
MIN_RATIO_STD = 0.3


def _std(values: list[float]) -> float:
    return statistics.pstdev(values) if len(values) > 1 else 0.0


@dataclass(frozen=True)
class DeskStats:
    """单个工位的历史完工超出比例。"""

    ratio_mean: float
    ratio_std: float
    rework_mean: float
    rework_std: float
    tickets: int
    censored: int  # 完工时间未知的旧单数(右删失)


@dataclass
class DeskModel:
    """按工位拟合的完工延误模型。"""

    stats: dict[str, DeskStats]
    global_stats: DeskStats

    @classmethod
    def fit(cls, history: list[HistoryRow], before_week: str | None = None) -> "DeskModel":
        """拟合工位模型;before_week 给定则只用该周之前的记录(供往季重跑)。"""
        rows = [r for r in history if before_week is None or r.week < before_week]
        tickets: dict[str, list[HistoryRow]] = {}
        for row in rows:
            tickets.setdefault(row.order, []).append(row)
        overall: list[float] = []
        rework: list[float] = []
        per_desk: dict[str, dict[str, list[float] | int]] = {}
        for ticket_rows in tickets.values():
            desk = ticket_rows[0].desk
            planned = max(r.planned for r in ticket_rows)
            known = [r.actual for r in ticket_rows if r.actual is not None]
            entry = per_desk.setdefault(
                desk, {"ratio": [], "rework": [], "censored": 0}
            )
            final = max(ticket_rows, key=lambda r: r.planned)
            if final.actual is None:
                # 完工时间未知的旧单:末道工序没有实际值,只作删失证据,不进均值
                entry["censored"] = int(entry["censored"]) + 1
                continue
            completion = max(known)
            if planned <= 0:
                continue
            ratio = (completion - planned) / planned
            entry["ratio"].append(ratio)  # type: ignore[union-attr]
            overall.append(ratio)
            if any(r.inspection_result == "需返检" for r in ticket_rows):
                entry["rework"].append(ratio)  # type: ignore[union-attr]
                rework.append(ratio)
        stats = {}
        for desk, entry in per_desk.items():
            ratios: list[float] = entry["ratio"]  # type: ignore[assignment]
            rew: list[float] = entry["rework"]  # type: ignore[assignment]
            stats[desk] = DeskStats(
                ratio_mean=statistics.fmean(ratios) if ratios else 0.0,
                ratio_std=max(_std(ratios), MIN_RATIO_STD) if ratios else 0.0,
                rework_mean=statistics.fmean(rew) if rew else (
                    statistics.fmean(ratios) if ratios else 0.0
                ),
                rework_std=max(
                    _std(rew) if rew else _std(ratios), MIN_RATIO_STD
                ),
                tickets=len(ratios),
                censored=int(entry["censored"]),
            )
        global_stats = DeskStats(
            ratio_mean=statistics.fmean(overall) if overall else 0.0,
            ratio_std=max(_std(overall), MIN_RATIO_STD),
            rework_mean=statistics.fmean(rework) if rework else (
                statistics.fmean(overall) if overall else 0.0
            ),
            rework_std=max(
                _std(rework) if rework else _std(overall), MIN_RATIO_STD
            ),
            tickets=len(overall),
            censored=0,
        )
        return cls(stats=stats, global_stats=global_stats)

    def desk(self, desk: str) -> DeskStats:
        """工位样本不足时退回全局统计,避免小样本过拟合。"""
        stats = self.stats.get(desk)
        if stats is None or stats.tickets < 2:
            return self.global_stats
        return stats


# 分包预测状态
STATE_DONE = DONE
STATE_REWORK = REWORK  # 返工尚未结束
STATE_PENDING_QC = PENDING_QC
STATE_UNKNOWN = "完工未知"  # 完工时间未知(无进度资料)


@dataclass(frozen=True)
class LotForecast:
    lot: str
    desk: str | None
    state: str
    mu: float  # 预计就绪时点(排运日零时后小时)
    sigma: float
    basis: str  # 估计依据,供复盘追溯

    def prob_ready_by(self, hour: float) -> float:
        if self.sigma <= 0:
            return 1.0 if hour >= self.mu else 0.0
        return _STD_NORMAL.cdf((hour - self.mu) / self.sigma)

    def interval(self, level: float = 0.8) -> tuple[float, float]:
        """准时区间:给定置信水平的就绪时点区间。"""
        if self.sigma <= 0:
            return (self.mu, self.mu)
        tail = (1.0 - level) / 2.0
        dist = statistics.NormalDist(self.mu, self.sigma)
        return (dist.inv_cdf(tail), dist.inv_cdf(1.0 - tail))


def _latest_evidence(
    lot: str, evidence: list[Evidence], now_h: float
) -> Evidence | None:
    seen = [e for e in evidence if e.lot == lot and e.hour <= now_h]
    return max(seen, key=lambda e: e.hour) if seen else None


def forecast_lots(
    materials: list[Material],
    evidence: list[Evidence],
    model: DeskModel,
    now_h: float,
    lots: list[str] | None = None,
) -> dict[str, LotForecast]:
    """按当前进度与质检证据给每个分包一个就绪预测。

    - 完工:时点确定。
    - 返检中(返工尚未结束):用返工子样本放大实时估计。
    - 待出检:用整体完工子样本,并按该工位删失比例放宽尾部。
    - 质检证据通过:以证据时点为准,残余不确定性很小。
    - 无资料:完工时间未知,宽分布并标明。
    """
    wanted = lots or [m.lot for m in materials]
    by_lot = {m.lot: m for m in materials}
    forecasts: dict[str, LotForecast] = {}
    for lot in wanted:
        material = by_lot.get(lot)
        ev = _latest_evidence(lot, evidence, now_h)
        if material is None:
            forecasts[lot] = LotForecast(
                lot, None, STATE_UNKNOWN, UNKNOWN_MU_H, UNKNOWN_SIGMA_H,
                "无进度资料,按完工时间未知处理",
            )
            continue
        desk_stats = model.desk(material.desk)
        if material.ready_state == DONE and material.ready_at is not None:
            forecasts[lot] = LotForecast(
                lot, material.desk, STATE_DONE, float(material.ready_at), 0.0,
                f"已完工于 {material.ready_at}h",
            )
            continue
        if ev is not None and ev.inspected == "通过":
            forecasts[lot] = LotForecast(
                lot, material.desk, material.ready_state, float(ev.new_ready),
                max(1.0, 0.05 * ev.new_ready),
                f"{ev.hour}h 质检通过,证据就绪 {ev.new_ready}h",
            )
            continue
        if material.ready_state == REWORK or (ev is not None and ev.inspected == "需返检"):
            base = ev.new_ready if ev is not None else material.estimated_ready
            mu = base * (1.0 + desk_stats.rework_mean)
            sigma = base * max(desk_stats.rework_std, 0.1)
            src = f"{ev.hour}h 证据" if ev is not None else "分包进度"
            forecasts[lot] = LotForecast(
                lot, material.desk, STATE_REWORK, mu, sigma,
                f"返工未结束,{src}估计 {base}h ×(1+返工超出 {desk_stats.rework_mean:.0%})",
            )
            continue
        # 待出检:生产基本完成,剩余是检验与可能返工;按删失比例放宽尾部
        base = material.estimated_ready
        mu = base * (1.0 + 0.5 * desk_stats.ratio_mean)
        censored_rate = (
            desk_stats.censored / (desk_stats.tickets + desk_stats.censored)
            if (desk_stats.tickets + desk_stats.censored) > 0
            else 0.0
        )
        sigma = base * max(desk_stats.ratio_std, 0.1) * (1.0 + 0.5 * censored_rate)
        forecasts[lot] = LotForecast(
            lot, material.desk, STATE_PENDING_QC, mu, sigma,
            f"待出检,估计 {base}h ×(1+完工超出 {0.5 * desk_stats.ratio_mean:.0%}),"
            f"删失票 {desk_stats.censored} 张放宽尾部",
        )
    return forecasts


def _clusters(lots: list[str], forecasts: dict[str, LotForecast],
              groups: dict[str, str] | None) -> list[list[str]]:
    """按同一工位/同一运输批次把分包并成相关簇。"""
    parent = {lot: lot for lot in lots}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        parent[find(a)] = find(b)

    for i, a in enumerate(lots):
        for b in lots[i + 1:]:
            fa, fb = forecasts[a], forecasts[b]
            same_desk = fa.desk is not None and fa.desk == fb.desk
            same_group = (
                groups is not None
                and groups.get(a) is not None
                and groups.get(a) == groups.get(b)
            )
            if same_desk or same_group:
                union(a, b)
    out: dict[str, list[str]] = {}
    for lot in lots:
        out.setdefault(find(lot), []).append(lot)
    return list(out.values())


def joint_prob_ready(
    forecasts: dict[str, LotForecast],
    lots: list[str],
    hour: float,
    groups: dict[str, str] | None = None,
) -> float:
    """全部分包在 hour 前就绪的联合概率。

    同一工位(RHO_DESK)或同一运输批次(RHO_GROUP)的分包共享一个公共冲击,
    对公共冲击求积;簇间独立。这比边际概率直接相乘更保守,
    反映一道返检同时推迟几包的情形。就绪时间确定的分包先折算成常数因子,
    不进求积。
    """
    if not lots:
        return 1.0
    prob = 1.0
    stochastic: list[str] = []
    for lot in lots:
        fc = forecasts[lot]
        if fc.sigma <= 0:
            if hour < fc.mu:
                return 0.0
        else:
            stochastic.append(lot)
    for cluster in _clusters(stochastic, forecasts, groups):
        if len(cluster) == 1:
            prob *= forecasts[cluster[0]].prob_ready_by(hour)
            continue
        desks = {forecasts[lot].desk for lot in cluster}
        rho = RHO_DESK if len(desks) == 1 and None not in desks else RHO_GROUP
        root = math.sqrt(rho)
        rest = math.sqrt(1.0 - rho)
        acc = 0.0
        for node in _QUAD_NODES:
            term = 1.0
            for lot in cluster:
                fc = forecasts[lot]
                # 条件分布: ready = mu + sigma*(sqrt(rho)*Z + sqrt(1-rho)*eps)
                cond_mu = fc.mu + fc.sigma * root * node
                cond_sigma = fc.sigma * rest
                term *= _STD_NORMAL.cdf((hour - cond_mu) / cond_sigma)
            acc += term
        prob *= acc / _QUAD_N
    return prob


def independent_prob_ready(
    forecasts: dict[str, LotForecast], lots: list[str], hour: float
) -> float:
    """对照用:把每包准时概率当成相互独立的乘积(不作为决策依据)。"""
    prob = 1.0
    for lot in lots:
        prob *= forecasts[lot].prob_ready_by(hour)
    return prob


def fit_from_dataset(ds: Dataset, before_week: str | None = None) -> DeskModel:
    return DeskModel.fit(ds.history, before_week=before_week)
