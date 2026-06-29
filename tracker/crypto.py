from cryptography.fernet import Fernet
from django.conf import settings


def _fernet():
    key = settings.FERNET_KEY
    if not key:
        raise RuntimeError('FERNET_KEY is not set; cannot encrypt/decrypt credentials.')
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()
