import base64
import hashlib
import logging
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


logger = logging.getLogger(__name__)
AAD = b"ultex-meta-reports:v1"


def _master_key() -> bytes:
    configured = settings.DATA_ENCRYPTION_KEY.strip()
    if configured:
        try:
            key = base64.urlsafe_b64decode(configured.encode("ascii"))
        except Exception as exc:
            raise ImproperlyConfigured("DATA_ENCRYPTION_KEY must be URL-safe base64.") from exc
        if len(key) != 32:
            raise ImproperlyConfigured("DATA_ENCRYPTION_KEY must decode to exactly 32 bytes.")
        return key
    if not settings.DEBUG:
        raise ImproperlyConfigured("DATA_ENCRYPTION_KEY is required when DJANGO_DEBUG=0.")
    logger.warning("Using a development-only encryption key derived from DJANGO_SECRET_KEY.")
    return hashlib.sha256(f"ultex-dev:{settings.SECRET_KEY}".encode("utf-8")).digest()


def encrypt_secret(value: str) -> str:
    if not value:
        return ""
    nonce = os.urandom(12)
    ciphertext = AESGCM(_master_key()).encrypt(nonce, value.encode("utf-8"), AAD)
    return base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")


def decrypt_secret(value: str) -> str:
    if not value:
        return ""
    try:
        payload = base64.urlsafe_b64decode(value.encode("ascii"))
        return AESGCM(_master_key()).decrypt(payload[:12], payload[12:], AAD).decode("utf-8")
    except Exception as exc:
        raise ImproperlyConfigured("The stored encrypted secret cannot be decrypted with the current key.") from exc


def secret_fingerprint(value: str) -> str:
    if not value:
        return ""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]

