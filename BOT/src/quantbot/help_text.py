"""Texts for /help, in Telegram HTML (bold headings, a copyable <pre> CSV example). Every topic stays well below Telegram's 4096-character limit.
Telegram only turns Latin letters, digits and underscores into tappable commands, so the topic shortcuts are /help_import and /help_faq."""
from __future__ import annotations

LINE = "━━━━━━━━━━━━━━"

QUICK_NEW = f"""📘 <b>这是什么</b>
这是一个美股投资组合助手：你记录持仓与现金，启用策略后查看买卖建议和每日盘前报告。

⚠️ <i>仅供参考，不构成投资建议；系统不会下单，也不连接你的券商账户，买卖由你自己决定。</i>

{LINE}
🔐 <b>使用前先绑定手机号</b>
1️⃣ 发 /start
2️⃣ 点输入框下方的按钮「📱 分享我的手机号」，在弹出的框里选「分享」
3️⃣ 授权后会检查免费白名单及付费订阅
4️⃣ 获得 Premium 后，发 /strategy 启用策略
5️⃣ 按策略欢迎页录入持仓和现金，再查看建议

<i>用手机版 Telegram；不能手动输入；号码只用来识别你本人，默认只保存不可逆的指纹。隐私说明：/privacy</i>"""

QUICK_PENDING = """✅ 手机号已验证，但尚未开通 Premium。
400 Telegram Stars / 30 天（约 US$7.99，实际价格因购买渠道而异）。

/subscribe　开通订阅，页面可联系管理员咨询优惠
/chat　给管理员留言
/start　查看当前权限
/privacy　查看手机号与付款隐私说明
/help　查看可用命令
/unbind　解绑并删除账户数据
/cancel　取消当前操作"""

STRATEGY_PICK = """✅ Premium 已开通。按三步开始：
1. 发送 /strategy1 或 /strategy2 启用策略，也可以同时启用。
2. 选择资金方式：CASH 1 200USD / CASH 2 300USD 分别指定金额；或 CASH 0 500USD 设置公共现金，再用 /strategy_weights 分配比例。
3. 用 /add 或 /import 记录持仓；没有持仓可跳过。/check 检查所有已启用策略；/check1 只检查 S1，/check2 只检查 S2。

/strategy 随时查看状态和下一步。/cancel 取消输入。
策略 1：月度定投个股(加强型 QQQ)，每日检查，月度买入。
策略 2：G2 趋势 ETF 轮动，每日说明趋势与操作。
仅生成建议，不会自动下单。"""

STRATEGY1_WELCOME = """📈 <b>策略 1：月度定投个股(加强型 QQQ)</b>

1. 先用 /add 或 /import 录入持仓；没有持仓可跳过。
2. 发 <code>CASH 1 200USD</code> 设置本策略现金；或 <code>CASH 0 1000USD</code> 设置公共现金，再 /strategy_weights 分配比例。
3. /strategy1_pool 查看股票池；回复 + AAPL 添加，或 - AAPL 移除。
4. /portfolio 核对记录，/check1 查看建议。

每个美股交易日盘前出报告，无需操作也会说明；月度买入按 /auto_notice_date 提醒。实际运行时间沿用系统盘前时间。
/settings 控制通知；/strategy 查看资金状态；/strategy1_off 停用；/cancel 取消输入。
成交后请更新持仓和现金。机器人不会自动下单。"""

STRATEGY2_WELCOME = """📈 <b>策略 2：G2 趋势 ETF 轮动</b>

上涨时SCHD最低目标15%，搭配动态TQQQ；降低进攻仓位时直接转向SCHD。
震荡、下跌及强下跌目标为20% SCHD＋80% SGOV。已有GPIQ只建议卖出转仓，不再买入。

1. /add 或 /import 录入现有 ETF 持仓；没有持仓可跳过。
2. 发 <code>CASH 2 300USD</code> 设置本策略现金；或 <code>CASH 0 1000USD</code> 设置公共现金，再 /strategy_weights 分配比例。
3. /portfolio 核对，/check2 查看当前趋势、目标配置与买卖建议。支持碎股，建议数量最多六位小数。

每个美股交易日与 S1 同时在盘前运行，使用最近已收盘数据。报告说明当前趋势和今日操作；无需操作也会出报告。
目标比例小幅变化不一定需要交易，请以「今日操作」为准；调仓按当前实际持仓与目标的差额判断。
/settings 控制通知；/strategy 查看资金状态；/strategy2_off 停用；/cancel 取消输入。
成交后请更新持仓和现金，系统不会自动同步成交或下单。"""

