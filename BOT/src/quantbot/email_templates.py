"""每日邮件提醒的固定文本，可直接手动编辑。

当天该策略没有任何需要执行的操作时，邮件使用下面的标题和正文；
有操作时，邮件正文是该用户当天 Telegram 日报的完整内容，标题使用 ACTION_SUBJECT。

可用的占位符（写在花括号里，发送时自动替换）：
  {date}      交易日，例如 2026-11-03
  {strategy}  策略编号，例如 1 或 2
修改后重启 Bot 生效。占位符以外不要使用单独的花括号 { }，否则会报错；确实需要时写成 {{ }}。
"""

# 当天没有操作时
NO_ACTION_SUBJECT = "策略 {strategy} · {date} · 今日无需操作"
NO_ACTION_BODY = """策略 {strategy} 每日盘前检查（{date}）

今天不需要任何操作，持仓保持不变。

如需查看当前趋势与目标配置，可在 Telegram 机器人中发送 /check{strategy}。
"""

# 当天有操作时（正文自动使用 Telegram 日报内容）
ACTION_SUBJECT = "策略 {strategy} · {date} · 今日有操作，请查看"
