"""Encryption for bank details at rest (Fernet: AES-128-CBC + HMAC-SHA256).

The key comes from FIELD_ENCRYPTION_KEY (a 32-byte urlsafe-base64 string; generate one with
`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`).
LOSE THE KEY AND EVERY STORED ACCOUNT NUMBER IS UNRECOVERABLE. Keep a copy somewhere safe.

With DEBUG on and no key set, a key is derived from SECRET_KEY so local development works.
In production a missing key is an error: bank details are never stored unencrypted.
"""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def _fernet():
    key = settings.FIELD_ENCRYPTION_KEY
    if not key:
        if not settings.DEBUG:
            raise ImproperlyConfigured('FIELD_ENCRYPTION_KEY is not set; refusing to handle bank details.')
        key = base64.urlsafe_b64encode(hashlib.sha256(settings.SECRET_KEY.encode()).digest()).decode()
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except ValueError as exc:
        raise ImproperlyConfigured(f'FIELD_ENCRYPTION_KEY is not a valid Fernet key: {exc}')


def encrypt(plain):
    return _fernet().encrypt(plain.encode()).decode() if plain else ''


def decrypt(token):
    if not token:
        return ''
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        raise ValueError('Stored value cannot be decrypted (wrong FIELD_ENCRYPTION_KEY?).')


def mask(number, show=4):
    """'••••1234' for display. Never show more than the last 4 digits."""
    number = (number or '').strip()
    return ('••••' + number[-show:]) if number else ''
