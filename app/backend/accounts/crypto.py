"""Versioned authenticated encryption and purpose-separated lookup digests."""
import base64
import hashlib
import hmac
import json
import secrets
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings
from backend.recommendation.errors import DomainError


def key(setting):
    try:
        value = base64.b64decode(getattr(settings, setting), validate=True)
        if len(value) != 32:
            raise ValueError()
        return value
    except (ValueError, TypeError, AttributeError):
        raise DomainError('ACCOUNT_KEYS_NOT_READY', '계정 보호 설정을 확인해 주세요.', 503) from None


def digest(purpose, value):
    return hmac.new(key('MEMBER_LOOKUP_KEY'), (purpose + '\0' + value).encode(), hashlib.sha256).hexdigest()


def seal(value):
    version = settings.MEMBER_ACTIVE_EMAIL_KEY
    try:
        raw = base64.b64decode(settings.MEMBER_EMAIL_KEYS[version], validate=True)
        if len(raw) != 32 or not version.isalnum():
            raise ValueError()
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(raw).encrypt(nonce, json.dumps(value, ensure_ascii=False).encode(), version.encode())
        return version + '.' + base64.urlsafe_b64encode(nonce + ciphertext).decode()
    except (KeyError, ValueError, TypeError):
        raise DomainError('ACCOUNT_KEYS_NOT_READY', '계정 보호 설정을 확인해 주세요.', 503) from None


def unseal(value):
    try:
        version, encoded = value.split('.', 1)
        raw = base64.b64decode(settings.MEMBER_EMAIL_KEYS[version], validate=True)
        data = base64.urlsafe_b64decode(encoded)
        return json.loads(AESGCM(raw).decrypt(data[:12], data[12:], version.encode()))
    except Exception:
        raise DomainError('ACCOUNT_DATA_UNAVAILABLE', '계정 보호 정보를 확인할 수 없습니다.', 503) from None
