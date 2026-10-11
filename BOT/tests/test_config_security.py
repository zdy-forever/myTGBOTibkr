import pytest

from quantbot.security import PhoneProtector, normalize_phone


def test_contact_phone_validation():
    assert normalize_phone("+61 400-000-001") == "+61400000001"
    assert normalize_phone("61 400-000-001") == "+61400000001"
    with pytest.raises(ValueError):
        normalize_phone("123")


def test_encrypted_phone_round_trip():
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    protector = PhoneProtector("x" * 32, key)
    encrypted = protector.encrypt("+61400000001")
    assert "+61400000001" not in encrypted
    assert protector.decrypt(encrypted) == "+61400000001"


def test_http_client_logs_never_contain_the_bot_token(capsys):
    import logging

    from quantbot.main import configure_logging

    configure_logging("INFO")
    logging.getLogger("httpx").info("HTTP Request: POST https://api.telegram.org/bot123456:SECRET/getMe")
    logging.getLogger("httpx").warning("kept")
    out = capsys.readouterr().out
    assert "SECRET" not in out and "kept" in out
