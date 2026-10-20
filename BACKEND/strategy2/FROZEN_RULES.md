# 当前规则冻结：G2

2026-10-20，用户决定保留当前实际默认规则，停止继续优化候选。本次没有调整策略参数或资产配置。

| 状态 | 冻结规则 |
| --- | --- |
| 强上涨 | GPIQ最低目标20%；TQQQ按现有波动率控制分配，其余沿用原CASCADE降风险规则 |
| 普通上涨 | 保留原配置；当前ALL_UP将确认上涨归入强上涨 |
| 震荡 | GPIQ，保留整股取整、费用和现金预留 |
| 下跌/强下跌 | GPIQ目标20%＋SGOV目标80% |

上涨进入比率1.035、退出比率1.01、下跌比率0.97，现有确认天数、波动率控制、5%调仓带和$13现金预留均保持当前配置。信号使用昨日及以前数据。完整配置与源码快照哈希见[FROZEN_RULES.json](FROZEN_RULES.json)。最低20%是目标权重，成交后实际权重会受整股、价格变化和费用影响。

GLD/IEF轮动、取消GPIQ底仓、释放底仓转SGOV，以及QQQT/QQQY/QLDY/QDTE等收入基金替换均保留为研究候选，未纳入默认规则。先前的`FROZEN_RESEARCH_CANDIDATE.json`仅冻结观察候选，不表示默认策略已采用该候选。

执行口径保留本轮审计修正：历史名义整股价格与拆股、分红应收与派息日到账、首日TWR、波动率预热。日线开盘成交假设及其他限制见[执行审计](research/execution_audit/SUMMARY.md)。本次不运行锁定窗口，不连接账户或执行交易。

[分状态结果](research/execution_audit/STATES.md) · [收入ETF比较](research/income_etf_results/SUMMARY.md) · [防守轮动研究](research/defensive_results/SUMMARY.md)

源码哈希用于追溯快照，不新增运行时哈希检查。后续策略变更须有新的明确决策，不自动将研究排名升级为默认策略。
