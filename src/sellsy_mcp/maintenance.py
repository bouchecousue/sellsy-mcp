from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from contextlib import asynccontextmanager

LOGGER = logging.getLogger(__name__)


def maintain(provider):
    provider.store.prune()
    for key, lock in list(provider._grant_locks.items()):
        # Runs on the same event loop as requests, without an intervening await.
        if not lock.locked() and not getattr(lock, "_waiters", None):
            provider._grant_locks.pop(key, None)
    provider.last_maintenance = int(time.time())
    LOGGER.info("metrics=%s", dict(provider.metrics))
    backup_dir = provider.settings.database_path.parent / "backups"
    backup_dir.mkdir(mode=0o700, exist_ok=True)
    from .admin import encrypted_backup

    backups = sorted(backup_dir.glob("state-*.fernet"))
    if not backups or time.time() - backups[-1].stat().st_mtime >= 86400:
        encrypted_backup(provider.store, provider.cipher, backup_dir / f"state-{int(time.time())}.fernet")
    for old in sorted(backup_dir.glob("state-*.fernet"))[:-7]:
        old.unlink()


def lifespan_for(provider):
    async def periodic():
        while True:
            await asyncio.sleep(provider.settings.maintenance_interval_seconds)
            try:
                maintain(provider)
            except Exception as exc:
                LOGGER.error("maintenance_failed error_type=%s", type(exc).__name__)

    @asynccontextmanager
    async def lifespan(_server):
        maintain(provider)
        task = asyncio.create_task(periodic())
        try:
            yield {}
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await provider.close()

    return lifespan
