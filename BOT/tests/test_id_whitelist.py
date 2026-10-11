from decimal import Decimal

import pytest
from sqlalchemy import select

from quantbot import bot
from quantbot.chat import _sender_note
from quantbot.models import AdminGrant, CashAccount, PhoneWhitelist, UserIdWhitelist
from tests.test_premium_access import Message, context, make_update


def test_id_grant_requires_verification_and_preserves_account(factory, accounts):
    with factory.begin() as session:
        accounts.register(session, 999, "+61400000999")
        assert accounts.whitelist_add(session, 999, 101, "ID note")
    with factory.begin() as session:
        assert accounts.access_status(session, 101) == "unregistered"
        assert not accounts.has_premium_access(session, 101)
        user = accounts.register(session, 101, "+61400000001").user
        uid = user.id
        session.get(CashAccount, uid).balance = Decimal("123.45")
        assert accounts.access_status(session, 101) == "whitelist"
        assert not accounts.whitelist_add(session, 999, 101, "cannot overwrite")
        assert accounts.whitelist_edit(session, 999, 101, "new note")
        accounts.whitelist_add(session, 999, "+61400000001", "phone note")
    with factory.begin() as session:
        assert session.get(UserIdWhitelist, 101).label == "new note"
        assert accounts.whitelist_remove(session, 999, 101)
    with factory.begin() as session:
        assert accounts.access_status(session, 101) == "whitelist"
        assert session.get(CashAccount, uid).balance == Decimal("123.45")
        assert accounts.whitelist_remove(session, 999, "+61400000001")
    with factory.begin() as session:
        assert accounts.access_status(session, 101) == "subscription_required"


async def test_id_whitelist_commands_staged_edit_show_list_remove(factory, accounts):
    with factory.begin() as session:
        accounts.register(session, 999, "+61400000999")
    ctx = context(factory, accounts)
    ctx.user_data = {}
    await bot._staged_command_entry(make_update(Message(text="/whitelist_add"), user_id=999), ctx)
    await bot._staged_command_data(make_update(Message(text="101 <note>"), user_id=999), ctx)
    ctx.args = ["101"]
    msg = Message()
    await bot.whitelist_show(make_update(msg, user_id=999), ctx)
    assert "&lt;note&gt;" in msg.replies[0][0] and "尚未绑定" in msg.replies[0][0]
    ctx.args = ["101", "-"]
    await bot.whitelist_edit(make_update(Message(), user_id=999), ctx)
    with factory.begin() as session:
        assert session.get(UserIdWhitelist, 101).label is None
        for tid in range(102, 112):
            accounts.whitelist_add(session, 999, tid)
    ctx.args = ["2"]
    msg = Message()
    await bot.whitelist_list(make_update(msg, user_id=999), ctx)
    assert "第 2/2 页" in msg.replies[0][0] and "共 11 项" in msg.replies[0][0]
    ctx.args = ["101"]
    await bot.whitelist_remove(make_update(Message(), user_id=999), ctx)
    with factory.begin() as session:
        assert session.get(UserIdWhitelist, 101) is None


def test_whitelist_id_permissions_notes_and_rebinding(factory, accounts):
    with factory.begin() as session:
        accounts.register(session, 999, "+61400000999")
        user = accounts.register(session, 101, "+61400000001").user
        with pytest.raises(PermissionError):
            accounts.whitelist_add(session, 101, 102)
        accounts.whitelist_add(session, 999, 101, "ID")
        accounts.whitelist_add(session, 999, "+61400000001", "phone")
        session.add(AdminGrant(telegram_id=101, label="admin", created_by_telegram_id=999))
        session.flush()
        assert _sender_note(session, user.id) == "备注：(ID/admin)\n"
        session.get(UserIdWhitelist, 101).label = None
        assert _sender_note(session, user.id) == "备注：(phone/admin)\n"
        accounts.unregister(session, 101)
        assert not accounts.has_premium_access(session, 101)
        assert session.get(UserIdWhitelist, 101) is not None
        assert session.scalar(select(PhoneWhitelist)) is not None


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "1e3", "１２３", "4503599627370496", "@username"])
def test_id_whitelist_rejects_invalid_ids(value):
    with pytest.raises(ValueError):
        bot._validate_command_args("whitelist_add", [value])
