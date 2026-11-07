"""Thread-safe configuration file persistence for Momcozy Client.

Ensures concurrent writers (WebServer settings toggles, Tuya session refreshes,
device sync, and LAN IP discovery) merge keys atomically under per-path locks
and write in-place to preserve Docker single-file bind mount inodes.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict

logger = logging.getLogger(__name__)

_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: Dict[Path, threading.RLock] = {}


def resolve_default_config_path() -> Path:
    """Resolve the default .momcozy.json configuration path."""
    env_cfg = os.environ.get("MOMCOZY_CONFIG")
    if env_cfg:
        p = Path(env_cfg).expanduser().resolve()
        if p.is_dir():
            return p / ".momcozy.json"
        return p
    script_dir = Path(__file__).resolve().parent.parent
    return script_dir / ".momcozy.json"


DEFAULT_CONFIG_PATH = resolve_default_config_path()


def normalize_config_path(path: Path | str) -> Path:
    """Normalize a config path. If pointing to a directory, append .momcozy.json or momcozy.json."""
    p = Path(path).expanduser()
    if p.is_dir():
        for candidate in (".momcozy.json", "momcozy.json"):
            if (p / candidate).is_file():
                return p / candidate
        return p / ".momcozy.json"
    return p


def _get_lock(path: Path) -> threading.RLock:
    try:
        key = path.resolve()
    except OSError:
        key = path
    with _LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


class ConfigStore:
    """Thread-safe JSON config reader and merger using in-place writes."""

    @staticmethod
    def read(path: Path | str) -> Dict[str, Any]:
        """Read and return config dictionary, or empty dict if missing or invalid."""
        target = normalize_config_path(path)
        with _get_lock(target):
            if not target.exists():
                return {}
            try:
                data = json.loads(target.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
            except Exception as exc:
                logger.warning("Failed reading config %s: %s", target, exc)
                return {}

    @staticmethod
    def mutate(
        path: Path | str,
        mutator: Callable[[Dict[str, Any]], bool | None],
        *,
        create: bool = False,
    ) -> Dict[str, Any]:
        """Atomically read, mutate in-memory, and write config back in-place.

        If `mutator(data)` explicitly returns `False`, writing is skipped.
        Uses `write_text` in-place rather than `os.replace` so Docker file bind
        mounts (`./.momcozy.json:/config/.momcozy.json`) keep their host inode.
        """
        target = normalize_config_path(path)
        with _get_lock(target):
            exists = target.exists()
            if not exists and not create:
                return {}

            data: Dict[str, Any] = {}
            if exists:
                try:
                    loaded = json.loads(target.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        data = loaded
                except Exception as exc:
                    logger.warning("Could not parse existing config %s before update: %s", target, exc)

            should_write = mutator(data)
            if should_write is False:
                return data

            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(data, indent=2), encoding="utf-8")
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass
            return data

    @classmethod
    def update(
        cls,
        path: Path | str,
        updates: Dict[str, Any],
        *,
        create: bool = True,
    ) -> Dict[str, Any]:
        """Merge `updates` into `path` without dropping unrelated existing keys."""
        def _apply(data: Dict[str, Any]) -> bool:
            data.update(updates)
            return True

        return cls.mutate(path, _apply, create=create)
