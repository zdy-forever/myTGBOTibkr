# 策略目录

- `strategy1/`：月度定投个股(加强型 QQQ)。共享行情与 SEC 财务数据，按用户持仓和个人候选池计算。
- `strategy2/`：20% SCHD底仓的G2原逐级ETF轮动。基础状态机来自 auto-quant/strategy2，当前配置见 `strategy2/DEPLOYED_RULES.json`，`sgs.live` 为只读盘前适配器。

从 BOT 目录安装：

```sh
pip install -e ../BACKEND/strategy1 -e ../BACKEND/strategy2 -e '.[dev]'
alembic upgrade head
```

两策略共用 `STRATEGY_PUSH_TIMEZONE` / `STRATEGY_PUSH_HOUR`；默认北京时间 10:00。
每个美股交易日检查，休市跳过，开盘后不补发盘前报告。S1 月度买入仍按用户日期；S2 每天计算趋势和操作。
账户、资金、权限、报告幂等与通知由 BOT 的 `daily_service.py` 管理。任何模块均不自动下单。
