# Telegram 多用户投资组合管理 Bot

这是一个 Python 3.11+、SQLite、SQLAlchemy 和 `python-telegram-bot` 的可运行 MVP。它提供多用户注册、模拟现金、持仓、定投、CSV 导入导出、操作审计、通知设置和管理员公告。

> 策略 1（加强型 QQQ）已作为独立库 `../BACKEND/strategy1/quant_strategy` 接入：按用户选择的每月交易日推送，同一天共用行情分析，然后**逐个用户**按其自己的持仓和现金打分，只通知、不下单，买什么由用户自己决定。详见下方「策略接入」。

## 安全与数据模型

- `users`：UUID 内部主键、Telegram ID、带服务器密钥的手机号 HMAC 指纹、角色；不保存完整手机号。
- `phone_whitelist`、`premium_subscriptions`、`subscription_payments`：免费授权名单、订阅到期时间和幂等付款记录。
- `admin_grants`、`admin_phone_grants`、`admin_bootstrap`：按 ID 或手机号指纹授权的管理员名单，以及一次性导入旧配置的标记；Bot Owner 的 ID 只保存在服务器配置中。
- `positions`：按 `(user_id, symbol, exchange)` 唯一；股数 `Numeric(24,10)`，成本 `Numeric(20,8)`。
- `cash_accounts`：余额、币种、现金管理模式，和持仓分离。
- `investment_plans`：投入金额、周期、下次执行时间、启用状态。
- `transactions`：修改前后 JSON、操作者、时间、幂等键。
- `notification_preferences`：信号和公告开关、静默时段预留字段。
- `announcements` / `announcement_deliveries`：公告和逐用户投递结果。
- `strategy_runs` / `signals` / `user_signals`：策略集成表，用法见「策略接入」。

SQLite 启用 foreign keys、WAL 和 `synchronous=NORMAL`。新绑定只保存手机号 HMAC 指纹；旧版可选的加密手机号字段不会在本次升级中自动删除，以免破坏现有数据。手机号已绑定另一个 Telegram ID 时拒绝自动重绑，避免号码回收或盗号导致接管；恢复需管理员线下核验后执行专门迁移。

## 本地运行

```bash
python3.11 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env
# 编辑 .env；生成 PHONE_HMAC_SECRET（新绑定不保存可逆手机号）
.venv/bin/alembic upgrade head
.venv/bin/pytest -q
.venv/bin/quant-bot
```

Bot 使用 Long Polling，无需域名、Webhook 或反向代理。启动前必须通过 Alembic 迁移，应用不会擅自修改生产 schema。

## 命令与交互