COMMON_HELP = """📘 <b>通用使用说明</b>
输入 / 打开快捷菜单。需要参数的命令可以先发送，再在下一条填写；/cancel 在输入过程中随时取消。

/check　依次检查所有已启用策略，结果分别发送
/strategy　策略管理入口：查看状态、启用、停用及分配权重
/strategy1　启用策略 1：月度定投个股(加强型 QQQ)
/strategy2　启用策略 2：G2 趋势 ETF 轮动
/cash　按提示设置独立策略现金，或公共现金（CASH 0）
/fx　查看或刷新自动AUD/USD汇率，无需手动填写
/strategy_weights　仅公共现金模式可用，单策略默认 100%；多策略比例总和须为 100%，最多两位小数
/subscribe　查看订阅状态或开通 Premium
/chat　给管理员留言，留言及回复保存 14 天后自动删除
/privacy　隐私说明
/unbind　解绑并删除账户数据，发送后点确认，无法撤销；已有投资数据建议先导出备份
/help　通用说明及所有已启用策略的专属说明
/help_faq　常见问题
/cancel　在输入过程中随时取消

以下按已启用策略分别说明。未启用的策略不显示操作说明；停用后对应说明也会移除。"""

AUD_HELP = ('AUD资金：直接填写CASH 1 200AUD、CASH 2 300AUD或CASH 0 1000AUD。'
            '系统自动获取AUD/USD汇率并在建议中显示来源和报价时间，正常缓存30秒；/fx可主动刷新，无需手动填汇率。'
            '外汇周末休市时标明最近报价；未配置行情源、请求失败或行情过期时暂停AUD资金建议。'
            '支持USD与AUD独立预算并存，按USD估值占比分配持仓计算份额。原币记录保留，建议仍以USD计价；'
            '这里只估算，不会自动换汇；未增加澳股选股功能。')

STRATEGY_PICK += '\n\n' + AUD_HELP
STRATEGY1_WELCOME += '\n\n' + AUD_HELP
STRATEGY2_WELCOME += '\n\n' + AUD_HELP

