"""排运测算:订单分批、分包进度、共享工位历史与货代舱位的统一测算。

模块划分:
- sources    数据接入与口径校验
- readiness  就绪预估(同一工位/同一运输批次关联, 区分返检中与完工未知)
- ledger     舱位台账(预订接受才占舱, 撤改未生效不放舱)
- reconcile  装箱与码头实装核实(箱号唯一, 码头实装为事实)
- plan       周会方案比较(装哪一班/等哪个分包/拆哪一票)
- replan     重排约束(不放掉最后一个可行班次, 取消截止, 已装车可撤回性)
- settlement 短装结算(是否补发/逾期责任/既成船费)
- requote    重新报价(加急与商议赔付对比, 全程可追溯)
- backtest   往季按发货日重跑(准时区间与紧张舱位下的误判)
- review     业务复盘(为什么没选上一班/实际配套量/风险变现)
"""

__all__ = [
    "sources",
    "readiness",
    "ledger",
    "reconcile",
    "plan",
    "replan",
    "settlement",
    "requote",
    "backtest",
    "review",
]
