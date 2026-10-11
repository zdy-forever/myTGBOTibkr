from types import SimpleNamespace

import pytest
from telegram.ext import ApplicationHandlerStop

from quantbot import bot
from quantbot.config import Settings
from quantbot.db import transaction
from quantbot.models import AdminGrant, AdminPhoneGrant, CashAccount, Role, User
from quantbot.security import PhoneProtector
from quantbot.services import AccountService


def configured_accounts():
    return AccountService(PhoneProtector("x" * 32), frozenset({200, 300}), owner_telegram_id=999)


def test_owner_requires_phone_and_legacy_admins_import_once(factory):
    accounts = configured_accounts()
    with transaction(factory) as session:
        assert not accounts.has_premium_access(session, 999)
        with pytest.raises(PermissionError):
            accounts.require_maintainer(session, 999)
        accounts.bootstrap_admins(session)
    with transaction(factory) as session:
        assert accounts.maintainer_ids(session) == frozenset({999, 200, 300})
        accounts.register(session, 999, "+61400000999")
        accounts.register(session, 200, "+61400000200")
        assert accounts.has_premium_access(session, 999)
        assert accounts.has_premium_access(session, 200)
        assert not accounts.has_premium_access(session, 300)
        assert accounts.admin_remove(session, 999, 200)
    restarted = configured_accounts()
    with transaction(factory) as session:
        restarted.bootstrap_admins(session)
        assert restarted.maintainer_ids(session) == frozenset({999, 300})
        assert not restarted.has_premium_access(session, 200)
        assert session.query(User).filter_by(telegram_id=200).one().role == Role.USER


def test_admin_crud_owner_protection_and_existing_account_data(factory):
    accounts = configured_accounts()
    with transaction(factory) as session:
        accounts.bootstrap_admins(session)
    with transaction(factory) as session:
        accounts.register(session, 999, "+61400000999")
        accounts.register(session, 200, "+61400000200")
        target = accounts.register(session, 400, "+61400000400").user
        session.get(CashAccount, target.id).balance = 123
    with transaction(factory) as session:
        assert accounts.admin_add(session, 200, 400, "研究员")
    with transaction(factory) as session:
        assert accounts.has_premium_access(session, 400)
        assert session.get(AdminGrant, 400).label == "研究员"
        assert accounts.admin_edit(session, 200, 400, "运营")
        with pytest.raises(PermissionError, match="Owner"):
            accounts.admin_remove(session, 200, 999)
        with pytest.raises(PermissionError, match="Owner"):
            accounts.admin_edit(session, 200, 999, "假 Owner")
        with pytest.raises(PermissionError, match="Owner"):
            accounts.admin_add(session, 200, 999)
    with transaction(factory) as session:
        assert session.get(AdminGrant, 400).label == "运营"
        assert accounts.admin_remove(session, 999, 400)
        assert not accounts.has_premium_access(session, 400)
        assert session.get(CashAccount, target.id).balance == 123
        assert session.get(User, target.id).role == Role.USER
        assert accounts.is_maintainer(session, 999)


def test_phone_admin_pending_claim_and_no_recycled_number_transfer(factory):
    accounts = configured_accounts()
    phone = "+86 13800138000"
    fingerprint = accounts.protector.fingerprint(phone)
    with transaction(factory) as session:
        accounts.register(session, 999, "+61400000999")
        assert accounts.admin_add_phone(session, 999, phone, "手机授权")
        grant = session.get(AdminPhoneGrant, fingerprint)
        assert grant.telegram_id is None
        assert phone not in repr(grant.__dict__)
    with transaction(factory) as session:
        assert not accounts.is_maintainer(session, 400)
        accounts.register(session, 400, phone)
        assert accounts.has_premium_access(session, 400)
        assert session.get(AdminPhoneGrant, fingerprint).telegram_id == 400
        assert accounts.admin_edit_phone(session, 999, phone, "新备注")
        assert session.get(AdminPhoneGrant, fingerprint).label == "新备注"
    with transaction(factory) as session:
        assert accounts.unregister(session, 400)
        accounts.register(session, 401, phone)
        assert not accounts.is_maintainer(session, 401)
        assert not accounts.has_premium_access(session, 401)
        assert 401 not in accounts.maintainer_ids(session)
        assert accounts.admin_remove_phone(session, 999, phone)
        assert accounts.admin_add_phone(session, 999, phone)
        assert accounts.is_maintainer(session, 401)


