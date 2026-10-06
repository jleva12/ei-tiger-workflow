"""MCP servers' credentials, encrypted at rest.

An API key, a client secret or an OAuth grant is kept in MySQL as a Fernet
token (AES-128-CBC with an HMAC) of its JSON, under a key derived from
``FORGE_ADMIN_SECRETS_KEY`` with HKDF. Nothing that's encrypted is ever
answered by the API: secrets are write-only.
"""

import base64
import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# Binds the derived key to what it encrypts, so the same secret could key
# something else without the two ever sharing a key.
_INFO = b"forge-admin/mcp-servers/credentials/v1"


class SecretsError(Exception):
    """A value couldn't be decrypted: the key changed, or it was tampered with."""


class SecretBox:
    """
    Encrypts and decrypts JSON values with one key.

    :param secret: ``FORGE_ADMIN_SECRETS_KEY``: at least 32 characters.
    """

    def __init__(self, secret: str) -> None:
        if len(secret) < 32:
            raise ValueError("The secrets key needs at least 32 characters")
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_INFO).derive(
            secret.encode()
        )
        self._fernet = Fernet(base64.urlsafe_b64encode(key))

    def seal(self, value: Any) -> str | None:
        """
        :param value: A JSON value; None (or an empty dict) for nothing.
        :return: Its ciphertext, or None for nothing.
        """
        if value is None or value == {}:
            return None
        plain = json.dumps(value, separators=(",", ":")).encode()
        return self._fernet.encrypt(plain).decode("ascii")

    def open(self, sealed: str | None) -> dict[str, Any]:
        """
        :param sealed: What :meth:`seal` made, or None.
        :return: The value; an empty dict for None.
        :raises SecretsError: It can't be decrypted with this key.
        """
        if not sealed:
            return {}
        try:
            value = json.loads(self._fernet.decrypt(sealed.encode("ascii")))
        except (InvalidToken, ValueError) as error:
            raise SecretsError(
                "A saved credential can't be decrypted: FORGE_ADMIN_SECRETS_KEY "
                "changed since it was saved. Enter it again."
            ) from error
        return value if isinstance(value, dict) else {}
