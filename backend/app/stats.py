"""Счётчик эффекта: сколько ответов проверено, сколько ошибок поймано, сколько это стоит.
Хранит только числа — ни текстов ответов, ни данных пользователей (тексты классов — в classroom.py)."""
from __future__ import annotations

import json
import logging
import time

from .config import get_settings
from .db import get_db


def record_feedback(channel: str, lang: str, useful: bool | None, would_notice: str | None) -> None:
    """Мини-опрос после проверки: помог ли Senim, заметил бы человек ошибку сам."""
    if not get_settings().stats_enabled:
        return
    get_db().execute("INSERT INTO feedback VALUES (?,?,?,?,?)",
                     (time.time(), channel, lang, None if useful is None else int(useful), would_notice))


def feedback_totals() -> dict:
    rows = get_db().execute("SELECT useful, would_notice FROM feedback")
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


SOURCE_AIS = ["chatgpt", "gemini", "claude", "copilot", "deepseek", "grok", "perplexity", "yandex", "gigachat", "other"]
SOURCE_AI_NAMES = {"chatgpt": "ChatGPT", "gemini": "Gemini", "claude": "Claude", "copilot": "Copilot",
                   "deepseek": "DeepSeek", "grok": "Grok", "perplexity": "Perplexity", "yandex": "Алиса (YandexGPT)",
                   "gigachat": "GigaChat", "other": "Другой ИИ"}


def clean_source_ai(v: str | None) -> str:
    v = (v or "").strip().lower()
    return v if v in SOURCE_AIS else ""


