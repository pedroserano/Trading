"""Schwab token storage, encrypted at rest.

The token file can place trades for as long as its refresh token lives, so it is never
written in plaintext. The encryption key is derived from SECRET_KEY, which lives in the
environment and not on the same dataset as the token.
"""

import base64
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


class TokenStoreError(Exception):
    pass


class TokenStore:
    def __init__(self, path: Path, secret_key: str):
        self.path = path
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"schwab-token-v1").derive(
            secret_key.encode()
        )
        self._fernet = Fernet(base64.urlsafe_b64encode(key))

    def exists(self) -> bool:
        return self.path.exists()

    def read(self) -> dict:
        try:
            return json.loads(self._fernet.decrypt(self.path.read_bytes()))
        except FileNotFoundError as e:
            raise TokenStoreError("No Schwab token stored") from e
        except InvalidToken as e:
            raise TokenStoreError(
                "Stored Schwab token cannot be decrypted. SECRET_KEY has probably changed; "
                "sign in to Schwab again."
            ) from e

    def write(self, token: dict, *args, **kwargs) -> None:
        # Extra arguments are passed by the OAuth client on refresh and are not needed.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(self._fernet.encrypt(json.dumps(token).encode()))
        os.replace(tmp, self.path)

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)
