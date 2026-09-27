"""Простой кеш на SQLite: экономит лимиты API и делает демо стабильным."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .config import get_settings

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        path = Path(get_settings().cache_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(path), check_same_thread=False)
        _conn.execute(
            "CREATE TABLE IF NOT EXISTS cache (k TEXT PRIMARY KEY, v TEXT, ts REAL)"
        )
        _conn.commit()
    return _conn


def make_key(namespace: str, payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return namespace + ":" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get(key: str) -> Optional[Any]:
    if not get_settings().cache_enabled:
        return None
    with _lock:
        row = _db().execute("SELECT v FROM cache WHERE k=?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def put(key: str, value: Any) -> None:
    if not get_settings().cache_enabled:
        return
    with _lock:
        _db().execute(
            "INSERT OR REPLACE INTO cache (k, v, ts) VALUES (?, ?, ?)",
            (key, json.dumps(value, ensure_ascii=False), time.time()),
        )
        _db().commit()


def clear() -> int:
    with _lock:
        n = _db().execute("DELETE FROM cache").rowcount
        _db().commit()
    return n