STRATEGY1_HELP = f"""📈 <b>策略 1：月度定投个股(加强型 QQQ) · 使用说明</b>

1️⃣ <b>录入你的持仓和现金</b>
/add 逐只添加　/import 导入 CSV
/cash 填写可以投入的现金
没有持仓可跳过 /add。/cash 填当前总余额，不含股票市值，会替换旧余额。建议先用 /portfolio 核对。

2️⃣ <b>随时查看建议</b>
/check1 最近可用行情的排名、总分与P/V/F/G/D/Q分项，以及按你的持仓和现金生成的建议

3️⃣ <b>每日盘前报告与月度买入</b>
每个美股交易日检查持仓，即使无需操作也会生成报告。
默认每月第一个美股交易日提醒，实际时间以 /auto_notice_date 的显示为准。需要 Premium 有效、策略已启用、权重有效、策略通知已开启且系统自动推送可用。用 /settings 查看通知开关。

4️⃣ <b>实际成交后更新记录</b>
买入新股票用 /add，已有股票数量变化用 /edit，全部卖出用 /remove；再用 /cash 更新剩余现金。机器人不会自动同步成交。

⚠️ <i>仅供参考，不构成投资建议；系统不会下单，也不连接你的券商账户，买卖由你自己决定。</i>

{LINE}
🧭 <b>全部命令</b>（点命令名就能发送）
输入 / 可打开快捷菜单。先发送命令，再按提示在下一条填写内容；/cancel 取消输入。命令后面的数字是参数，例如 /auto_notice_date 5 中的 5 表示第 5 个交易日，不是每月 5 号。

📊 <b>查看</b>（直接发送，不需要参数）
/strategy1_off　停用策略 1，保留持仓、现金与历史记录
/check1　最近可用行情的排名、总分与P/V/F/G/D/Q分项和你的买卖建议
/cooldown　暂不建议买入/卖出的股票
/portfolio　我的持仓和现金；较多时用按钮翻页，或发 <code>/portfolio 2</code> 查看第 2 页
/history　最近 20 条账户操作

✏️ <b>录入</b>
/add　添加持仓。发送后输入「代码 股数 平均成本 币种」，例如 <code>ISRG 0.55 374.21 USD</code>
/edit　修改持仓。发送后输入「代码 新股数 新平均成本」，例如 <code>ISRG 0.75 380</code>，再点确认
/remove　删除持仓。发送 <code>/remove ISRG</code>（换成你的股票代码），再点确认
/cash　修改现金。发送后输入金额，例如 <code>1000 USD</code>（<code>1000usd</code>、<code>$1000</code> 也行）

📁 <b>导入导出</b>
/import　发送后，再把 CSV 文件发给我（格式见 /help_import）
/export　直接发送，会收到一份 CSV 文件

⚙️ <b>设置</b>
本策略按所选资金方式计算建议。独立现金用 <code>CASH 1 200USD</code>；仅公共现金（CASH 0）可设置比例。
/strategy1_pool 查看自己的买入候选池；<code>/strategy1_pool + AAPL MSFT</code> 添加，<code>/strategy1_pool - AAPL MSFT</code> 移除。首次修改以当前默认池为基础。
/settings　查看通知设置。<code>/settings signals</code> 开/关已启用策略的每日报告与操作提醒，<code>/settings announcements</code> 开/关公告
/auto_notice_date　修改个人通知日期，例如 <code>/auto_notice_date 5</code> 表示第 5 个美股交易日；<code>/auto_notice_date random</code> 每月随机选一个交易日；不带参数查看设置及日期预览

📖 <b>更多说明</b>
/help_import　CSV 导入格式
/help_faq　常见问题"""

STRATEGY1_HELP += '\n\n' + AUD_HELP

STRATEGY_HELP_BLOCKS = {
    "strategy1": STRATEGY1_HELP,
    "strategy2": STRATEGY2_WELCOME + "\n\n/portfolio 查看持仓和现金；/add 添加；/edit 修改；/remove 删除。\n/import 与 /export 导入导出 CSV，格式见 /help_import；/history 查看操作记录。\n独立现金模式：持仓按各策略现金占总现金的比例划分计算份额；未启用策略的预算保留，不会转给其他策略。\n公共现金模式：持仓和现金按设置的权重划分。其他证券不纳入 S2 的 ETF 调仓。\n/help 查看通用说明；/help_faq 查看账户常见问题。",
}

STRATEGY_NAMES = {"strategy1": "策略 1：月度定投个股(加强型 QQQ)", "strategy2": "策略 2：G2 趋势 ETF 轮动"}
STRATEGY_WELCOME_BLOCKS = {"strategy1": STRATEGY1_WELCOME, "strategy2": STRATEGY2_WELCOME}
COMMON_FAQ = """❓ <b>账户常见问题</b>
首次使用请 /start 验证本人手机号，获得 Premium 后用 /strategy 启用策略。
选择独立金额：CASH 1 200USD / CASH 2 300USD；或先 CASH 0 设置公共现金，再 /strategy_weights 分配总和为 100% 的比例。
机器人不连接券商，也不会自动下单或同步成交。
/chat 可联系管理员，需先验证手机号，留言及回复保存 14 天后自动删除。
/privacy 查看隐私说明；/unbind 确认后删除账户数据，无法撤销。
/help 查看你当前已启用策略的专属使用说明。"""


def strategy_name(strategy_id: str) -> str:
    # Unknown database values are not interpolated into Telegram HTML.
    return STRATEGY_NAMES.get(strategy_id, "未识别策略（暂不可用，请联系管理员）")


