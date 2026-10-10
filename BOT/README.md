# Telegram 多用户投资组合管理 Bot

这是一个 Python 3.11+、SQLite、SQLAlchemy 和 `python-telegram-bot` 的可运行 MVP。它提供多用户注册、模拟现金、持仓、定投、CSV 导入导出、操作审计、通知设置和管理员公告。

> 策略（Phase 2）已作为独立库 `../BACKEND/quant_strategy` 接入：每月一次共享行情分析，然后**逐个用户**按其自己的持仓和现金打分，只通知、不下单，买什么由用户自己决定。详见下方「策略接入」。

## 安全与数据模型

- `users`：UUID 内部主键、Telegram ID、带服务器密钥的手机号 HMAC 指纹、可选 Fernet 密文、角色。
- `positions`：按 `(user_id, symbol, exchange)` 唯一；股数 `Numeric(24,10)`，成本 `Numeric(20,8)`。
- `cash_accounts`：余额、币种、现金管理模式，和持仓分离。
- `investment_plans`：投入金额、周期、下次执行时间、启用状态。
- `transactions`：修改前后 JSON、操作者、时间、幂等键。
- `notification_preferences`：信号和公告开关、静默时段预留字段。
- `announcements` / `announcement_deliveries`：公告和逐用户投递结果。
- `strategy_runs` / `signals` / `user_signals`：策略集成表，用法见「策略接入」。

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
- `/plan` 命令已移除（没有入口创建或取消定投，后台的定投扫描任务也已停用）；`investment_plans` 表和 `PlanService` 代码保留。
- `/import` 后发送 CSV；`/export` 下载 CSV。导入在单一事务中先验证后写入。
- `/history`：最近 20 条账户操作；`/settings`：切换通知。
- `/check`（旧名 `/signals` 仍可用）：今天对股票池的评分前 10、你的持仓评分，以及按你当前持仓和现金算出的买卖建议（只算你自己的）。共享数据超过 12 小时没更新时，先显示旧结果，同时在后台更新一次，完成后自动把最新结果分别发给提问的人。
- `/cooldown`：看你自己的冷却期（建议卖出后 180 天内不买回等）。
- `/unbind`：用户自己解绑：确认后删除手机号绑定、持仓、现金、定投计划、操作记录和建议（不能撤销，建议先 `/export`），之后可以用 `/start` 重新绑定同一个或另一个手机号。只会删除按按钮的人自己的账号。
- `/help`：新手教程（已绑定的用户看到的快速上手没有绑定那一步；没绑定的人只看到怎么开始）。主题快捷命令 `/help_import`（CSV 导入格式）、`/help_faq`（常见问题）可以直接点击；Telegram 的命令名只能用英文字母、数字和下划线，所以不能用中文，旧写法 `/help 导入`、`/help 问题` 仍然有效。评分方法和规则细节不对用户公开，没有对应主题，`/check` 也只显示总分。
- 消息用 Telegram HTML 格式排版（粗体标题、等宽对齐的表格），所有动态内容都经过转义，测试会检查标签配对和转义。
- 管理员：`/announce`、`/users`、`/stats`、`/health`、`/runstrategy`（手动运行策略并推送建议）。权限每次在服务器白名单校验。

公告先预览确认，再创建逐用户投递记录并后台分批发送。处理 Telegram `RetryAfter`，封禁与 API 错误会记录到 delivery，最多尝试 5 次；唯一约束防止重复建立投递。

### CSV 格式

```csv
Company,Shares Held,Average Price
current_cash,0,1000 USD
ISRG,0.55,374.21 USD
```

`current_cash` 这一行的第二列固定写 0，导入时不读取它（写什么都行，不影响）；第三列是金额和币种。定投计划已不再提供入口。导入默认不覆盖同名持仓。

## 策略接入

```bash
.venv/bin/pip install -e ../BACKEND      # 策略库，先装
.venv/bin/pip install -e '.[dev]'
```

在 `.env` 里加（密钥只放在服务器，不进仓库）：

