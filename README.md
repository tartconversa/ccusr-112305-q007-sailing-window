# 季节性货运资料与排运测算

这里收录了工序历史、当前分包、可用船班和货代回执。记录为本地合成数据。每个 JSON 文件是来自同一来源的一组记录,字段含义与业务单位见 [资料口径](data/README.md)。

使用 Python 3.11 和标准库。检查资料概要与关系时,从项目根目录运行下面两条命令。

~~~sh
python3 scripts/inspect_sources.py
python3 -m unittest discover -s tests
~~~

查看单一数据来源时,可向检查脚本传入 JSON 文件名去掉扩展名后的名称。

## 排运测算(shipping 包)

`shipping` 包把订单分批条款、分包进度、共享工位历史与货代舱位接入同一次测算:

- `readiness` 就绪预估:返检中(返工未结束)区别于完工时间未知的旧单;同一工位或同一运输批次的分包延误相关,联合准时概率不把每包当成相互独立。
- `ledger` 舱位台账:预订接受才占舱,撤改未生效不放舱,已失效预订不能恢复;排运员确认的预订占用真实箱位。
- `plan` 周会方案:装哪一班、等哪个分包、拆哪一票,按情景比较缺货赔付、候补、改舱费、加急运输,并给出决定后各班真正能用的舱位。
- `replan` 重排:持证申请(不放掉最后一个可行班次再试更优班次),取消截止计费,已装车可撤回性,同箱订单成套约束;保不住承诺时保留原状态并列出需人工确认的差额。
- `reconcile` 接货核实:以码头实装为事实,换舱重复抄送作废;整套按成套计量,已离港可成套数与仍可补发欠件分列,多余半套不转单。
- `settlement` 短装结算:是否补发、补了能否弥补逾期责任、既成船费不相抵,顺着同一次测算打通。
- `requote` 重新报价:加急与商议赔付对剩余舱位、已兑现数量及当前净成本的影响,每份提议可追到剩余套数、最后可行班次和既成回单。
- `backtest` 往季重跑:按发货日重跑已知结果的往季,看逐票准时区间与紧张舱位下的误判。
- `review` 业务复盘:为什么上一班没选、客户实际收到的配套量、多少延误风险变成了事实。

命令(默认 `--data data`,`--now` 为排运日零时后小时,缺省取资料中最晚时点):

~~~sh
python3 -m shipping weekly --now 10        # 周会方案比较
python3 -m shipping replan O1 S3 --now 20  # 换舱判定
python3 -m shipping evidence C12 --now 29  # 新质检证据触发的判定
python3 -m shipping reconcile              # 接货核实
python3 -m shipping settle --now 47        # 短装结算
python3 -m shipping requote --now 47       # 重新报价
python3 -m shipping backtest --capacity 1  # 往季重跑(紧张舱位)
python3 -m shipping review --now 79        # 业务复盘
python3 -m shipping book O2 S1 B7 --lots C21 --now 12   # 确认预订,占真实箱位
python3 -m shipping modify B8 O1 S3 --now 20            # 登记撤改申请(待答复)
~~~

`book`/`modify` 会把回执写回 `data/carrier_events.json`;确认前校验整套同船、最小批量、批次间隔与非同船限制,占不了舱就不写回。
