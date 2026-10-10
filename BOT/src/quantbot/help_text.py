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
3️⃣ 绑定后再发 /help，就能看到怎么用

<i>用手机版 Telegram；不能手动输入；号码只用来识别你本人，默认只保存不可逆的指纹。</i>"""

QUICK = f"""📘 <b>快速上手</b>

1️⃣ <b>录入你的持仓和现金</b>
/add 逐只添加　/import 导入 CSV
/cash 填写可以投入的现金

2️⃣ <b>随时查看建议</b>
/check 今天的打分，和按你的持仓给出的买卖建议

3️⃣ <b>每月自动推送</b>
每月美股第一个交易日开盘前（北京时间白天）发给你。建议按你当时的持仓和现金单独计算，每个人收到的不一样。

⚠️ <i>仅供参考，不构成投资建议；系统不会下单，也不连接你的券商账户，买卖由你自己决定。</i>

{LINE}
🧭 <b>全部命令</b>（点命令名就能发送）

📊 <b>查看</b>（直接发送，不需要参数）
/check　今天的排名和你的买卖建议
/cooldown　暂不建议买入/卖出的股票
/portfolio　我的持仓和现金
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
/settings　查看通知设置。<code>/settings signals</code> 开/关每月推送，<code>/settings announcements</code> 开/关公告
/unbind　解绑手机号并删除我的全部数据。发送后点确认；建议先 /export 备份
/cancel　在输入过程中随时取消

📖 <b>更多说明</b>
/help_import　CSV 导入格式
/help_faq　常见问题"""

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

<b>我的数据别人看得到吗？</b>
不会。每个人只用自己的持仓和现金计算，看到的也只有自己的结果。

<b>怎么解绑、删除我的数据？</b>
发 /unbind，确认后会删除你的手机号绑定、持仓、现金、操作记录和建议，不能撤销，建议先 /export 导出。之后随时可以用 /start 重新绑定。

<b>怎么关闭每月推送？</b>
/settings signals（再发一次就重新打开）。/check 随时可用。

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
