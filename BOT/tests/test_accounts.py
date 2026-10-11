
from types import SimpleNamespace

from telegram.ext import Application, ConversationHandler, MessageHandler

from quantbot import bot
from quantbot.db import transaction
from quantbot.models import CashAccount, Role, User


class _Message:
    def __init__(self, contact=None):
        self.contact = contact
        self.from_user = SimpleNamespace(id=101)
        self.forward_origin = None
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))


def _update(message, telegram_id=101):
    return SimpleNamespace(message=message, effective_user=SimpleNamespace(id=telegram_id),
                           effective_chat=SimpleNamespace(type="private"))


def _context(factory, accounts):
    return SimpleNamespace(application=SimpleNamespace(bot_data={"session_factory": factory, "accounts": accounts}))


def test_registration_initializes_account_and_repeated_start(factory, accounts):
    with transaction(factory) as session:
        first = accounts.register(session, 101, "+61 400 000 001")
        user_id = first.user.id
        assert first.created
    with transaction(factory) as session:
        second = accounts.register(session, 101, "+61 499 999 999")
        assert not second.created
        assert second.user.id == user_id
        assert session.get(CashAccount, user_id).balance == 0


def test_phone_is_hmac_not_plain_sha_and_duplicate_binding_denied(factory, accounts):
    import hashlib
    with transaction(factory) as session:
        user = accounts.register(session, 101, "+61400000001").user
        assert "+61400000001" not in user.phone_fingerprint
        assert user.phone_fingerprint != hashlib.sha256(b"+61400000001").hexdigest()
        assert user.phone_encrypted is None
    with transaction(factory) as session:
        try:
            accounts.register(session, 102, "+61400000001")
        except ValueError as exc:
            assert "already bound" in str(exc)
        else:
            raise AssertionError("duplicate phone binding should fail")


def test_maintainer_is_server_allowlist_only(factory, accounts):
    with transaction(factory) as session:
        admin = accounts.register(session, 999, "+61400000999").user
        normal = accounts.register(session, 100, "+61400000100").user
        assert admin.role == Role.MAINTAINER
        assert normal.role == Role.USER
        accounts.require_maintainer(session, 999)
        try:
            accounts.require_maintainer(session, 100)
        except PermissionError:
            pass
        else:
            raise AssertionError("normal user was treated as admin")


async def test_repeated_start_and_old_phone_prompts(factory, accounts):
    context = _context(factory, accounts)
    first = _Message()
    second = _Message()
    assert await bot.start(_update(first), context) == bot.CONTACT
    assert await bot.start(_update(second), context) == bot.CONTACT
    assert "分享我的手机号" in first.replies[0][0]
    assert "分享我的手机号" in second.replies[0][0]

    own_contact = SimpleNamespace(user_id=101, phone_number="+61400000001")
    old_prompt = _Message(own_contact)
    assert await bot.contact(_update(old_prompt), context) == ConversationHandler.END
    assert "注册完成" in old_prompt.replies[0][0]

    stale_prompt = _Message(own_contact)
    assert await bot.contact(_update(stale_prompt), context) == ConversationHandler.END
    assert "已失效" in stale_prompt.replies[0][0]
    with transaction(factory) as session:
        assert session.query(User).count() == 1
    assert await bot.start(_update(_Message()), context) == ConversationHandler.END


def test_start_handler_accepts_reentry_and_old_contact(factory, accounts):
    app = Application.builder().token("123456:TEST").build()
    bot.configure_application(app, accounts, None)
    start_handler = app.handlers[0][0]
    assert isinstance(start_handler, ConversationHandler)
    assert start_handler.allow_reentry
    assert any(isinstance(handler, MessageHandler) and handler.callback is bot.contact
               for handler in app.handlers[0][1:])
