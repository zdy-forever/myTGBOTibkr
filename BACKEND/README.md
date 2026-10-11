# 策略目录

- [`strategy1/`](strategy1/)：月度定投个股(加强型 QQQ)，当前唯一开放的策略。包含独立的 `quant_strategy` Python 包、测试、文档和运行缓存。
- 策略 2 正在研发中，尚无可运行代码。

Bot 安装策略库时使用 `pip install -e ../BACKEND/strategy1`（从 `BOT/` 目录执行）。策略代码移动后，已有环境也需要重新安装该可编辑包。
