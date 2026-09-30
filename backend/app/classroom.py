"""Режим учителя: класс по коду.

Учитель создаёт класс → получает КОД (для учеников) и секретный КЛЮЧ (для своей панели).
Ученик открывает сайт по ссылке с кодом (или вводит код / пишет боту /class КОД) —
его проверки попадают в панель учителя: кто сколько проверил, какие ошибки ИИ встречаются
чаще всего и сколько ошибок ученики находят САМИ в режиме «Сначала подумай».

Храним только то, что нужно учителю: имя, которое ученик ввёл сам, начало проверенного
текста и спорные/ложные утверждения. Никаких e-mail, телефонов и паролей.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from collections import Counter, defaultdict

from .db import get_db

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # без похожих 0/O, 1/I
CODE_RE = re.compile(r"^[A-Z2-9]{6}$")
PROBLEM = {"contradicted", "disputed"}


def normalize_code(code: str | None) -> str:
    c = (code or "").strip().upper().replace("-", "").replace(" ", "")
    return c if CODE_RE.match(c) else ""


def clean_student(name: str | None) -> str:
    name = re.sub(r"\s+", " ", (name or "").strip())
    return name[:40] or "—"


def _hash(key: str) -> str:
    return hashlib.sha256(("senim-class:" + key).encode()).hexdigest()


def create_class(name: str) -> dict:
    db = get_db()
    name = re.sub(r"\s+", " ", (name or "").strip())[:60] or "Класс"
    for _ in range(20):
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
        if not db.execute("SELECT 1 FROM classes WHERE code = ?", (code,)):
            break
    key = secrets.token_urlsafe(12)
    db.execute("INSERT INTO classes VALUES (?,?,?,?)", (code, _hash(key), name, time.time()))
    return {"code": code, "key": key, "name": name}


def get_class(code: str) -> dict | None:
    code = normalize_code(code)
    if not code:
        return None
    rows = get_db().execute("SELECT code, name, created, key_hash FROM classes WHERE code = ?", (code,))
    if not rows:
        return None
    c, name, created, key_hash = rows[0]
    return {"code": c, "name": name, "created": created, "_key_hash": key_hash}


def check_key(cls: dict, key: str) -> bool:
    return bool(key) and hmac.compare_digest(cls["_key_hash"], _hash(key))


# ------------------------------------------------------------------ запись
def record_check(code: str, student: str, channel: str, lang: str, text: str,
                 claims: list[dict], results: list[dict], trust: dict) -> None:
    """Сохраняет проверку ученика для панели учителя."""
    if not get_class(code):
        return
    by_id = {c["id"]: c for c in claims}
    problems = []
    for r in results:
        st = r.get("status")
        if st in PROBLEM:
            c = by_id.get(r.get("claim_id"), {})
            problems.append({"text": (c.get("text") or "")[:220], "status": st,
                             "error_type": r.get("error_type") or "",
                             "correction": (r.get("correction") or "")[:220]})
    cnt = trust.get("counts", {})
    get_db().execute(
        "INSERT INTO class_checks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (time.time(), normalize_code(code), clean_student(student), channel, lang,
         int(trust.get("trust_index") or 0), trust.get("band") or "",
         sum(int(cnt.get(k, 0)) for k in ("supported", "disputed", "contradicted", "unverifiable")),
         int(cnt.get("supported", 0)), int(cnt.get("disputed", 0)), int(cnt.get("contradicted", 0)),
         int(cnt.get("unverifiable", 0)), int(trust.get("fabricated_citations") or 0),
         re.sub(r"\s+", " ", text.strip())[:300], json.dumps(problems, ensure_ascii=False)))


def record_think(code: str, student: str, caught: int, missed: int, false_alarms: int) -> bool:
    """Итог режима «Сначала подумай»: сколько ошибок ИИ ученик нашёл сам до подсказки Senim."""
    if not get_class(code):
        return False
    get_db().execute("INSERT INTO class_think VALUES (?,?,?,?,?,?)",
                     (time.time(), normalize_code(code), clean_student(student),
                      max(0, int(caught)), max(0, int(missed)), max(0, int(false_alarms))))
    return True


# ------------------------------------------------------------------ Telegram
def tg_join(chat_id: int | str, code: str, student: str) -> dict | None:
    cls = get_class(code)
    if not cls:
        return None
    db = get_db()
    db.execute("DELETE FROM tg_members WHERE chat_id = ?", (str(chat_id),))
    db.execute("INSERT INTO tg_members VALUES (?,?,?,?)", (str(chat_id), cls["code"], clean_student(student), time.time()))
    return cls


def tg_leave(chat_id: int | str) -> None:
    get_db().execute("DELETE FROM tg_members WHERE chat_id = ?", (str(chat_id),))


def tg_membership(chat_id: int | str) -> tuple[str, str] | None:
    rows = get_db().execute("SELECT code, student FROM tg_members WHERE chat_id = ?", (str(chat_id),))
    return (rows[0][0], rows[0][1]) if rows else None


# ------------------------------------------------------------------ панель учителя
def dashboard(code: str) -> dict:
    db = get_db()
    code = normalize_code(code)
    rows = db.execute("""SELECT ts, student, channel, lang, trust, band, claims, supported, disputed,
        contradicted, unverifiable, fabricated_citations, snippet, problems
        FROM class_checks WHERE code = ? ORDER BY ts DESC""", (code,))
    think = db.execute("SELECT student, caught, missed, false_alarms FROM class_think WHERE code = ?", (code,))

    students: dict[str, dict] = defaultdict(lambda: {"checks": 0, "trust_sum": 0, "errors": 0, "disputed": 0,
                                                     "caught": 0, "missed": 0, "false_alarms": 0, "last": 0.0})
    problem_counter: Counter = Counter()
    problem_examples: dict[str, dict] = {}
    error_types: Counter = Counter()
    recent = []
    total_claims = total_errors = total_disputed = total_fab = 0
    trusts = []
    for (ts, student, channel, lang, trust, band, claims, sup, disp, contra, unv, fab, snippet, probs) in rows:
        st = students[student]
        st["checks"] += 1
        st["trust_sum"] += trust or 0
        st["errors"] += (contra or 0) + (fab or 0)
        st["disputed"] += disp or 0
        st["last"] = max(st["last"], ts)
        total_claims += claims or 0
        total_errors += (contra or 0) + (fab or 0)
        total_disputed += disp or 0
        total_fab += fab or 0
        trusts.append(trust or 0)
        plist = json.loads(probs or "[]")
        for p in plist:
            key = re.sub(r"\W+", " ", p["text"].lower()).strip()[:120]
            problem_counter[key] += 1
            problem_examples.setdefault(key, p)
            if p.get("error_type"):
                error_types[p["error_type"]] += 1
        if len(recent) < 40:
            recent.append({"ts": ts, "student": student, "channel": channel, "lang": lang, "trust": trust,
                           "band": band, "claims": claims, "supported": sup, "disputed": disp,
                           "contradicted": contra, "fabricated_citations": fab, "snippet": snippet,
                           "problems": plist[:6]})
    caught = missed = false_alarms = 0
    for student, c, m, fa in think:
        st = students[student]
        st["caught"] += c or 0
        st["missed"] += m or 0
        st["false_alarms"] += fa or 0
        caught += c or 0
        missed += m or 0
        false_alarms += fa or 0

    by_student = []
    for name, st in students.items():
        found = st["caught"] + st["missed"]
        by_student.append({
            "student": name, "checks": st["checks"],
            "avg_trust": round(st["trust_sum"] / st["checks"]) if st["checks"] else None,
            "errors": st["errors"], "disputed": st["disputed"],
            "self_found_share": round(st["caught"] / found, 2) if found else None,
            "think_rounds": found, "last": st["last"],
        })
    by_student.sort(key=lambda x: (-x["checks"], x["student"]))

    top = [{**problem_examples[k], "count": n} for k, n in problem_counter.most_common(10)]
    think_total = caught + missed
    return {
        "totals": {
            "checks": len(rows), "students": len([s for s in students.values() if s["checks"] or s["caught"] or s["missed"]]),
            "claims": total_claims, "errors": total_errors, "disputed": total_disputed,
            "fabricated_citations": total_fab,
            "avg_trust": round(sum(trusts) / len(trusts)) if trusts else None,
            "self_found_share": round(caught / think_total, 2) if think_total else None,
            "think_caught": caught, "think_missed": missed, "think_false_alarms": false_alarms,
        },
        "by_student": by_student,
        "top_problems": top,
        "error_types": dict(error_types.most_common()),
        "recent": recent,
    }