def strategy_management_text(weights: dict, allocation_valid: bool, budgets=None) -> str:
    budgets = budgets or {}
    lines = ["📊 策略管理", "第一步：启用策略（可同时使用）"]
    for number in (1, 2):
        key = f"strategy{number}"
        lines += [strategy_name(key) + (" · 已启用" if key in weights else " · 未启用"),
                  f"/strategy{number} " + ("查看说明" if key in weights else "启用")]
        if key in weights:
            lines.append(f"/strategy{number}_off 停用")
    lines += ["", "第二步：选择一种资金方式"]
    if budgets:
        lines += ["当前：独立金额模式（比例设置暂停）"]
        for key, value in sorted(budgets.items())[:20]:
            amount, currency = value if isinstance(value, tuple) else (value, 'USD')
            lines.append(f"策略 {key.removeprefix('strategy')}：{amount:,.2f} {currency}")
        if len(budgets) > 20:
            lines.append("更多独立现金请用 /portfolio 翻页查看。")
    else:
        lines += ["当前：公共现金 / 比例模式（CASH 0）"]
        lines += [f"策略 {k.removeprefix('strategy')}：{v:g}%" if v is not None else f"{strategy_name(k)}：待分配" for k, v in sorted(weights.items())]
    lines += ["CASH 1 200USD → 替换策略 1 的独立现金", "CASH 2 300USD → 替换策略 2 的独立现金",
              "也支持CASH 2 300AUD，汇率自动获取；/fx查看或刷新，不会实际换汇。",
              "CASH 0 500USD → 清除全部独立现金，改用公共现金",
              "/strategy_weights → 仅公共现金模式可设置比例，例如 1=60 2=40；合计 100%",
              "独立金额模式的持仓按各策略现金占比划分计算份额；预算保留给对应编号，停用不会自动重分配。",
              "✅ 资金配置有效。" if allocation_valid else "下一步：补齐已启用策略的现金或有效比例，完成后才能生成建议。",
              "", "第三步：记录持仓并查看报告", "/add 或 /import 录入持仓，/portfolio 核对；没有持仓可跳过。"]
    if weights:
        lines.append("/check 检查所有已启用策略，结果分别发送")
    if "strategy1" in weights:
        lines += ["/strategy1_pool 修改自己的 S1 股票池；/check1 查看 S1", "/auto_notice_date 设置 S1 月度买入提醒日"]
    if "strategy2" in weights:
        lines.append("/check2 查看 S2 趋势与操作")
    lines += ["每天交易日盘前出报告，无需操作也会说明。需 Premium、通知与系统自动运行均已开启。",
              "/settings 通知设置；/help 使用说明；/cancel 取消输入。", "只提供建议，不会自动下单。"]
    return "\n".join(lines)


def welcome_blocks(enabled: set[str]) -> list[str]:
    return [STRATEGY_WELCOME_BLOCKS.get(key, "该策略暂不可用，请通过 /chat 联系管理员。")
            for key in sorted(enabled)]


def help_pages(enabled: set[str], *, admin: bool = False) -> list[str]:
    """One bounded message per strategy; selection does not imply availability."""
    common = COMMON_HELP + ("\n\n/admin_help　管理员命令说明书（管理功能无需启用投资策略）" if admin else "")
    if not enabled:
        return [common + "\n\n" + STRATEGY_PICK]
    return [common] + [STRATEGY_HELP_BLOCKS[key] for key in sorted(enabled) if key in STRATEGY_HELP_BLOCKS]


# Full strategy-1 reference for topic fallbacks and documentation checks.
QUICK = COMMON_HELP + "\n\n" + STRATEGY1_HELP
ADMIN_QUICK = QUICK + "\n\n🛡️ <b>管理员</b>\n/admin_help　查看管理员命令说明书"

