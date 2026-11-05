from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class TokenCipher:
    def __init__(self, key_file: Path):
        self.key_file = key_file
        self._fernet = Fernet(self._load_or_create_key())

    def _load_or_create_key(self) -> bytes:
        if self.key_file.exists():
            if self.key_file.stat().st_mode & 0o077:
                raise PermissionError(f"Encryption key must be mode 0600: {self.key_file}")
            return self.key_file.read_bytes().strip()
        self.key_file.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        descriptor = os.open(self.key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(key + b"\n")
        return key

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode()).decode()
        except InvalidToken as exc:
            raise RuntimeError("Unable to decrypt stored OAuth token") from exc