- `/start` 在私聊要求本人分享 Telegram Contact，验证 `user_id`、有效号码并标准化 E.164 后才注册；未注册时每次发送都会重新提示，已注册不重复建档。随后依次检查管理员白名单、付费订阅；均不满足时仅可使用注册、隐私、订阅、留言和解绑相关命令。
- `/subscribe`：验证手机号后开通 400 Telegram Stars / 30 天的自动续订；付款成功才激活。`/privacy` 查看隐私说明。
- 管理员用 `/whitelist_add +(国家区号) (手机号) [备注]`、`/whitelist_edit +(国家区号) (手机号) 备注`、`/whitelist_remove +(国家区号) (手机号)` 管理免费 Premium 名单；`/whitelist_list [页码]` 每页查看 10 项，`/whitelist_show +(国家区号) (手机号)` 精确查询。可先单独发送命令，再在下一条消息输入参数；输入错误会提示重试，`/cancel` 取消。列表仅显示指纹摘要，不存完整号码；变更立即影响已绑定用户。管理员也必须先完成本人手机号验证。
- 管理员 `/announce` 发布普通公告（尊重公告开关与 Premium 权限）；`/important_announce` 经预览确认后向所有已验证用户发送并逐一置顶，不受普通公告开关或 Premium 状态影响。下一条重要公告成功置顶后，会撤下该用户聊天里的上一条重要公告置顶；普通公告和用户自行置顶的消息不受影响。发送、置顶及失败状态保存在数据库，管理员会收到完成统计。
- `/portfolio`、`/add`、`/edit`、`/remove`：在私聊查看及修改本人的持仓。持仓较多时 `/portfolio` 分页显示，可点击翻页按钮或发送 `/portfolio 2`；修改显示前后值，删除和修改均需按钮确认。
- `/cash`：现金余额和币种。
- `/plan` 命令已移除（没有入口创建或取消定投，后台的定投扫描任务也已停用）；`investment_plans` 表和 `PlanService` 代码保留。
- `/import` 后发送 CSV；`/export` 下载 CSV。导入在单一事务中先验证后写入。
- `/history`：最近 20 条账户操作；`/settings`：切换通知。
- `/strategy`：已验证且有 Premium 的用户查看已启用策略；`/strategy1` 启用策略 1（加强型 QQQ），`/strategy1_off` 停用，`/strategy2` 仅显示“研发中”。一个用户可以同时启用多个策略，选择保存在 SQLite 的 `user_strategies` 表。新用户开通 Premium 后须先启用策略；旧用户已选择的策略 1 会保留。未开通 Premium 的用户不能使用这些命令。
- `/chat`：已绑定用户给服务器白名单中的管理员留言，管理员可从通知按钮回复。留言和回复最多 2000 字，在数据库中保存 14 天后自动删除；管理员通知发送失败会每小时重试、每位管理员最多 5 次。Bot 重启后，14 天内尚存在的留言仍可回复；用户解绑时相关留言立即级联删除。
- `/auto_notice_date 5`：将自己的每月自动通知设为第 5 个美股交易日，默认第 1 个。只接受 1–23 的整数；`/auto_notice_date random` 每月稳定地随机选一个真实交易日，切换当月只从剩余交易日中选择；无参数查看设置。本月、下个月会显示能否生效及日期。固定模式还会核实未来 12 个月中交易日不足的月份。已过日期不补发，已经处理过的当月不重复通知。设置不影响其他用户，也不自动打开通知开关。
- `/check`（旧名 `/signals` 仍可用）：显示买入股票池排名前 10、自己的持仓排名，以及个人买卖建议。更新时间显示北京时间；持仓显示占比按股票市值计算，不含现金。锁表示暂不符合买入条件，不等于冷却期。池外持仓补充行情与财报后参与个人评分和卖出判断，不加入买入池；数据不足则标明无法评分。操作建议只显示买卖方向、股票、金额和估算股数，不附排名、策略类型或额外候选列表。共享数据超过 12 小时、策略版本改变或新增持仓尚未分析时，先显示旧结果，再后台更新（受重试间隔限制）。
- `/cooldown`：查看自己当前冷却中的股票，不显示期限或到期日期。用 `/edit` 减少股数或 `/remove` 删除持仓后，系统询问是否把卖出的股票加入冷却期（默认设置：加入）；只是改错可选「不加入」。以确认持仓更新的时间记录卖出，不从策略建议生成时间起算。
- `/unbind`：用户自己解绑：确认后删除手机号绑定、持仓、现金、定投计划、操作记录和建议（不能撤销，建议先 `/export`），之后可以用 `/start` 重新绑定同一个或另一个手机号。确认按钮只对生成时的账户有效；重新注册后旧按钮会失效。
- `/help`：按当前权限显示可用命令；未绑定者只看到绑定方法，已绑定但未开通者只看到订阅等入口。主题快捷命令 `/help_import`（CSV 导入格式）、`/help_faq`（常见问题）可以直接点击；Telegram 的命令名只能用英文字母、数字和下划线，所以不能用中文，旧写法 `/help 导入`、`/help 问题` 仍然有效。评分方法和规则细节不对用户公开，没有对应主题，`/check` 只展示排名，不展示分数。
- 消息用 Telegram HTML 格式排版（粗体标题、等宽对齐的表格），所有动态内容都经过转义，测试会检查标签配对和转义。
- 管理员：`/admin_help` 查看完整说明书；`/admin_list`、`/admin_show (用户ID)`、`/admin_add (用户ID) [备注]`、`/admin_edit (用户ID) 备注`、`/admin_remove (用户ID)` 管理普通管理员。四条增删查改命令也接受 `+(国家区号) (手机号)`；需要参数的命令支持先发命令、再发参数，`/cancel` 取消。手机号只保存 HMAC 指纹，尚未绑定时授权待生效；本人验证后绑定到该 Telegram ID，换号持有人不会自动继承。按 ID 与手机号授权相互独立。另可用 `/announce`、`/important_announce`、`/users`、`/stats`、`/health`、`/runstrategy`。普通管理员不能增删改 Bot Owner，Owner 也必须先验证手机号。权限每次从 SQLite 名单校验，撤销后立即生效。

