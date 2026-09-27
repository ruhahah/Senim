"""Счётчик эффекта: сколько ответов проверено, сколько ошибок поймано, сколько это стоит.
Хранит только числа — ни текстов ответов, ни данных пользователей."""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from .config import get_settings

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None

SCHEMA = """CREATE TABLE IF NOT EXISTS checks (
  ts REAL, channel TEXT, lang TEXT, claims INTEGER, supported INTEGER, disputed INTEGER,
  contradicted INTEGER, unverifiable INTEGER, fabricated_citations INTEGER, trust INTEGER,
  elapsed_ms INTEGER, llm_calls INTEGER, cache_hits INTEGER, tokens_in INTEGER, tokens_out INTEGER,
  cost_usd REAL)"""


FEEDBACK_SCHEMA = """CREATE TABLE IF NOT EXISTS feedback (
  ts REAL, channel TEXT, lang TEXT, useful INTEGER, would_notice TEXT)"""


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        path = Path(get_settings().stats_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(path), check_same_thread=False)
        _conn.execute(SCHEMA)
        _conn.execute(FEEDBACK_SCHEMA)
        _conn.commit()
    return _conn


def record_feedback(channel: str, lang: str, useful: bool | None, would_notice: str | None) -> None:
    """Мини-опрос после проверки: помог ли Senim, заметил бы человек ошибку сам."""
    if not get_settings().stats_enabled:
        return
    with _lock:
        _db().execute("INSERT INTO feedback VALUES (?,?,?,?,?)",
                      (time.time(), channel, lang, None if useful is None else int(useful), would_notice))
        _db().commit()


def feedback_totals() -> dict:
    with _lock:
        rows = _db().execute("SELECT useful, would_notice FROM feedback").fetchall()
    useful = [u for u, _ in rows if u is not None]
    notice = [w for _, w in rows if w]
    return {
        "answers": len(rows),
        "useful_yes": sum(useful), "useful_total": len(useful),
        "share_useful": round(sum(useful) / len(useful), 3) if useful else 0,
        "would_not_notice": sum(1 for w in notice if w == "no"),
        "would_notice_total": len(notice),
        "share_would_not_notice": round(sum(1 for w in notice if w == "no") / len(notice), 3) if notice else 0,
    }


def record(channel: str, lang: str, trust: dict, elapsed_ms: int, usage: dict) -> None:
    if not get_settings().stats_enabled:
        return
    c = trust.get("counts", {})
    row = (time.time(), channel, lang, sum(c.get(k, 0) for k in ("supported", "disputed", "contradicted", "unverifiable")),
           c.get("supported", 0), c.get("disputed", 0), c.get("contradicted", 0), c.get("unverifiable", 0),
           trust.get("fabricated_citations", 0), trust.get("trust_index", 0), elapsed_ms,
           usage.get("calls", 0), usage.get("cache_hits", 0), usage.get("in", 0), usage.get("out", 0),
           round(usage.get("cost_usd", 0.0), 6))
    with _lock:
        _db().execute("INSERT INTO checks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
        _db().commit()


def feedback_totals_unlocked() -> dict:
    return feedback_totals()


def totals() -> dict:
    with _lock:
        r = _db().execute("""SELECT COUNT(*), COALESCE(SUM(claims),0), COALESCE(SUM(contradicted),0),
            COALESCE(SUM(disputed),0), COALESCE(SUM(fabricated_citations),0), COALESCE(AVG(elapsed_ms),0),
            COALESCE(SUM(cost_usd),0), COALESCE(SUM(CASE WHEN llm_calls > 0 THEN 1 ELSE 0 END),0),
            COALESCE(SUM(CASE WHEN llm_calls > 0 THEN cost_usd ELSE 0 END),0),
            COALESCE(SUM(CASE WHEN contradicted > 0 OR fabricated_citations > 0 THEN 1 ELSE 0 END),0)
            FROM checks""").fetchone()
        by_channel = dict(_db().execute("SELECT channel, COUNT(*) FROM checks GROUP BY channel").fetchall())
        by_lang = dict(_db().execute("SELECT lang, COUNT(*) FROM checks GROUP BY lang").fetchall())
    checks, claims, contradicted, disputed, fabricated, avg_ms, cost, paid_checks, paid_cost, with_errors = r
    return {
        "checks": checks, "claims_checked": claims, "errors_caught": contradicted + fabricated,
        "disputed": disputed, "fabricated_citations": fabricated,
        "answers_with_errors": with_errors,
        "share_answers_with_errors": round(with_errors / checks, 3) if checks else 0,
        "avg_seconds": round(avg_ms / 1000, 1),
        "total_cost_usd": round(cost, 4),
        "avg_cost_per_check_usd": round(paid_cost / paid_checks, 5) if paid_checks else 0,
        "by_channel": by_channel, "by_lang": by_lang,
        "feedback": feedback_totals_unlocked(),
    }
