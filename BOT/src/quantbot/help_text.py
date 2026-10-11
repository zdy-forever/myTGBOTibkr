"""Texts for /help, in Telegram HTML (bold headings, a copyable <pre> CSV example). Every topic stays well below Telegram's 4096-character limit.
Telegram only turns Latin letters, digits and underscores into tappable commands, so the topic shortcuts are /help_import and /help_faq."""
from __future__ import annotations

LINE = "━━━━━━━━━━━━━━"

QUICK_NEW = f"""📘 <b>这是什么</b>
按你自己的持仓和现金，给出美股的买卖建议。

⚠️ <i>仅供参考，不构成投资建议；系统不会下单，也不连接你的券商账户，买卖由你自己决定。</i>

{LINE}
🔐 <b>使用前先绑定手机号</b>
1️⃣ 发 /start
2️⃣ 点输入框下方的按钮「📱 分享我的手机号」，在弹出的框里选「分享」
3️⃣ 授权后会检查免费白名单及付费订阅

<i>用手机版 Telegram；不能手动输入；号码只用来识别你本人，默认只保存不可逆的指纹。隐私说明：/privacy</i>"""

QUICK_PENDING = """✅ 手机号已验证，但尚未开通 Premium。
400 Telegram Stars / 30 天（约 US$7.99，实际价格因购买渠道而异）。

/subscribe　开通订阅
/chat　给管理员留言
/start　查看当前权限
/privacy　查看手机号与付款隐私说明
/help　查看可用命令
/unbind　解绑并删除账户数据
/cancel　取消当前操作"""

QUICK = f"""📘 <b>快速上手</b>

1️⃣ <b>录入你的持仓和现金</b>
/add 逐只添加　/import 导入 CSV
/cash 填写可以投入的现金

2️⃣ <b>随时查看建议</b>
/check 今天的打分，和按你的持仓给出的买卖建议

3️⃣ <b>每月自动推送</b>
默认每月美股第一个交易日开盘前（北京时间白天）发给你。用 /auto_notice_date 修改你自己的通知日期。建议按你当时的持仓和现金单独计算，每个人收到的不一样。

⚠️ <i>仅供参考，不构成投资建议；系统不会下单，也不连接你的券商账户，买卖由你自己决定。</i>

{LINE}
🧭 <b>全部命令</b>（点命令名就能发送）

📊 <b>查看</b>（直接发送，不需要参数）
/check　今天的排名和你的买卖建议
/cooldown　暂不建议买入/卖出的股票
/portfolio　我的持仓和现金；较多时用按钮翻页，或发 <code>/portfolio 2</code> 查看第 2 页
/history　最近 20 条账户操作
/chat　给管理员留言。发送后输入内容；管理员回复会通过机器人发给你，留言和回复保存 14 天后自动删除

✏️ <b>录入</b>
/add　添加持仓。发送后输入「代码 股数 平均成本 币种」，例如 <code>ISRG 0.55 374.21 USD</code>
/edit　修改持仓。发送后输入「代码 新股数 新平均成本」，例如 <code>ISRG 0.75 380</code>，再点确认
/remove　删除持仓。发送 <code>/remove ISRG</code>（换成你的股票代码），再点确认
/cash　修改现金。发送后输入金额，例如 <code>1000 USD</code>（<code>1000usd</code>、<code>$1000</code> 也行）

📁 <b>导入导出</b>
/import　发送后，再把 CSV 文件发给我（格式见 /help_import）
/export　直接发送，会收到一份 CSV 文件

⚙️ <b>设置</b>
/settings　查看通知设置。<code>/settings signals</code> 开/关每月推送，<code>/settings announcements</code> 开/关公告
/auto_notice_date　修改个人通知日期，例如 <code>/auto_notice_date 5</code> 表示第 5 个美股交易日；<code>/auto_notice_date random</code> 每月随机选一个交易日；不带参数查看设置及日期预览
/unbind　解绑手机号并删除我的全部数据。发送后点确认；建议先 /export 备份
/cancel　在输入过程中随时取消
/subscribe　查看订阅状态或开通 Premium
/privacy　手机号与付款隐私说明

📖 <b>更多说明</b>
/help_import　CSV 导入格式
/help_faq　常见问题"""

ADMIN_QUICK = QUICK + "\n\n🛡️ <b>管理员</b>\n/admin_help　查看管理员命令说明书"

