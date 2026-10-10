from __future__ import annotations

import hashlib
import hmac
import re

from cryptography.fernet import Fernet


def normalize_phone(phone: str) -> str:
    phone = phone.strip()
    digits = re.sub(r"\D", "", phone)
    if not 7 <= len(digits) <= 15:
        raise ValueError("invalid phone number length")
    return "+" + digits


class PhoneProtector:
    def __init__(self, hmac_secret: str, encryption_key: str | None = None) -> None:
        if len(hmac_secret) < 32:
            raise ValueError("HMAC secret must contain at least 32 characters")
        self._secret = hmac_secret.encode()
        self._fernet = Fernet(encryption_key.encode()) if encryption_key else None

    def fingerprint(self, phone: str) -> str:
        normalized = normalize_phone(phone)
        return hmac.new(self._secret, normalized.encode(), hashlib.sha256).hexdigest()

    def encrypt(self, phone: str) -> str | None:
        if self._fernet is None:
            return None
        return self._fernet.encrypt(normalize_phone(phone).encode()).decode()

    def decrypt(self, value: str | None) -> str | None:
        if not value or self._fernet is None:
            return None
        return self._fernet.decrypt(value.encode()).decode()
