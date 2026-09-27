"""Извлечение утверждений и вердикт по каждому.

Три «якоря» доверия (чтобы это не было «ИИ проверяет ИИ»):
 1. Вердикт выносится ТОЛЬКО по найденным доказательствам.
 2. Цитата из доказательства проверяется программно — нет цитаты, нет вердикта.
 3. (опционально) NLI-модель даёт независимое второе мнение.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from ..config import get_settings
from ..llm import LLMRouter
from ..prompts import (EXTRACT_SYSTEM, EXTRACT_USER, LANG_NAMES, QUOTE_REPAIR_SYSTEM, QUOTE_REPAIR_USER,
                       VERDICT_SYSTEM, VERDICT_USER)
from ..schemas import Claim, ClaimResult, ClaimType, Evidence, RawVerdict, Sentence, Status
from . import nli
from .text_utils import locate_sentence, quote_in_text

log = logging.getLogger("senim.verify")

HARD_ERRORS = {"wrong_date", "wrong_number", "wrong_entity", "fabricated_source"}
VALID_ERRORS = {"wrong_date", "wrong_number", "wrong_entity", "fabricated_source",
                "outdated", "overgeneralization", "no_evidence", "none"}

MSG = {
    "opinion": {
        "kk": "Бұл — пікір немесе баға. Оны фактілермен дәлелдеу не жоққа шығару мүмкін емес.",
        "ru": "Это мнение или оценка. Его нельзя подтвердить или опровергнуть фактами.",
        "en": "This is an opinion or evaluation. Facts can't prove or disprove it.",
    },
    "no_evidence": {
        "kk": "Сенімді дереккөздерден бұл туралы ақпарат табылмады. Бұл жалған дегенді білдірмейді, бірақ сенуге әлі ерте.",
        "ru": "В надёжных источниках не нашлось информации об этом. Это не значит, что утверждение ложное, но доверять ему пока рано.",
        "en": "No information about this was found in reliable sources. That doesn't make it false, but it's too early to trust it.",
    },
    "quote_unverified": {
        "kk": "Тексеруші дәйексөз келтірді, бірақ ол дереккөз мәтінінде табылмады — сондықтан нәтиже күмәнді.",
        "ru": "Проверяющая модель привела цитату, но её нет в тексте источника — поэтому результат под сомнением.",
        "en": "The checker quoted a source, but that quote isn't in the source text — so the result is uncertain.",
    },
    "models_disagree": {
        "kk": "Екі тәуелсіз тексеру әртүрлі нәтиже берді — өзіңіз тексеріңіз.",
        "ru": "Две независимые проверки разошлись во мнении — лучше проверить самому.",
        "en": "Two independent checks disagree — better verify it yourself.",
    },
    "how_to_check": {
        "kk": "Кемінде екі сенімді дереккөзден іздеңіз: оқулық, энциклопедия немесе ресми сайт.",
        "ru": "Поищите в двух надёжных источниках: учебник, энциклопедия или официальный сайт.",
        "en": "Look it up in two reliable sources: a textbook, an encyclopedia or an official website.",
    },
}


def _msg(key: str, lang: str) -> str:
    return MSG[key].get(lang, MSG[key]["ru"])


# ------------------------------------------------------------------ extraction
async def extract_claims(router: LLMRouter, text: str, lang: str, sentences: list[Sentence],
                         question: str = "") -> list[Claim]:
    s = get_settings()
    system = EXTRACT_SYSTEM.format(max_claims=s.max_claims)
    qb = f"ВОПРОС ПОЛЬЗОВАТЕЛЯ: {question}\n" if question.strip() else ""
    user = EXTRACT_USER.format(lang=LANG_NAMES.get(lang, lang), question_block=qb, text=text)
    data = await router.complete_json(system, user, cache_ns="extract")
    raw = data.get("claims", []) if isinstance(data, dict) else data
    claims: list[Claim] = []
    for i, c in enumerate(raw[: s.max_claims]):
        if not isinstance(c, dict) or not str(c.get("text", "")).strip():
            continue
        ctype = c.get("type", "fact")
        try:
            ctype = ClaimType(ctype)
        except ValueError:
            ctype = ClaimType.fact
        try:
            imp = min(3, max(1, int(c.get("importance", 2))))
        except (TypeError, ValueError):
            imp = 2
        claim = Claim(
            id=len(claims) + 1, text=str(c["text"]).strip(), span=str(c.get("span", "")).strip(),
            type=ctype, importance=imp,
            search_queries=[str(q) for q in (c.get("search_queries") or []) if str(q).strip()][:3],
        )
        claim.sentence_index = locate_sentence(claim.span or claim.text, sentences)
        claims.append(claim)
    return fill_uncovered(claims, sentences, s.max_claims)


OPINION_MARKERS = re.compile(
    r"(менің ойымша|меніңше|ойымша|по-моему|по моему мнению|я думаю|я считаю|на мой взгляд|"
    r"in my opinion|i think|i believe|the (greatest|best)|самый великий|ең ұлы|ең үздік)", re.IGNORECASE)
REF_MARKERS = re.compile(r"(10\.\d{4,9}/|\(\s*(1[5-9]|20)\d{2}\s*\)|et al\.)", re.IGNORECASE)


def fill_uncovered(claims: list[Claim], sentences: list[Sentence], max_claims: int) -> list[Claim]:
    """Страховка от «ленивой» модели: предложение без единого утверждения проверяем целиком.
    Так ни один факт из ответа не останется без проверки."""
    covered = {c.sentence_index for c in claims if c.sentence_index is not None}
    for sent in sentences:
        if len(claims) >= max_claims:
            break
        text = sent.text.strip()
        if sent.index in covered or len(text) < 15 or not re.search(r"\w{3,}", text) or text.endswith(":"):
            continue
        ctype = (ClaimType.citation if REF_MARKERS.search(text)
                 else ClaimType.opinion if OPINION_MARKERS.search(text) else ClaimType.fact)
        claims.append(Claim(id=len(claims) + 1, text=text, span=text, type=ctype, importance=2,
                            search_queries=[text[:120]], sentence_index=sent.index))
    return claims


# ------------------------------------------------------------------ verdict
def _find_quote(quote: str, ev: Optional[Evidence], evidence: list[Evidence]) -> tuple[Optional[Evidence], bool]:
    """Ищем цитату сначала в указанном доказательстве, потом во всех."""
    if not quote:
        return ev, False
    if ev and quote_in_text(quote, ev.text):
        return ev, True
    for e in evidence:
        if quote_in_text(quote, e.text):
            return e, True
    return ev, False


def format_evidence(evidence: list[Evidence]) -> str:
    return "\n\n".join(f"[{e.id}] ({e.source}) {e.title} — {e.url}\n{e.text}" for e in evidence)


def map_status(raw: RawVerdict, quote_ok: bool) -> tuple[Status, list[str]]:
    flags: list[str] = []
    if raw == RawVerdict.NOT_ENOUGH_EVIDENCE:
        return Status.unverifiable, flags
    if raw == RawVerdict.PARTIAL:
        if not quote_ok:
            flags.append("quote_unverified")
        return Status.disputed, flags
    if not quote_ok:
        flags.append("quote_unverified")
        return Status.disputed, flags
    return (Status.supported if raw == RawVerdict.SUPPORTED else Status.contradicted), flags


async def judge_claim(router: LLMRouter, claim: Claim, evidence: list[Evidence], lang: str) -> ClaimResult:
    if claim.type in (ClaimType.opinion, ClaimType.advice):
        return ClaimResult(claim_id=claim.id, status=Status.unverifiable, explanation=_msg("opinion", lang),
                           how_to_check=_msg("how_to_check", lang))
    if not evidence:
        return ClaimResult(claim_id=claim.id, status=Status.unverifiable, raw_verdict=RawVerdict.NOT_ENOUGH_EVIDENCE,
                           error_type="no_evidence", explanation=_msg("no_evidence", lang),
                           how_to_check=_msg("how_to_check", lang))

    system = VERDICT_SYSTEM.format(lang=LANG_NAMES.get(lang, lang))
    user = VERDICT_USER.format(claim=claim.text, evidence=format_evidence(evidence))
    data = await router.complete_json(system, user, cache_ns="verdict")
    if not isinstance(data, dict):
        data = {}

    try:
        raw = RawVerdict(str(data.get("status", "")).upper())
    except ValueError:
        raw = RawVerdict.NOT_ENOUGH_EVIDENCE
    err = data.get("error_type") or "none"
    err = err if err in VALID_ERRORS else "none"
    quote = str(data.get("quote") or "").strip()

    ev: Optional[Evidence] = None
    try:
        ev_id = int(data.get("evidence_id")) if data.get("evidence_id") is not None else None
    except (TypeError, ValueError):
        ev_id = None
    if ev_id is not None:
        ev = next((e for e in evidence if e.id == ev_id), None)
    ev, quote_ok = _find_quote(quote, ev, evidence)
    rejected_quote = ""
    # модель вынесла вердикт, но цитата не нашлась дословно — просим один раз скопировать точно
    if not quote_ok and raw in (RawVerdict.SUPPORTED, RawVerdict.CONTRADICTED, RawVerdict.PARTIAL):
        rejected_quote = quote
        try:
            fix = await router.complete_json(
                QUOTE_REPAIR_SYSTEM,
                QUOTE_REPAIR_USER.format(claim=claim.text, bad_quote=quote or "(пусто)",
                                         evidence=format_evidence(evidence)),
                cache_ns="quote_repair")
            new_quote = str((fix or {}).get("quote") or "").strip() if isinstance(fix, dict) else ""
            try:
                fix_id = int(fix.get("evidence_id")) if isinstance(fix, dict) and fix.get("evidence_id") else None
            except (TypeError, ValueError):
                fix_id = None
            fix_ev = next((e for e in evidence if e.id == fix_id), ev)
            ev2, ok2 = _find_quote(new_quote, fix_ev, evidence)
            if ok2:
                ev, quote, quote_ok, rejected_quote = ev2, new_quote, True, ""
        except Exception as e:  # noqa: BLE001 — ремонт цитаты необязателен
            log.info("quote repair failed: %s", e)

    # Модели часто пишут PARTIAL, хотя сами же называют конкретную ошибку («ошибся в году»).
    # Правило Senim: неверная дата/число/имя, подтверждённая цитатой из источника → опровергнуто.
    if raw == RawVerdict.PARTIAL and quote_ok and err in HARD_ERRORS:
        raw = RawVerdict.CONTRADICTED

    status, flags = map_status(raw, quote_ok)
    explanation = str(data.get("explanation") or "").strip()
    if "quote_unverified" in flags:
        explanation = (explanation + " " + _msg("quote_unverified", lang)).strip()
    if raw == RawVerdict.NOT_ENOUGH_EVIDENCE and not explanation:
        explanation = _msg("no_evidence", lang)

    result = ClaimResult(
        claim_id=claim.id, status=status, raw_verdict=raw,
        error_type=err if status != Status.supported else "none",
        quote=quote if quote_ok else "", quote_verified=quote_ok, evidence=ev,
        explanation=explanation, correction=str(data.get("correction") or "").strip(),
        how_to_check=str(data.get("how_to_check") or "").strip() or _msg("how_to_check", lang),
        flags=flags, rejected_quote=rejected_quote,
    )

    # второе мнение NLI (если включено)
    if ev and nli.enabled() and raw in (RawVerdict.SUPPORTED, RawVerdict.CONTRADICTED):
        label = await nli.classify(premise=ev.text, hypothesis=claim.text)
        if label:
            result.nli_label = label
            agrees = not (
                (raw == RawVerdict.SUPPORTED and label == "contradiction")
                or (raw == RawVerdict.CONTRADICTED and label == "entailment")
            )
            result.nli_agrees = agrees
            if not agrees:
                result.status = Status.disputed
                result.flags.append("models_disagree")
                result.explanation = (result.explanation + " " + _msg("models_disagree", lang)).strip()
    return result