```bash
ALPACA_API_KEY=...            # 也认 ALPACA_PAPER1_API_KEY_ID / ALPACA_PAPER1_API_SECRET_KEY 这类名字；只读行情，策略库里没有任何下单接口
ALPACA_API_SECRET=...
SEC_USER_AGENT=Your Name you@example.com
STRATEGY_AUTORUN=1            # 可选：每月第一个工作日（纽约 10:00 后）自动运行；不设则只用 /runstrategy 手动运行
QUANT_STRATEGY_HOME=/opt/quantbot/app/data/strategy   # 部署时务必设在可写目录（systemd 里只有 data/ 和 backups/ 可写）：股票池与 SEC 缓存都写在这里
```

- **两种用法**：每月美股第一个交易日（Alpaca 日历确认）当天、本地时间 `STRATEGY_PUSH_HOUR` 点（默认 10:00，时区 `STRATEGY_PUSH_TIMEZONE`，默认 Asia/Shanghai 即 UTC+8，此时美股还没开盘）自动运行，并按每个用户各自的持仓和现金分别推送给开启了信号通知的用户（只推这一天，当天失败最多重试 3 次，每次间隔 1 小时；之后只能 `/runstrategy` 手动补发）；平时任何注册用户用 `/check` 随时查看。`/check` 触发的更新不会向别人推送，也不算每月推送。更新失败后 30 分钟内不会重复触发。
- **流程**：一次共享运行（行情、SEC 财报，不含任何用户数据）→ 存入 `strategy_runs`（`metadata_json` 是整份结果）→ 逐个用户、各自单独事务、只读该用户自己的 `positions` 和 `cash_accounts` 打分 → 写入 `user_signals` → 每人一条 Telegram 消息推送（受 `/settings signals` 开关控制；失败记录在 `notification_attempts` / `last_error`，最多重试 5 次）。
- **用户自己填持仓**：`/add`、`/import`、`/cash`。可投入金额就是该用户的现金余额，现金为 0 时不给买入金额，会提示先用 `/cash` 填写。只分析美元金额。
- **`signals` 表**：每次运行里每只出现在建议中的股票一行共享参考行（价格、P/V/F 原始分，`action` 固定 `HOLD`，表示共享层没有动作；买价区间字段留空，策略没有这类规则）。真正的买入/卖出在 `user_signals.action` 和 `recommendation` 里，按用户各自计算。
- **幂等**：`strategy_runs` 按（最后价格日, 策略版本）唯一，同一天已完成的运行不会重复；`user_signals` 按（用户, 股票）唯一。
- **耗时**：首次运行要下载股票池和 SEC 财报，约十几分钟，在后台线程里跑，不阻塞 Telegram。
- 仅供参考，不构成投资建议；策略参数来自对 2010–2025 年历史数据的反复研究，不是对未来的承诺。

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

- 策略库已接入，但 Telegram 推送和每月自动运行尚未在真实 bot token 下联调；静默时段字段仍是预留，发送时不检查。
- 用户卖出过的股票 180 天内不再被建议买回的规则，需要区分「卖出」和「改错删除」，暂未接入（库支持 `blocked` 参数）。
- 第一版定投仅增加模拟现金，不触发银行或券商操作。
- 当前 CSV 股票缺少交易所列，按 `US` 导入；Bot CRUD 支持显式交易所。未来可在兼容旧格式的前提下增加 Exchange 列。
- SQLite 适用于单机单进程部署；不要同时运行多个 Bot 进程。规模扩大后再评估 PostgreSQL。
- Telegram 会话状态在进程重启后丢失，但所有已确认业务写入持久化；定投与公告使用数据库状态恢复和去重。
- 公告失败记录已保留，但当前没有管理员命令手动重试指定公告。

## 阶段状态

- Phase 1：数据库、注册鉴权、用户隔离、持仓/现金 CRUD、CSV、定投与测试已完成。
- Phase 2：策略适配、每月运行、逐用户信号与推送已完成（见「策略接入」）。
- Phase 3：管理员公告、通知开关、结构化日志、systemd、备份恢复和部署文档已完成；`/runstrategy` 已可用。