def test_phone_admin_cannot_modify_owner_and_id_grant_survives_phone_removal(factory):
    accounts = configured_accounts()
    with transaction(factory) as session:
        accounts.register(session, 999, "+61400000999")
        accounts.register(session, 400, "+61400000400")
        with pytest.raises(PermissionError, match="Owner"):
            accounts.admin_add_phone(session, 999, "+61400000999")
        assert accounts.admin_add_phone(session, 999, "+61400000400")
        assert accounts.admin_add(session, 999, 400)
        assert accounts.admin_remove_phone(session, 999, "+61400000400")
        assert accounts.is_maintainer(session, 400)
        assert session.query(User).filter_by(telegram_id=400).one().role == Role.MAINTAINER


class Message:
    def __init__(self, text):
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))


def update(message, telegram_id):
    return SimpleNamespace(message=message, effective_message=message,
                           effective_user=SimpleNamespace(id=telegram_id),
                           effective_chat=SimpleNamespace(type="private"), pre_checkout_query=None,
                           callback_query=None)


def context(factory, accounts, args=None):
    return SimpleNamespace(application=SimpleNamespace(bot_data={"session_factory": factory, "accounts": accounts}),
                           args=args or [], user_data={})


async def test_admin_help_is_private_and_commands_work(factory):
    accounts = configured_accounts()
    with transaction(factory) as session:
        accounts.bootstrap_admins(session)
        accounts.register(session, 999, "+61400000999")
        accounts.register(session, 200, "+61400000200")
        accounts.register(session, 101, "+61400000001")

    normal = Message("/admin_help")
    await bot.admin_help(update(normal, 101), context(factory, accounts))
    assert "仅已验证的管理员" in normal.replies[0][0]
    with pytest.raises(ApplicationHandlerStop):
        await bot.authorization_gate(update(Message("/admin_help"), 101), context(factory, accounts))

    for admin_id in (999, 200):
        message = Message("/admin_help")
        await bot.admin_help(update(message, admin_id), context(factory, accounts))
        manual = message.replies[0][0]
        assert "/admin_add" in manual and "/admin_edit" in manual and "/admin_remove" in manual
        assert "/whitelist_add" in manual and "/runstrategy" in manual

    add = Message("/admin_add 400 团队成员")
    await bot.admin_add(update(add, 200), context(factory, accounts, ["400", "团队成员"]))
    assert "须先用 /start" in add.replies[0][0]
    show = Message("/admin_show 400")
    await bot.admin_show(update(show, 200), context(factory, accounts, ["400"]))
    assert "团队成员" in show.replies[0][0] and "未完成" in show.replies[0][0]
    with transaction(factory) as session:
        accounts.register(session, 400, "+61400000400")
        assert accounts.has_premium_access(session, 400)
    listing = Message("/admin_list")
    await bot.admin_list(update(listing, 200), context(factory, accounts))
    assert "Bot Owner" in listing.replies[0][0] and "400" in listing.replies[0][0]
    remove_owner = Message("/admin_remove 999")
    await bot.admin_remove(update(remove_owner, 200), context(factory, accounts, ["999"]))
    assert "不能移除 Bot Owner" in remove_owner.replies[0][0]


async def test_admin_phone_commands(factory):
    accounts = configured_accounts()
    with transaction(factory) as session:
        accounts.register(session, 999, "+61400000999")
    add = Message("/admin_add +86 13800138000 研究员")
    await bot.admin_add(update(add, 999), context(factory, accounts, ["+86", "13800138000", "研究员"]))
    assert "手机号管理员授权" in add.replies[0][0] and "须先用 /start" in add.replies[0][0]
    show = Message("/admin_show +86 13800138000")
    await bot.admin_show(update(show, 999), context(factory, accounts, ["+86", "13800138000"]))
    assert "待验证管理员" in show.replies[0][0]
    assert "13800138000" not in show.replies[0][0]
    with transaction(factory) as session:
        accounts.register(session, 400, "+86 13800138000")
    listing = Message("/admin_list")
    await bot.admin_list(update(listing, 999), context(factory, accounts))
    assert "手机指纹" in listing.replies[0][0] and "400" in listing.replies[0][0]
    edit = Message("/admin_edit +86 13800138000 新备注")
    await bot.admin_edit(update(edit, 999), context(factory, accounts, ["+86", "13800138000", "新备注"]))
    assert "已更新" in edit.replies[0][0]
    remove = Message("/admin_remove +86 13800138000")
    await bot.admin_remove(update(remove, 999), context(factory, accounts, ["+86", "13800138000"]))
    assert "已移除" in remove.replies[0][0]
    with transaction(factory) as session:
        assert not accounts.is_maintainer(session, 400)


def test_bot_will_not_start_without_explicit_owner():
    from quantbot.main import build_application

    settings = Settings(telegram_bot_token="123456:TEST", phone_hmac_secret="x" * 32)
    with pytest.raises(ValueError, match="BOT_OWNER_TELEGRAM_ID"):
        build_application(settings)