ADMIN_HELP = """🛡️ <b>管理员说明书</b>

<b>管理员名单</b>
/admin_list　分页列出 Bot Owner 和所有管理员；/admin_list 2 查看第 2 页
/admin_show 123456789　按 Telegram ID 查询管理员
/admin_add 123456789 张三　按 Telegram ID 添加管理员（备注可省略）
/admin_edit 123456789 新备注　修改备注；用 <code>-</code> 清空
/admin_remove 123456789　移除这项 ID 授权

以上四条也接受手机号，例如：
<code>/admin_add +86 13800138000 张三</code>
<code>/admin_show +86 13800138000</code>
<code>/admin_edit +86 13800138000 新备注</code>
<code>/admin_remove +86 13800138000</code>
手机号只保存 HMAC 指纹。对方尚未验证时，授权待生效；本人验证后绑定到该 Telegram ID。号码换人不会自动转移权限，须移除旧授权再重新添加。ID 与手机号是独立授权，移除其中一项不影响另一项。

普通管理员可以管理其他普通管理员，但不能添加、修改或移除 Bot Owner。Owner 的 ID 只能在服务器配置中设置。新管理员仍须先用 /start 验证本人手机号。

<b>免费白名单</b>
/whitelist_list　每页 10 项；<code>/whitelist_list 2</code> 查看第 2 页
/whitelist_show +86 13800138000　查询号码是否在白名单及其备注
/whitelist_add +86 13800138000 张三　添加手机号到免费 Premium 白名单，备注可省略
/whitelist_edit +86 13800138000 新备注　修改备注；用 <code>-</code> 清空
/whitelist_remove +86 13800138000　移除手机号
必须写国家区号与手机号；机器人只保存 HMAC 指纹，列表不会还原显示完整号码。

<b>运营与维护</b>
/announce　编辑、预览并确认发布公告
/important_announce　向所有已验证用户发送并置顶重要公告；下一条重要公告会替换旧置顶
/users　查看用户概况
/stats　查看系统统计
/health　检查 Bot 与数据库状态
/runstrategy　立即运行策略，向已开启策略通知且有 Premium 权限的用户推送建议

<b>其他</b>
/help　普通用户命令
/cancel　取消正在输入的操作"""

CSV = f"""📥 <b>导入持仓</b>
先发 /import，再发送 CSV 文件（UTF-8，不超过 1 MB）。示例，点一下可以复制：

<pre>Company,Shares Held,Average Price
current_cash,0,1000 USD
ISRG,0.55,374.21 USD
NVDA,3,120 USD</pre>

{LINE}
<b>怎么写</b>
• 每行一只股票：代码，股数，平均成本（带币种，如 374.21 USD）
• <code>current_cash</code> 这一行是现金：第二列固定填 <code>0</code>（填什么都不影响），第三列是金额和币种

<b>要注意</b>
• 已有的同名持仓不会被覆盖
• 有错误会指出行号，这时整份文件都不会导入
• 只分析美元（USD），其他币种会被忽略
• /export 导出的文件格式相同，可以直接再导入

不想用文件：/add，按提示输入「股票代码 股数 平均成本 币种」，例如 <code>ISRG 0.55 374.21 USD</code>"""

FAQ = """❓ <b>常见问题</b>

<b>/check 没有给出买卖建议？</b>
可能是现金为 0（用 /cash 填写），或者目前没有符合条件的标的。底部的提示会说明。

<b>数据日期不是今天？</b>
行情和财报每天最多更新一次。上一次更新超过半天时，/check 会先显示旧结果，同时在后台更新，完成后自动把最新的再发给你（首次可能要几分钟）。

<b>为什么同一只股票别人看到的分数和我不一样？</b>
分数会参考你自己的持仓，所以每个人看到的不完全一样。

<b>会不会自动下单？</b>
不会。没有任何下单功能，也不连接你的券商账户。

<b>卖出后怎么更新？</b>
部分卖出用 /edit 减少股数，全部卖出用 /remove 删除持仓。确认后会询问是否加入冷却期（默认设置：加入）。如果只是改错、没有实际卖出，选「不加入」。用 /cooldown 查看当前冷却中的股票。

<b>我的数据别人看得到吗？</b>
不会。每个人只用自己的持仓和现金计算，看到的也只有自己的结果。

<b>怎么联系管理员？</b>
发 /chat，再输入留言。只有绑定手机号的用户可以留言；管理员回复会由机器人发给你。留言和回复最多 2000 字，在数据库中保存 14 天后自动删除。

<b>怎么解绑、删除我的数据？</b>
发 /unbind，确认后会删除你的手机号绑定、持仓、现金、操作记录和建议，不能撤销，建议先 /export 导出。之后随时可以用 /start 重新绑定。

<b>怎么关闭每月推送？</b>
/settings signals（再发一次就重新打开）。/check 随时可用。

<b>怎么修改每月通知日期？</b>
发 /auto_notice_date 5，设为每月第 5 个美股交易日；或发 /auto_notice_date random，每月随机选一个真实美股交易日。固定日期只接受 1–23 的整数，周末和休市日不算。回复会说明本月、下个月是否生效；固定模式还会检查未来 12 个月中哪些月份交易日不足。交易日不足则当月跳过，不自动提前或顺延；当月日期已过不补发，当月已通知过不重复发送。随机模式当月仅从设置之后的交易日中选。设置只影响自己，不会自动开启已关闭的通知。

<b>币种？</b>
只分析美元金额，其他币种会被忽略并提示。"""

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