def record(channel: str, lang: str, trust: dict, elapsed_ms: int, usage: dict, source_ai: str = "") -> None:
    if not get_settings().stats_enabled:
        return
    c = trust.get("counts", {})
    row = (time.time(), channel, lang, sum(c.get(k, 0) for k in ("supported", "disputed", "contradicted", "unverifiable")),
           c.get("supported", 0), c.get("disputed", 0), c.get("contradicted", 0), c.get("unverifiable", 0),
           trust.get("fabricated_citations", 0), trust.get("trust_index", 0), elapsed_ms,
           usage.get("calls", 0), usage.get("cache_hits", 0), usage.get("in", 0), usage.get("out", 0),
           round(usage.get("cost_usd", 0.0), 6))
    get_db().execute("""INSERT INTO checks (ts, channel, lang, claims, supported, disputed, contradicted,
        unverifiable, fabricated_citations, trust, elapsed_ms, llm_calls, cache_hits, tokens_in, tokens_out,
        cost_usd, source_ai) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", row + (clean_source_ai(source_ai) or None,))


RATING_MIN_CHECKS = 5


def live_rating() -> dict:
    """Рейтинг ИИ по реальным проверкам пользователей: у чьих ответов больше ошибок."""
    rows = get_db().execute("""SELECT source_ai, lang, COUNT(*), COALESCE(SUM(claims),0), COALESCE(SUM(supported),0),
        COALESCE(SUM(disputed),0), COALESCE(SUM(contradicted),0), COALESCE(SUM(fabricated_citations),0),
        COALESCE(AVG(trust),0) FROM checks WHERE source_ai IS NOT NULL AND source_ai <> ''
        GROUP BY source_ai, lang""")
    agg: dict[str, dict] = {}
    for ai, lang, n, claims, sup, disp, contra, fab, avg_trust in rows:
        a = agg.setdefault(ai, {"id": ai, "name": SOURCE_AI_NAMES.get(ai, ai), "checks": 0, "claims": 0,
                                "supported": 0, "disputed": 0, "contradicted": 0, "fabricated_citations": 0,
                                "trust_sum": 0.0, "by_lang": {}})
        n, claims, sup, disp, contra, fab = map(int, (n, claims, sup, disp, contra, fab))
        a["checks"] += n
        a["claims"] += claims
        a["supported"] += sup
        a["disputed"] += disp
        a["contradicted"] += contra
        a["fabricated_citations"] += fab
        a["trust_sum"] += float(avg_trust) * n
        a["by_lang"][lang] = _rates({"checks": n, "claims": claims, "supported": sup, "disputed": disp,
                                     "contradicted": contra, "fabricated_citations": fab})
    out = []
    for a in agg.values():
        a["avg_trust"] = round(a.pop("trust_sum") / a["checks"]) if a["checks"] else None
        out.append(_rates(a))
    out.sort(key=lambda x: (x["checks"] < RATING_MIN_CHECKS, -(x["reliability"] or 0)))
    return {"min_checks": RATING_MIN_CHECKS, "models": out}


def _rates(a: dict) -> dict:
    """Достоверность = доля подтверждённых среди проверяемых утверждений;
    ошибки = опровергнутые + выдуманные ссылки на 100 утверждений."""
    judged = a["supported"] + a["disputed"] + a["contradicted"]
    a["reliability"] = round(a["supported"] / judged, 3) if judged else None
    a["errors_per_100"] = round(100 * (a["contradicted"] + a["fabricated_citations"]) / a["claims"], 1) if a["claims"] else None
    return a


def totals() -> dict:
    db = get_db()
    r = db.execute("""SELECT COUNT(*), COALESCE(SUM(claims),0), COALESCE(SUM(contradicted),0),
        COALESCE(SUM(disputed),0), COALESCE(SUM(fabricated_citations),0), COALESCE(AVG(elapsed_ms),0),
        COALESCE(SUM(cost_usd),0), COALESCE(SUM(CASE WHEN llm_calls > 0 THEN 1 ELSE 0 END),0),
        COALESCE(SUM(CASE WHEN llm_calls > 0 THEN cost_usd ELSE 0 END),0),
        COALESCE(SUM(CASE WHEN contradicted > 0 OR fabricated_citations > 0 THEN 1 ELSE 0 END),0)
        FROM checks""")[0]
    by_channel = {k: int(v) for k, v in db.execute("SELECT channel, COUNT(*) FROM checks GROUP BY channel")}
    by_lang = {k: int(v) for k, v in db.execute("SELECT lang, COUNT(*) FROM checks GROUP BY lang")}
    checks, claims, contradicted, disputed, fabricated, avg_ms, cost, paid_checks, paid_cost, with_errors = \
        [float(x) if isinstance(x, (int, float)) or x is None else float(x) for x in r]
    checks, claims, contradicted, disputed, fabricated = map(int, (checks, claims, contradicted, disputed, fabricated))
    paid_checks, with_errors = int(paid_checks), int(with_errors)
    classes = db.execute("SELECT COUNT(*) FROM classes")[0][0]
    fb = feedback_totals()

    base = _baseline()
    if base:  # история до переезда на постоянную базу
        b_checks = int(base.get("checks", 0))
        total_ms = avg_ms * checks + float(base.get("avg_seconds", 0)) * 1000 * b_checks
        checks += b_checks
        avg_ms = total_ms / checks if checks else 0
        claims += int(base.get("claims_checked", 0))
        b_fab = int(base.get("fabricated_citations", 0))
        contradicted += int(base.get("errors_caught", 0)) - b_fab
        fabricated += b_fab
        disputed += int(base.get("disputed", 0))
        with_errors += int(base.get("answers_with_errors", 0))
        cost += float(base.get("total_cost_usd", 0))
        paid_checks += b_checks
        paid_cost += float(base.get("total_cost_usd", 0))
        for k, v in (base.get("by_channel") or {}).items():
            by_channel[k] = by_channel.get(k, 0) + int(v)
        for k, v in (base.get("by_lang") or {}).items():
            by_lang[k] = by_lang.get(k, 0) + int(v)
        bf = base.get("feedback") or {}
        fb["answers"] += int(bf.get("answers", 0))
        fb["useful_yes"] += int(bf.get("useful_yes", 0))
        fb["useful_total"] += int(bf.get("useful_total", 0))
        fb["would_not_notice"] += int(bf.get("would_not_notice", 0))
        fb["would_notice_total"] += int(bf.get("would_notice_total", 0))
        fb["share_useful"] = round(fb["useful_yes"] / fb["useful_total"], 3) if fb["useful_total"] else 0
        fb["share_would_not_notice"] = (round(fb["would_not_notice"] / fb["would_notice_total"], 3)
                                        if fb["would_notice_total"] else 0)
    return {
        "checks": checks, "claims_checked": claims, "errors_caught": contradicted + fabricated,
        "disputed": disputed, "fabricated_citations": fabricated,
        "answers_with_errors": with_errors,
        "share_answers_with_errors": round(with_errors / checks, 3) if checks else 0,
        "avg_seconds": round(avg_ms / 1000, 1),
        "total_cost_usd": round(cost, 4),
        "avg_cost_per_check_usd": round(paid_cost / paid_checks, 5) if paid_checks else 0,
        "by_channel": by_channel, "by_lang": by_lang,
        "classes": int(classes),
        "storage": db.kind,
        "includes_history_before_db": bool(base),
        "feedback": fb,
    }


def _baseline() -> dict:
    raw = get_settings().stats_baseline.strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        logging.getLogger("senim.stats").warning("STATS_BASELINE: неверный JSON — не учитываю")
        return {}
