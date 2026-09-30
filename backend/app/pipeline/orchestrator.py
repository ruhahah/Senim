"""Весь конвейер Senim. Отдаёт события по мере готовности (для «живой» подсветки в UI)."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import AsyncIterator

from rapidfuzz import fuzz

from .. import classroom, stats
from ..config import get_settings
from ..llm import LLMError, LLMNotConfigured, get_router, new_usage
from ..schemas import (Claim, ClaimResult, ClaimType, CitationResult, CitationStatus, Status)
from . import citations as cit
from .scoring import compute_trust, is_fabricated
from .search import gather_evidence, http_client
from .text_utils import detect_lang, normalize, split_sentences
from .verify import (PER_SENTENCE_MAX, extract_claims, extract_sentence_claims, judge_claim, sentence_fallback_claim,
                     worth_checking)

log = logging.getLogger("senim.pipeline")
MAX_SENTENCES = 16  # длиннее ответ — проверяем первые 16 предложений

CITE_MSG = {
    "verified": {"kk": "Бұл дереккөз ғылыми базаларда бар, деректері сәйкес келеді.",
                 "ru": "Этот источник существует в научных базах, данные совпадают.",
                 "en": "This source exists in scholarly databases and the details match."},
    "mismatch": {"kk": "Дереккөз табылды, бірақ авторы, жылы немесе атауы бұрмаланған.",
                 "ru": "Источник найден, но автор, год или название искажены.",
                 "en": "The source was found, but the author, year or title is distorted."},
    "fabricated": {"kk": "Мұндай DOI жоқ немесе ол мүлде басқа жұмысқа тиесілі — дереккөз ойдан шығарылған болуы әбден мүмкін.",
                   "ru": "Такого DOI не существует или он принадлежит совсем другой работе — источник почти наверняка выдуман.",
                   "en": "This DOI doesn't exist or belongs to a different work — the source is very likely fabricated."},
    "not_found": {"kk": "Ғылыми базалардан табылмады. Кітап немесе қазақ тіліндегі дереккөз болуы мүмкін — кітапханадан тексеріңіз.",
                  "ru": "Не найдено в научных базах. Это может быть книга или казахоязычный источник — проверьте вручную.",
                  "en": "Not found in scholarly databases. It may be a book or a local source — verify manually."},
}


def _cm(key: str, lang: str) -> str:
    return CITE_MSG[key].get(lang, CITE_MSG[key]["ru"])


def _match_citation(claim: Claim, results: list[CitationResult]) -> CitationResult | None:
    target = normalize(claim.span or claim.text)
    best, best_s = None, 0.0
    for r in results:
        s = max(fuzz.partial_ratio(normalize(r.citation.raw), target),
                fuzz.partial_ratio(normalize(r.citation.title), target) if r.citation.title else 0)
        if s > best_s:
            best, best_s = r, s
    return best if best_s >= 60 else None


def citation_claim_result(claim: Claim, r: CitationResult | None, lang: str) -> ClaimResult:
    if r is None or r.status in (CitationStatus.not_found, CitationStatus.error):
        return ClaimResult(claim_id=claim.id, status=Status.unverifiable, error_type="no_evidence",
                           explanation=_cm("not_found", lang))
    if is_fabricated(r):
        return ClaimResult(claim_id=claim.id, status=Status.contradicted, error_type="fabricated_source",
                           explanation=_cm("fabricated", lang))
    if r.status == CitationStatus.mismatch:
        return ClaimResult(claim_id=claim.id, status=Status.disputed, error_type="wrong_entity",
                           explanation=_cm("mismatch", lang), correction=r.matched_title)
    return ClaimResult(claim_id=claim.id, status=Status.supported, explanation=_cm("verified", lang))


async def run_check(text: str, question: str = "", ui_lang: str | None = None,
                    channel: str = "web", class_code: str = "", student: str = "",
                    source_ai: str = "") -> AsyncIterator[dict]:
    t0 = time.perf_counter()
    usage = new_usage()  # токены и стоимость именно этой проверки
    lang = detect_lang(text)
    out_lang = ui_lang or lang
    sentences = split_sentences(text)
    router = get_router()
    yield {"type": "start", "lang": lang, "sentences": [s.model_dump(mode="json") for s in sentences],
           "provider": router.label, "llm_available": router.available}

    async with http_client() as client:
        # ссылки проверяем параллельно с извлечением утверждений
        async def citations_task() -> list[CitationResult]:
            # отдельный вызов ИИ за ссылками нужен, только если в тексте похоже есть литература
            use_llm = router.available and cit.may_have_citations(text)
            cites = await cit.extract_citations(text, router if use_llm else None)
            return await cit.check_citations(client, cites) if cites else []

        cit_future = asyncio.create_task(citations_task())

        claims: list[Claim] = []
        results: list[ClaimResult] = []

        async def check_one(c: Claim) -> ClaimResult:
            if c.type == ClaimType.citation:
                cres = await cit_future
                return citation_claim_result(c, _match_citation(c, cres), out_lang)
            ev = await gather_evidence(c, lang, client)
            try:
                return await judge_claim(router, c, ev, out_lang)
            except LLMError as e:
                log.warning("judge failed for claim %s: %s", c.id, e)
                return ClaimResult(claim_id=c.id, status=Status.unverifiable, flags=["check_failed"],
                                   explanation=str(e)[:200])

        if not router.available:
            yield {"type": "error", "code": "llm_not_configured",
                   "message": "ИИ-провайдер не настроен: добавьте ключ в .env"}
        elif get_settings().extract_mode == "whole":
            # старый режим: одно извлечение на весь ответ, затем параллельная проверка утверждений
            try:
                claims = await extract_claims(router, text, lang, sentences, question)
                yield {"type": "claims", "claims": [c.model_dump(mode="json") for c in claims]}
            except LLMNotConfigured as e:
                yield {"type": "error", "code": "llm_not_configured", "message": str(e)}
            except LLMError as e:
                yield {"type": "error", "code": "llm_failed", "message": str(e)}
            for fut in asyncio.as_completed([asyncio.create_task(check_one(c)) for c in claims]):
                r = await fut
                results.append(r)
                yield {"type": "claim_result", "result": r.model_dump(mode="json")}
        else:
            # Каждое предложение идёт своим потоком: извлечь утверждения → найти источник → вердикт.
            # Первое предложение не ждёт остальных, поэтому первый результат появляется через секунды.
            queue: asyncio.Queue = asyncio.Queue()
            todo = [sn for sn in sentences if worth_checking(sn)][:MAX_SENTENCES]
            max_claims = get_settings().max_claims
            budget = {"left": max_claims}
            failures: list[str] = []

            async def sentence_flow(order: int, sent) -> None:
                first_id = order * PER_SENTENCE_MAX + 1
                try:
                    found = await extract_sentence_claims(router, text, lang, sent, first_id, question)
                except LLMNotConfigured as e:
                    failures.append(str(e))
                    found = []
                except LLMError as e:
                    log.warning("extract failed for sentence %s: %s", sent.index, e)
                    failures.append(str(e))
                    found = [sentence_fallback_claim(sent, first_id)]
                found = found[: max(0, budget["left"])]
                budget["left"] -= len(found)
                if not found:
                    return
                await queue.put(("claims", found))

                async def one(c: Claim) -> None:
                    await queue.put(("result", await check_one(c)))

                await asyncio.gather(*(one(c) for c in found))

            async def run_all() -> None:
                try:
                    await asyncio.gather(*(sentence_flow(i, sn) for i, sn in enumerate(todo)))
                finally:
                    await queue.put(("end", None))

            runner = asyncio.create_task(run_all())
            while True:
                kind, payload = await queue.get()
                if kind == "end":
                    break
                if kind == "claims":
                    claims.extend(payload)
                    yield {"type": "claims", "claims": [c.model_dump(mode="json") for c in payload]}
                else:
                    results.append(payload)
                    yield {"type": "claim_result", "result": payload.model_dump(mode="json")}
            await runner
            if failures and not claims:
                yield {"type": "error", "code": "llm_failed", "message": failures[0]}
            claims.sort(key=lambda c: c.id)

        cit_results = await cit_future
        yield {"type": "citations", "results": [r.model_dump(mode="json") for r in cit_results]}

    trust = compute_trust(claims, results, cit_results)
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    trust_json = trust.model_dump(mode="json")
    try:
        if (claims or cit_results) and channel != "warmup":  # пустые прогоны и прогрев не считаем
            stats.record(channel, lang, trust_json, elapsed_ms, usage, source_ai)
    except Exception as e:  # noqa: BLE001 — статистика не должна ломать проверку
        log.warning("stats not recorded: %s", e)
    if class_code and (claims or cit_results):
        try:
            classroom.record_check(class_code, student, channel, lang, text,
                                   [c.model_dump(mode="json") for c in claims],
                                   [r.model_dump(mode="json") for r in results], trust_json)
        except Exception as e:  # noqa: BLE001
            log.warning("class check not recorded: %s", e)
    yield {"type": "done", "trust": trust_json, "elapsed_ms": elapsed_ms,
           "usage": {k: (round(v, 6) if isinstance(v, float) else v) for k, v in usage.items()}}


async def run_check_full(text: str, question: str = "", ui_lang: str | None = None,
                         channel: str = "api", class_code: str = "", student: str = "",
                         source_ai: str = "") -> dict:
    """То же самое, но одним JSON (для бенчмарка, API и бота)."""
    out: dict = {"claims": [], "results": [], "citations": [], "errors": []}
    async for ev in run_check(text, question, ui_lang, channel, class_code, student, source_ai):
        t = ev["type"]
        if t == "start":
            out.update(lang=ev["lang"], sentences=ev["sentences"], provider=ev["provider"])
        elif t == "claims":
            out["claims"].extend(ev["claims"])
        elif t == "claim_result":
            out["results"].append(ev["result"])
        elif t == "citations":
            out["citations"] = ev["results"]
        elif t == "error":
            out["errors"].append(ev)
        elif t == "done":
            out.update(trust=ev["trust"], elapsed_ms=ev["elapsed_ms"], usage=ev.get("usage"))
    out["claims"].sort(key=lambda c: c["id"])
    out["results"].sort(key=lambda r: r["claim_id"])
    return out