ADMIN_HELP = """🛡️ <b>管理员说明书</b>
需要参数的命令可先单独发送，再在下一条消息输入参数；输入有误会提示重试，/cancel 可取消。下列括号是占位符，请换成实际值。

<b>管理员名单</b>
/admin_list　分页列出 Bot Owner 和所有管理员；/admin_list 2 查看第 2 页
/admin_show (用户ID)　按 Telegram ID 查询管理员
/admin_add (用户ID) [备注]　按 Telegram ID 添加管理员（备注可省略）
/admin_edit (用户ID) 新备注　修改备注；用 <code>-</code> 清空
/admin_remove (用户ID)　移除这项 ID 授权

以上四条也接受手机号，例如：
<code>/admin_add +(国家区号) (手机号) [备注]</code>
<code>/admin_show +(国家区号) (手机号)</code>
<code>/admin_edit +(国家区号) (手机号) 新备注</code>
<code>/admin_remove +(国家区号) (手机号)</code>
手机号只保存 HMAC 指纹。对方尚未验证时，授权待生效；本人验证后绑定到该 Telegram ID。号码换人不会自动转移权限，须移除旧授权再重新添加。ID 与手机号是独立授权，移除其中一项不影响另一项。

普通管理员可以管理其他普通管理员，但不能添加、修改或移除 Bot Owner。Owner 的 ID 只能在服务器配置中设置。新管理员仍须先用 /start 验证本人手机号。

<b>免费白名单</b>
/whitelist_list　每页 10 项；<code>/whitelist_list 2</code> 查看第 2 页
/whitelist_show +(国家区号) (手机号)　查询号码是否在白名单及其备注
/whitelist_add +(国家区号) (手机号) [备注]　添加手机号到免费 Premium 白名单，备注可省略
/whitelist_edit +(国家区号) (手机号) 新备注　修改备注；用 <code>-</code> 清空
/whitelist_remove +(国家区号) (手机号)　移除手机号
以上四条也支持用户 ID：
<code>/whitelist_add (用户ID) [备注]</code>
<code>/whitelist_show (用户ID)</code>
<code>/whitelist_edit (用户ID) 新备注</code>
<code>/whitelist_remove (用户ID)</code>
可先添加尚未注册的 ID，但用户仍须 /start 验证本人手机号后才能获得 Premium。ID 与手机号是独立授权，移除一项不影响另一项；ID 白名单在解绑后保留，重新验证后生效。手机号仅保存 HMAC 指纹，不显示完整号码。
留言的白名单备注优先使用 ID 备注，无 ID 备注则使用手机号备注；同时有管理员备注时显示（白名单备注/管理员备注）。

<b>运营与维护</b>
/announce　编辑、预览并确认发布普通公告
/important_announce　向所有已验证用户发送并置顶重要公告；替换本次运行期间的旧重要公告置顶
两类公告均不附加编号，不持久化正文、投递或置顶记录。重启后不续传，也不能自动移除重启前的旧置顶；Telegram 中已发出的消息仍然保留。
/users　查看用户概况
/stats　查看系统统计
/health　检查 Bot 与数据库状态
/runstrategy　立即运行策略 1，向已启用策略 1、权重有效、已开启策略通知且有 Premium 权限的用户推送建议

<b>其他</b>
管理员管理功能不要求启用投资策略；查看自己的投资建议仍须先启用策略。
/help　普通用户命令
/cancel　取消正在输入的操作"""

CSV = f"""📥 <b>账户 · 导入持仓与策略现金</b>
先发 /import，再发送 CSV 文件（UTF-8，不超过 1 MB）。示例，点一下可以复制：

<pre>Company,Shares Held,Average Price
CASH,0,1000USD
ISRG,0.55,374.21 USD
NVDA,3,120 USD</pre>

{LINE}
<b>怎么写</b>
• 每行一只股票：代码，股数，平均成本（带币种，如 374.21 USD）
• 股票代码恰为 CASH 时写 <code>STOCK:CASH</code>，避免与现金行混淆
• <code>CASH,0,1000USD</code> 为公共现金，必须是唯一现金行，才可按比例分配。
• 独立金额可同时填写 <code>CASH,1,200USD</code> 与 <code>CASH,2,300AUD</code>，AUD汇率自动获取。第二列是策略编号，支持 1、2、3 等，设置预算不会启用策略。
• 独立现金与 CASH 0 不能混用。旧 current_cash 行兼容为 CASH 0。

<b>要注意</b>
• 文件包含已有同名股票时，整次导入会被拒绝；修改已有持仓请用 /edit
• CASH N 替换该编号现金；CASH 0 清除独立现金并替换公共余额。不含现金行则保持原设置。
• 有错误会指出行号，这时整份文件都不会导入
• 美股持仓价格仍为USD；AUD现金自动按市场参考汇率折算USD，/fx可查看。CSV导出保留现金原币，不将换算金额写回原币记录。
• /export 导出的文件格式相同；已有持仓不能重复导入，恢复前请核对账户记录

不想用文件：/add，按提示输入「股票代码 股数 平均成本 币种」，例如 <code>ISRG 0.55 374.21 USD</code>"""

