"""Encrypt/decrypt LLM provider API keys stored in the database.

Fernet, keyed by ``LLM_KEY_ENCRYPTION_KEY`` -- deliberately NOT derived from
``DJANGO_SECRET_KEY``, so rotating one never silently breaks the other.

Generate a key with:

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

import os

from cryptography.fernet import Fernet, InvalidToken

ENCRYPTION_KEY_ENV_VAR = "LLM_KEY_ENCRYPTION_KEY"


class LLMKeyEncryptionError(RuntimeError):
    """Raised when ``LLM_KEY_ENCRYPTION_KEY`` is missing or invalid, or a
    stored ciphertext can't be decrypted with the configured key."""


def _fernet() -> Fernet:
    key = os.environ.get(ENCRYPTION_KEY_ENV_VAR)
    if not key:
        raise LLMKeyEncryptionError(
            f"{ENCRYPTION_KEY_ENV_VAR} is not set. Generate one with "
            '`python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"` and add it to your .env file.'
        )
    try:
        return Fernet(key.encode("utf-8") if isinstance(key, str) else key)
    except (ValueError, TypeError) as exc:
        raise LLMKeyEncryptionError(
            f"{ENCRYPTION_KEY_ENV_VAR} is not a valid Fernet key: {exc}"
        ) from exc


def encrypt_key(plaintext: str) -> bytes:
    """Encrypt a provider API key for ``LLMConfiguration.encrypted_api_key``."""
    return _fernet().encrypt(plaintext.encode("utf-8"))


def decrypt_key(blob: bytes) -> str:
    """Decrypt a stored ciphertext blob back to the plaintext API key."""
    try:
        return _fernet().decrypt(bytes(blob)).decode("utf-8")
    except InvalidToken as exc:
        raise LLMKeyEncryptionError(
            "Stored LLM API key could not be decrypted -- "
            f"{ENCRYPTION_KEY_ENV_VAR} may have changed since it was saved."
        ) from exc
