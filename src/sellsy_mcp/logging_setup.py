from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler


class SafeLogFilter(logging.Filter):
    def filter(self, record):
        # Defence in depth for libraries logging URLs, including callback code/state and CRM queries.
        record.msg = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?[redacted]", record.getMessage())
        record.args = ()
        return True


def configure_logging(path):
    os.umask(0o077)
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    os.chmod(path, 0o600)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(SafeLogFilter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    for name in ("httpx", "httpcore", "httpx2", "httpcore2", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.disabled = True
