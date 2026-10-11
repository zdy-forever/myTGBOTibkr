from __future__ import annotations

import hashlib
import hmac
import re

import phonenumbers
from cryptography.fernet import Fernet


def normalize_phone(phone: str) -> str:
    if not isinstance(phone, str):
        raise ValueError("请输入含国家区号的有效手机号，格式：+(国家区号) (手机号)")  # noqa: TRY004 - invalid user input is a ValueError
    value = phone.strip()
    # Telegram clients can omit the leading '+' while still sending the
    # international number. Never guess a region for a national-only number.
    if not value.startswith("+") and re.fullmatch(r"[0-9][0-9 ()-]*", value):
        value = "+" + value
    try:
        parsed = phonenumbers.parse(value, None)
    except (AttributeError, phonenumbers.NumberParseException) as exc:
        raise ValueError("请输入含国家区号的有效手机号，格式：+(国家区号) (手机号)") from exc
    if not phonenumbers.is_valid_number(parsed):
        raise ValueError("请输入含国家区号的有效手机号，格式：+(国家区号) (手机号)")
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


class PhoneProtector:
    def __init__(self, hmac_secret: str, encryption_key: str | None = None) -> None:
        if len(hmac_secret) < 32:
            raise ValueError("HMAC secret must contain at least 32 characters")
        self._secret = hmac_secret.encode()
        self._fernet = Fernet(encryption_key.encode()) if encryption_key else None

    def fingerprint(self, phone: str) -> str:
        normalized = normalize_phone(phone)
        return hmac.new(self._secret, normalized.encode(), hashlib.sha256).hexdigest()

    def random_notice_day(self, user_id: str, month: str, trading_day_count: int) -> int:
        """A stable, secret-keyed selection for one account and calendar month."""
        if trading_day_count < 1:
            raise ValueError("trading_day_count must be positive")
        digest = hmac.new(self._secret, f"monthly-notice:{user_id}:{month}".encode(), hashlib.sha256).digest()
        return int.from_bytes(digest, "big") % trading_day_count + 1

    def encrypt(self, phone: str) -> str | None:
        if self._fernet is None:
            return None
        return self._fernet.encrypt(normalize_phone(phone).encode()).decode()

    def decrypt(self, value: str | None) -> str | None:
        if not value or self._fernet is None:
            return None
        return self._fernet.decrypt(value.encode()).decode()