`BOT_OWNER_TELEGRAM_ID` 必须设置为 Owner 本人的数字 Telegram ID，否则 Bot 拒绝启动。首次升级后启动时，旧 `MAINTAINER_TELEGRAM_IDS` 会一次性导入 SQLite；之后修改此环境变量不会覆盖通过命令做的增删。管理员变更及备注随 SQLite 备份持久保存。新加的管理员必须自己完成 `/start` 手机号验证后才可使用管理命令。

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
.venv/bin/pip install -e ../BACKEND/strategy1      # 策略库，先装；目录迁移后需重新安装
.venv/bin/pip install -e '.[dev]'
```

在 `.env` 里加（密钥只放在服务器，不进仓库）：

```bash
ALPACA_API_KEY=...            # 也认 ALPACA_PAPER1_API_KEY_ID / ALPACA_PAPER1_API_SECRET_KEY 这类名字；只读行情，策略库里没有任何下单接口
ALPACA_API_SECRET=...
SEC_USER_AGENT=Your Name you@example.com
STRATEGY_AUTORUN=1            # 可选：按各用户选择的每月美股交易日自动运行；不设则只用 /runstrategy 手动运行
QUANT_STRATEGY_HOME=/opt/quantbot/app/data/strategy   # 部署时务必设在可写目录（systemd 里只有 data/ 和 backups/ 可写）：股票池与 SEC 缓存都写在这里
```

- **两种用法**：用户选定的每月第 N 个美股交易日（默认第 1 个，Alpaca 日历确认）当天、本地时间 `STRATEGY_PUSH_HOUR` 点后（默认 10:00，时区 `STRATEGY_PUSH_TIMEZONE`，默认 Asia/Shanghai）自动运行，只为当天到期且开启信号通知的用户生成建议。同日共享行情，每个用户独立记录月份完成状态和运行尝试，当天最多尝试 3 次、间隔至少 1 小时；消息发送失败由独立任务每小时重试，总计最多 5 次，重启后继续。交易日历不可用时不猜测、不发送，之后重新查询。平时任何注册用户用 `/check` 随时查看，更新不会向别人推送，也不算每月推送。管理员 `/runstrategy` 仍可主动向所有订阅用户推送，并记入各用户当月完成状态。
- **数据库升级**：个人提醒日期、随机模式、白名单备注、重要公告置顶状态和多策略启用记录由 Alembic 迁移持久保存。现有安装更新代码后，启动前需在 `BOT/` 目录执行 `.venv/bin/alembic upgrade head`（使用部署实际的虚拟环境路径；最新迁移为 `0011_multi_strategy`）。旧用户此前的策略 1 选择会迁入新表；新用户需主动启用策略。持仓、订阅状态和已完成月份保留。
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
- 冷却期依据用户通过 `/edit`、`/remove` 记录的卖出及其选择，不连接券商核验成交。旧建议和未带卖出标记的历史修改不会追溯触发冷却期；选择存入操作记录，重启后保留。
- 第一版定投仅增加模拟现金，不触发银行或券商操作。
- 当前 CSV 股票缺少交易所列，按 `US` 导入；Bot CRUD 支持显式交易所。未来可在兼容旧格式的前提下增加 Exchange 列。
- SQLite 适用于单机单进程部署；不要同时运行多个 Bot 进程。规模扩大后再评估 PostgreSQL。
- Telegram 会话状态在进程重启后丢失，但所有已确认业务写入持久化；定投与公告使用数据库状态恢复和去重。
- 公告失败记录已保留，但当前没有管理员命令手动重试指定公告。

## 阶段状态

- Phase 1：数据库、注册鉴权、用户隔离、持仓/现金 CRUD、CSV、定投与测试已完成。
- Phase 2：策略适配、每月运行、逐用户信号与推送已完成（见「策略接入」）。
- Phase 3：管理员公告、通知开关、结构化日志、systemd、备份恢复和部署文档已完成；`/runstrategy` 已可用。
