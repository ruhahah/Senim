"""Тренажёр «Найди ложь ИИ».

Senim берёт проверенные справки из учебной базы и просит ИИ написать короткий «ответ ИИ»,
в который НАМЕРЕННО внесены 1–3 правдоподобные ошибки (дата, место, имя, число).
Ученик ищет ошибки, потом видит правильные ответы с цитатой из справки.

Где правда — известно заранее: ошибки закладываются по справке из базы, а цитату
с правильным фактом код проверяет в тексте справки (как и в обычной проверке).
Текст тренажёра всегда помечен как учебный — это не настоящий ответ ИИ.
"""
from __future__ import annotations

import json
import logging
import random
import re
import secrets
import time

from . import classroom
from .db import get_db
from .llm import LLMError, get_router
from .pipeline.search import load_kb, search_kb
from .pipeline.text_utils import quote_in_text

log = logging.getLogger("senim.trainer")

LANG_NAME = {"kk": "казахском", "ru": "русском", "en": "английском"}

SYSTEM = """Ты составляешь учебное упражнение «Найди ошибку ИИ» для школьников.
Тебе дают проверенные ФАКТЫ. Напиши правдоподобный ответ чат-бота из {n_sent} коротких предложений
на {lang_name} языке по этим фактам — так, как ИИ отвечал бы на вопрос ученика.

Ровно {n_err} предложений должны содержать ОДНУ правдоподобную фактическую ошибку каждое:
изменённый год, место, имя, число или авторство. Ошибка должна противоречить ФАКТАМ,
но звучать уверенно и естественно. Остальные предложения — строго по ФАКТАМ, без выдумок.
Не используй предложения, которые нельзя проверить по ФАКТАМ.

Верни ТОЛЬКО JSON:
{{"question": "вопрос ученика, на который это ответ (на том же языке)",
  "sentences": [
    {{"text": "предложение", "false": false}},
    {{"text": "предложение с ошибкой", "false": true,
      "correct": "как правильно (коротко, на том же языке)",
      "quote": "ДОСЛОВНЫЙ фрагмент из ФАКТОВ, который доказывает правильный вариант"}}
  ]}}"""


class TrainerError(Exception):
    pass


def _pick_entries(topic: str, lang: str) -> list[dict]:
    if topic.strip():
        hits = search_kb([topic], limit=2, min_score=0.2)
        if hits:
            by_url = {f"senim-kb://{e['id']}": e for e in load_kb()}
            return [by_url[h["url"]] for h in hits if h["url"] in by_url]
    pool = [e for e in load_kb() if e.get("lang") == lang] or load_kb()
    if not pool:
        raise TrainerError("База знаний пуста")
    return [random.choice(pool)]


def _validate(data: dict, facts: str, n_err: int) -> dict:
    sents = [s for s in (data or {}).get("sentences", []) if isinstance(s, dict) and str(s.get("text", "")).strip()]
    if len(sents) < 3:
        raise TrainerError("слишком короткий текст")
    false_ = [s for s in sents if s.get("false")]
    if len(false_) != n_err:
        raise TrainerError(f"ожидалось {n_err} ошибок, получено {len(false_)}")
    out = []
    for s in sents:
        item = {"text": str(s["text"]).strip()[:400], "false": bool(s.get("false"))}
        if item["false"]:
            item["correct"] = str(s.get("correct") or "").strip()[:300]
            q = str(s.get("quote") or "").strip()
            # цитату проверяет код: если её нет в справке — не показываем её как доказательство
            item["quote"] = q[:300] if q and quote_in_text(q, facts) else ""
            if not item["correct"]:
                raise TrainerError("нет правильного варианта")
        out.append(item)
    if sum(1 for s in out if s["false"] and s["quote"]) < n_err:
        raise TrainerError("цитата не найдена в справке")
    return {"question": str(data.get("question") or "").strip()[:200], "sentences": out}


async def generate(topic: str, lang: str, n_err: int, n_sent: int = 6) -> dict:
    entries = _pick_entries(topic, lang)
    facts = "\n".join(f"{e['title']}: {e['text']}" for e in entries)
    router = get_router()
    system = SYSTEM.format(n_sent=n_sent, n_err=n_err, lang_name=LANG_NAME.get(lang, "русском"))
    last: Exception | None = None
    for attempt in range(3):
        try:
            data = await router.complete_json(system, f"ФАКТЫ:\n{facts}", cache_ns=f"trainer:{attempt}:{secrets.token_hex(4)}")
            payload = _validate(data, facts, n_err)
            payload.update({"sources": [{"title": e["title"], "text": e["text"]} for e in entries],
                            "n_errors": n_err, "lang": lang, "topic": entries[0]["title"]})
            return payload
        except (TrainerError, LLMError, KeyError, TypeError, ValueError) as e:
            last = e
            log.warning("trainer generation attempt %d failed: %s", attempt + 1, e)
    raise TrainerError(f"Не удалось составить упражнение: {last}")


# ------------------------------------------------------------------ хранение раундов
def save_round(payload: dict, code: str = "") -> str:
    rid = secrets.token_urlsafe(6).replace("-", "a").replace("_", "b")
    get_db().execute("INSERT INTO trainer_rounds VALUES (?,?,?,?,?,?)",
                     (rid, classroom.normalize_code(code) or None, time.time(), payload["lang"], payload["topic"],
                      json.dumps(payload, ensure_ascii=False)))
    return rid


