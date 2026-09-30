"""Хранилище Senim: статистика, опрос, классы учителей.

- Если задан DATABASE_URL (postgres://…) — данные лежат в PostgreSQL (например, бесплатный Neon)
  и НЕ стираются при перезапуске хостинга.
- Иначе — локальный файл SQLite (удобно на своём компьютере и в тестах).

Запросы пишутся с плейсхолдером «?», для PostgreSQL он автоматически меняется на «%s».
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

from .config import get_settings

log = logging.getLogger("senim.db")

_lock = threading.RLock()
_db: Optional["DB"] = None

# Типы колонок: FLOAT → REAL в SQLite и DOUBLE PRECISION в PostgreSQL (время в секундах от 1970 года
# не помещается точно в 4-байтовый REAL PostgreSQL).
TABLES = {
    "checks": """ts FLOAT, channel TEXT, lang TEXT, claims INTEGER, supported INTEGER, disputed INTEGER,
      contradicted INTEGER, unverifiable INTEGER, fabricated_citations INTEGER, trust INTEGER,
      elapsed_ms INTEGER, llm_calls INTEGER, cache_hits INTEGER, tokens_in INTEGER, tokens_out INTEGER,
      cost_usd FLOAT""",
    "feedback": "ts FLOAT, channel TEXT, lang TEXT, useful INTEGER, would_notice TEXT",
    "classes": "code TEXT PRIMARY KEY, key_hash TEXT, name TEXT, created FLOAT",
    "class_checks": """ts FLOAT, code TEXT, student TEXT, channel TEXT, lang TEXT, trust INTEGER, band TEXT,
      claims INTEGER, supported INTEGER, disputed INTEGER, contradicted INTEGER, unverifiable INTEGER,
      fabricated_citations INTEGER, snippet TEXT, problems TEXT""",
    "class_think": "ts FLOAT, code TEXT, student TEXT, caught INTEGER, missed INTEGER, false_alarms INTEGER",
    "tg_members": "chat_id TEXT PRIMARY KEY, code TEXT, student TEXT, ts FLOAT",
    "trainer_rounds": "id TEXT PRIMARY KEY, code TEXT, created FLOAT, lang TEXT, topic TEXT, payload TEXT",
    "trainer_results": """ts FLOAT, round_id TEXT, code TEXT, student TEXT, correct INTEGER, missed INTEGER,
      false_alarms INTEGER, seconds INTEGER, score INTEGER""",
}
MIGRATIONS = [
    ("checks", "source_ai", "TEXT"),   # чей ответ проверяли (ChatGPT, Gemini…) — для рейтинга ИИ
]
INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_class_checks_code ON class_checks(code)",
    "CREATE INDEX IF NOT EXISTS idx_class_think_code ON class_think(code)",
    "CREATE INDEX IF NOT EXISTS idx_trainer_results_round ON trainer_results(round_id)",
    "CREATE INDEX IF NOT EXISTS idx_trainer_rounds_code ON trainer_rounds(code)",
]


class DB:
    def __init__(self, url: str, sqlite_path: str):
        self.url = url.strip()
        self.sqlite_path = sqlite_path
        self.is_pg = self.url.startswith(("postgres://", "postgresql://"))
        self._conn: Any = None

    # ------------------------------------------------------------ connection
    def _connect(self):
        if self.is_pg:
            import psycopg  # только если используется PostgreSQL

            self._conn = psycopg.connect(self.url, autocommit=True, connect_timeout=10)
        else:
            path = Path(self.sqlite_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._create_schema()

    def _create_schema(self):
        for name, cols in TABLES.items():
            cols = cols.replace("FLOAT", "DOUBLE PRECISION" if self.is_pg else "REAL")
            self._raw(f"CREATE TABLE IF NOT EXISTS {name} ({cols})")
        for sql in INDEXES:
            self._raw(sql)
        # новые колонки в старых таблицах (миграции без внешних инструментов)
        for table, col, typ in MIGRATIONS:
            try:
                if self.is_pg:
                    self._raw(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {typ}")
                else:
                    cols = [r[1] for r in self._raw(f"PRAGMA table_info({table})").fetchall()]
                    if col not in cols:
                        self._raw(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
            except Exception as e:  # noqa: BLE001
                log.warning("migration %s.%s: %s", table, col, e)
        if not self.is_pg:
            self._conn.commit()

    def _raw(self, sql: str, params: tuple = ()):
        if self.is_pg:
            sql = sql.replace("?", "%s")
        cur = self._conn.cursor()
        cur.execute(sql, params)
        return cur

    # ------------------------------------------------------------ public API
    def execute(self, sql: str, params: tuple = ()) -> list[tuple]:
        """Выполняет запрос и возвращает строки (для INSERT — пустой список).
        Разорванное соединение (Neon «засыпает» без запросов) переподключается один раз."""
        with _lock:
            for attempt in range(2):
                try:
                    if self._conn is None:
                        self._connect()
                    cur = self._raw(sql, params)
                    rows = cur.fetchall() if cur.description else []
                    if not self.is_pg:
                        self._conn.commit()
                    return [tuple(r) for r in rows]
                except Exception as e:  # noqa: BLE001
                    if attempt == 0 and self.is_pg and _is_connection_error(e):
                        log.warning("БД: соединение потеряно (%s) — переподключаюсь", type(e).__name__)
                        self.close()
                        continue
                    raise
        return []

    def close(self):
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:  # noqa: BLE001
            pass
        self._conn = None

    @property
    def kind(self) -> str:
        return "postgres" if self.is_pg else "sqlite"


def _is_connection_error(e: Exception) -> bool:
    name = type(e).__name__
    return name in ("OperationalError", "InterfaceError", "AdminShutdown") or "closed" in str(e).lower()


def get_db() -> DB:
    global _db
    if _db is None:
        s = get_settings()
        _db = DB(s.database_url, s.stats_path)
    return _db


def reset_db() -> None:
    """Для тестов: закрыть соединение и перечитать настройки."""
    global _db
    if _db is not None:
        _db.close()
    _db = None
