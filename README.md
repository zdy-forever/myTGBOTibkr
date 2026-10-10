# Telegram 多用户投资组合管理 Bot

这是一个 Python 3.11+、SQLite、SQLAlchemy 和 `python-telegram-bot` 的可运行 MVP。它提供多用户注册、模拟现金、持仓、定投、CSV 导入导出、操作审计、通知设置和管理员公告。

> 当前仓库初始化时为空，未包含原量化项目、P/F/V 公式、阈值、买卖规则或回测代码。按项目方最新要求，本版本**不实现策略 Adapter、不运行每日策略、不生成信号**。数据库预留 `strategy_runs`、`signals`、`user_signals` 三张集成表；`/signals` 与 `/runstrategy` 会明确报告尚未接入，不会使用臆造规则。收到原策略代码后再做策略审计与 Phase 2。

## 安全与数据模型

- `users`：UUID 内部主键、Telegram ID、带服务器密钥的手机号 HMAC 指纹、可选 Fernet 密文、角色。
- `positions`：按 `(user_id, symbol, exchange)` 唯一；股数 `Numeric(24,10)`，成本 `Numeric(20,8)`。
- `cash_accounts`：余额、币种、现金管理模式，和持仓分离。
- `investment_plans`：投入金额、周期、下次执行时间、启用状态。
- `transactions`：修改前后 JSON、操作者、时间、幂等键。
- `notification_preferences`：信号和公告开关、静默时段预留字段。
- `announcements` / `announcement_deliveries`：公告和逐用户投递结果。
- `strategy_runs` / `signals` / `user_signals`：仅为未来量化集成契约。

SQLite 启用 foreign keys、WAL 和 `synchronous=NORMAL`。手机号默认不保存可恢复内容；`PHONE_ENCRYPTION_KEY` 留空时只保存 HMAC 指纹。手机号已绑定另一个 Telegram ID 时拒绝自动重绑，避免号码回收或盗号导致接管；恢复需管理员线下核验后执行专门迁移。

## 本地运行

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env
# 编辑 .env；生成 PHONE_HMAC_SECRET 和可选 PHONE_ENCRYPTION_KEY
.venv/bin/alembic upgrade head
.venv/bin/pytest -q
.venv/bin/quant-bot
```

Bot 使用 Long Polling，无需域名、Webhook 或反向代理。启动前必须通过 Alembic 迁移，应用不会擅自修改生产 schema。

## 命令与交互

- `/start` 验证 Telegram Contact 的 `user_id` 后注册；重复执行直接加载账户。
- `/portfolio`、`/add`、`/edit`、`/remove`：查看及修改本人的持仓。修改显示前后值，删除和修改均需按钮确认。
- `/cash`：现金余额和币种。
- `/plan`：每周、每月、每季度、每年模拟投入；后台每小时扫描。唯一幂等键防止重启后重复入账。
- `/import` 后发送 CSV；`/export` 下载 CSV。导入在单一事务中先验证后写入。
- `/history`：最近 20 条账户操作；`/settings`：切换通知。
- `/signals`：当前仅说明策略未接入。
- 管理员：`/announce`、`/users`、`/stats`、`/health`、`/runstrategy`。权限每次在服务器白名单校验。

公告先预览确认，再创建逐用户投递记录并后台分批发送。处理 Telegram `RetryAfter`，封禁与 API 错误会记录到 delivery，最多尝试 5 次；唯一约束防止重复建立投递。

### CSV 格式

```csv
Company,Shares Held,Average Price
current_cash,0,1000 USD
ISRG,0.55,374.21 USD
```

`current_cash` 的模式值：0 手动、1 每月、2 每周、3 每季度、4 每年。CSV 只能表达现金模式，不能完整表达定投金额和下次执行时间；这些信息由 `/plan` 和 `investment_plans` 保存。导入默认不覆盖同名持仓。

## Ubuntu 22.04/24.04 部署

```bash
sudo adduser --system --group --home /opt/quantbot quantbot
sudo mkdir -p /opt/quantbot
sudo chown -R quantbot:quantbot /opt/quantbot
sudo -u quantbot git clone YOUR_REPOSITORY_URL /opt/quantbot/app
sudo -u quantbot mkdir -p /opt/quantbot/app/data /opt/quantbot/app/backups
sudo -u quantbot python3.11 -m venv /opt/quantbot/app/.venv
sudo -u quantbot /opt/quantbot/app/.venv/bin/pip install -e '/opt/quantbot/app'
sudo -u quantbot cp /opt/quantbot/app/.env.example /opt/quantbot/app/.env
sudo -u quantbot editor /opt/quantbot/app/.env
sudo -u quantbot /opt/quantbot/app/.venv/bin/alembic -c /opt/quantbot/app/alembic.ini upgrade head
sudo cp /opt/quantbot/app/deploy/quantbot.service /etc/systemd/system/
sudo cp /opt/quantbot/app/deploy/quantbot-backup.service /etc/systemd/system/
sudo cp /opt/quantbot/app/deploy/quantbot-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now quantbot.service quantbot-backup.timer
sudo systemctl status quantbot.service
sudo journalctl -u quantbot.service -f
```

升级：

```bash
sudo systemctl stop quantbot
sudo -u quantbot /opt/quantbot/app/.venv/bin/quant-backup
sudo -u quantbot git -C /opt/quantbot/app pull --ff-only
sudo -u quantbot /opt/quantbot/app/.venv/bin/pip install -e '/opt/quantbot/app'
sudo -u quantbot /opt/quantbot/app/.venv/bin/alembic -c /opt/quantbot/app/alembic.ini upgrade head
sudo systemctl start quantbot
```

恢复（先停止 Bot，避免同时写入）：

```bash
sudo systemctl stop quantbot
sudo -u quantbot /opt/quantbot/app/.venv/bin/quant-backup --restore /opt/quantbot/app/backups/quantbot-YYYYMMDDTHHMMSSZ.db
sudo systemctl start quantbot
```

备份使用 SQLite Online Backup API，避免直接复制 WAL 数据库造成不一致；恢复前执行 `PRAGMA integrity_check` 并以原子替换落盘。

## 当前边界与风险

- 未提供量化策略，因此 Phase 2 未开始，P/F/V 与回测回归测试也无法编写。
- 第一版定投仅增加模拟现金，不触发银行或券商操作。
- 当前 CSV 股票缺少交易所列，按 `US` 导入；Bot CRUD 支持显式交易所。未来可在兼容旧格式的前提下增加 Exchange 列。
- SQLite 适用于单机单进程部署；不要同时运行多个 Bot 进程。规模扩大后再评估 PostgreSQL。
- Telegram 会话状态在进程重启后丢失，但所有已确认业务写入持久化；定投与公告使用数据库状态恢复和去重。
- 公告失败记录已保留，但当前没有管理员命令手动重试指定公告。

## 阶段状态

- Phase 1：数据库、注册鉴权、用户隔离、持仓/现金 CRUD、CSV、定投与测试已完成。
- Phase 2：按项目方要求暂缓；未实现策略适配、定时行情分析、信号生成或回测回归。
- Phase 3：管理员公告、通知开关、结构化日志、systemd、备份恢复和部署文档已完成；策略相关管理员运行与统计仍等待 Phase 2。