def load_round(rid: str) -> dict | None:
    if not re.fullmatch(r"[A-Za-z0-9]{4,16}", rid or ""):
        return None
    rows = get_db().execute("SELECT id, code, created, payload FROM trainer_rounds WHERE id = ?", (rid,))
    if not rows:
        return None
    rid, code, created, payload = rows[0]
    return {"id": rid, "code": code or "", "created": created, **json.loads(payload)}


def reuse_round(lang: str, topic: str) -> str | None:
    """Для одиночной игры берём уже составленный раунд (без класса), если их накопилось достаточно —
    так тренажёр почти ничего не стоит."""
    rows = get_db().execute("SELECT id, topic FROM trainer_rounds WHERE code IS NULL AND lang = ?", (lang,))
    if topic.strip():
        t = topic.strip().lower()
        rows = [r for r in rows if t in (r[1] or "").lower() or (r[1] or "").lower() in t]
    if len(rows) >= 4 and random.random() < 0.8:
        return random.choice(rows)[0]
    return None


def public_view(r: dict) -> dict:
    """То, что видит ученик ДО ответа: без пометок, где ошибка."""
    cls = classroom.get_class(r["code"]) if r.get("code") else None
    return {"id": r["id"], "lang": r["lang"], "topic": r["topic"], "question": r.get("question", ""),
            "n_errors": r["n_errors"], "sentences": [s["text"] for s in r["sentences"]],
            "class_name": cls["name"] if cls else ""}


def score_answer(r: dict, marked: list[int], seconds: int) -> dict:
    marked_set = {i for i in marked if 0 <= i < len(r["sentences"])}
    wrong = {i for i, s in enumerate(r["sentences"]) if s["false"]}
    correct = len(marked_set & wrong)
    missed = len(wrong - marked_set)
    false_alarms = len(marked_set - wrong)
    score = max(0, 100 * correct - 50 * false_alarms)
    if missed == 0 and false_alarms == 0:
        score += max(0, 90 - int(seconds))  # бонус за скорость, только за идеальный раунд
    return {"correct": correct, "missed": missed, "false_alarms": false_alarms, "score": score,
            "total": len(wrong),
            "reveal": [{"text": s["text"], "false": s["false"], "marked": i in marked_set,
                        "correct": s.get("correct", ""), "quote": s.get("quote", "")}
                       for i, s in enumerate(r["sentences"])],
            "sources": r.get("sources", [])}


def record_result(r: dict, student: str, res: dict, seconds: int) -> None:
    marked = [i for i, s in enumerate(res.get("reveal", [])) if s.get("marked")]
    get_db().execute(
        "INSERT INTO trainer_results (ts, round_id, code, student, correct, missed, false_alarms, seconds, score, marked) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (time.time(), r["id"], r.get("code") or None, classroom.clean_student(student),
         res["correct"], res["missed"], res["false_alarms"], int(seconds), res["score"], json.dumps(marked)))


def missed_errors(rid: str) -> list[dict]:
    """Какие спрятанные ошибки ученики класса пропустили — для разбора на уроке.
    Считаем последнюю попытку каждого ученика."""
    r = load_round(rid)
    if not r:
        return []
    rows = get_db().execute("SELECT student, marked FROM trainer_results WHERE round_id = ? ORDER BY ts", (rid,))
    last: dict[str, set[int]] = {}
    for student, marked in rows:
        if marked is None:  # результаты до появления колонки — без отметок
            continue
        try:
            last[student] = {int(i) for i in json.loads(marked)}
        except (TypeError, ValueError):
            continue
    out = []
    for i, s in enumerate(r["sentences"]):
        if not s.get("false"):
            continue
        missed_by = [st for st, m in last.items() if i not in m]
        out.append({"text": s["text"], "correct": s.get("correct", ""), "missed": len(missed_by),
                    "of": len(last), "students": missed_by[:10]})
    return out


def leaderboard(rid: str, limit: int = 20) -> list[dict]:
    rows = get_db().execute("""SELECT student, score, correct, missed, false_alarms, seconds
        FROM trainer_results WHERE round_id = ? ORDER BY score DESC, seconds ASC""", (rid,))
    best: dict[str, dict] = {}
    for student, score, correct, missed, fa, sec in rows:  # лучший результат каждого участника
        if student not in best:
            best[student] = {"student": student, "score": score, "correct": correct, "missed": missed,
                             "false_alarms": fa, "seconds": sec}
    return list(best.values())[:limit]


def class_rounds(code: str) -> list[dict]:
    db = get_db()
    rounds = db.execute("SELECT id, created, lang, topic FROM trainer_rounds WHERE code = ? ORDER BY created DESC",
                        (classroom.normalize_code(code),))
    out = []
    for rid, created, lang, topic in rounds[:20]:
        res = db.execute("SELECT correct, missed FROM trainer_results WHERE round_id = ?", (rid,))
        found = sum(c for c, _ in res)
        total = sum(c + m for c, m in res)
        out.append({"id": rid, "created": created, "lang": lang, "topic": topic, "plays": len(res),
                    "found_share": round(found / total, 2) if total else None,
                    "leaderboard": leaderboard(rid, 5), "errors": missed_errors(rid)})
    return out