FAQ = """❓ <b>策略 1 · 常见问题及账户说明</b>

<b>/check1 没有给出买卖建议？</b>
可能是现金为 0（用 /cash 填写），或者目前没有符合条件的标的。底部的提示会说明。

<b>数据日期不是今天？</b>
行情日期是最近可用交易日，周末或休市时不一定是今天。共享数据超过半天等情况下，/check1 会显示已有结果并后台更新；更新失败会提示重试，受重试间隔限制。首次准备数据可能需要几分钟。

<b>为什么我的建议和别人不同？</b>
建议参考你填报的持仓、现金及策略资金权重。请先用 /portfolio 核对记录。

<b>多个策略的资金怎么算？</b>
单策略默认 100%。同时启用多个策略后，用 /strategy_weights 手动分配，每项大于 0，总和为 100%，最多两位小数。百分比以持仓市值与现金合计为基准：例如 60%/40%，各策略按这个比例使用持仓份额和现金计算建议，原始持仓记录不变。独立现金用 CASH 1 200USD、CASH 2 300USD；该模式按各策略现金占总现金的比例划分已有持仓份额，不启用百分比设置。仅有 CASH 0 时才可按比例分配。

<b>停用或修改权重会怎样？</b>
/strategy1_off 停止策略 1 的建议与通知，保留持仓和现金。/strategy1 可重新启用。多策略停用一项后若权重不足 100%，须重新分配；不会自动把资金转给其他策略。权重修改后旧的待发送建议不再推送，当月已处理的定时提醒不会补发，可用 /check1 重新查看。

<b>会不会自动下单？</b>
不会。没有任何下单功能，也不连接你的券商账户。

<b>卖出后怎么更新？</b>
部分卖出用 /edit 填写剩余总股数，全部卖出用 /remove。确认后会询问是否加入冷却期（默认设置：加入）。如果只是改错、没有实际卖出，选「不加入」。持仓修改不会自动调整现金，请再用 /cash 填写成交后的现金余额。用 /cooldown 查看当前冷却中的股票。

<b>我的数据别人看得到吗？</b>
不会。每个人只用自己的持仓和现金计算，看到的也只有自己的结果。

<b>怎么联系管理员？</b>
发 /chat，再输入留言。只有绑定手机号的用户可以留言；管理员回复会由机器人发给你。留言和回复最多 2000 字，在数据库中保存 14 天后自动删除。

<b>怎么解绑、删除我的数据？</b>
发 /unbind，确认后会删除你的手机号绑定、持仓、现金、操作记录和建议，不能撤销，建议先 /export 导出。之后随时可以用 /start 重新绑定。

<b>怎么关闭每月推送？</b>
/settings signals（再发一次就重新打开）。Premium 有效且策略启用、权重有效时，/check1 仍可手动查看。

<b>怎么修改每月通知日期？</b>
发 /auto_notice_date 5，设为每月第 5 个美股交易日；或发 /auto_notice_date random，每月随机选一个真实美股交易日。固定日期只接受 1–23 的整数，周末和休市日不算。回复会说明本月、下个月是否生效；固定模式还会检查未来 12 个月中哪些月份交易日不足。交易日不足则当月跳过，不自动提前或顺延；当月日期已过不补发，当月已通知过不重复发送。随机模式当月仅从设置之后的交易日中选。设置只影响自己，不会自动开启已关闭的通知。

<b>币种？</b>
美股持仓和建议使用USD。现金支持USD和AUD；AUD汇率自动获取，/fx查看或刷新，报告标明来源和报价时间。只作估值，不会实际换汇。行情无效时暂停AUD资金建议；其他币种持仓不纳入美股分析。"""

TOPICS = {
    "导入": CSV, "csv": CSV, "import": CSV,
    "问题": FAQ, "faq": FAQ,
}


def help_for(topic: str | None, registered: bool = True) -> str:
    """Registered users get the quick start without the binding step; people who have not bound a phone yet only get how to start."""
    if not registered:
        return QUICK_NEW
    if not topic:
        return QUICK
    return TOPICS.get(topic.strip().lower(), QUICK + "\n\n<i>（没有这个主题，上面是总览。）</i>")
