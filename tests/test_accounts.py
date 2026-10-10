
from quantbot.db import transaction
from quantbot.models import CashAccount, Role


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
