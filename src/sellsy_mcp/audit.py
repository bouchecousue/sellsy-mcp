from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class AuditLog:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
        elif self.path.stat().st_mode & 0o077:
            raise PermissionError(f"Audit log must be mode 0600: {self.path}")

    def write(self, event: dict[str, Any]) -> None:
        safe = {
            "timestamp": datetime.now(UTC).isoformat(),
            "staff_id": event.get("staff_id"),
            "staff_email": event.get("staff_email"),
            "tool": event.get("tool"),
            "object_type": event.get("object_type"),
            "object_id": event.get("object_id"),
            "manifest_id": event.get("manifest_id"),
            "result": event.get("result"),
            "error_code": event.get("error_code"),
            "request_id": event.get("request_id"),
            "before_fingerprint": event.get("before_fingerprint"),
            "after_fingerprint": event.get("after_fingerprint"),
        }
        with self._lock:
            if self.path.stat().st_size >= 10_000_000:
                oldest = self.path.with_suffix(self.path.suffix + ".5")
                oldest.unlink(missing_ok=True)
                for index in range(4, 0, -1):
                    old = self.path.with_suffix(self.path.suffix + f".{index}")
                    if old.exists():
                        old.rename(self.path.with_suffix(self.path.suffix + f".{index + 1}"))
                self.path.rename(self.path.with_suffix(self.path.suffix + ".1"))
                descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.close(descriptor)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(safe, ensure_ascii=False, separators=(",", ":")) + "\n")
