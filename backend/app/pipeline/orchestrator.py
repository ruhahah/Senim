"""Весь конвейер Senim. Отдаёт события по мере готовности (для «живой» подсветки в UI)."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import AsyncIterator

from rapidfuzz import fuzz

from .. import stats
from ..llm import LLMError, LLMNotConfigured, get_router, new_usage
from ..schemas import (Claim, ClaimResult, ClaimType, CitationResult, CitationStatus, Status)
from . import citations as cit
from .scoring import compute_trust, is_fabricated
from .search import gather_evidence, http_client
from .text_utils import detect_lang, normalize, split_sentences
from .verify import extract_claims, judge_claim

log = logging.getLogger("senim.pipeline")

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
                    channel: str = "web") -> AsyncIterator[dict]:
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
            cites = await cit.extract_citations(text, router if router.available else None)
            return await cit.check_citations(client, cites) if cites else []

        cit_future = asyncio.create_task(citations_task())

        claims: list[Claim] = []
        try:
            claims = await extract_claims(router, text, lang, sentences, question)
            yield {"type": "claims", "claims": [c.model_dump(mode="json") for c in claims]}
        except LLMNotConfigured as e:
            yield {"type": "error", "code": "llm_not_configured", "message": str(e)}
        except LLMError as e:
            yield {"type": "error", "code": "llm_failed", "message": str(e)}

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

        tasks = [asyncio.create_task(check_one(c)) for c in claims]
        for fut in asyncio.as_completed(tasks):
            r = await fut
            results.append(r)
            yield {"type": "claim_result", "result": r.model_dump(mode="json")}

        cit_results = await cit_future
        yield {"type": "citations", "results": [r.model_dump(mode="json") for r in cit_results]}

    trust = compute_trust(claims, results, cit_results)
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    trust_json = trust.model_dump(mode="json")
    try:
        if claims or cit_results:  # пустые/неудачные прогоны в статистику эффекта не пишем
            stats.record(channel, lang, trust_json, elapsed_ms, usage)
    except Exception as e:  # noqa: BLE001 — статистика не должна ломать проверку
        log.warning("stats not recorded: %s", e)
    yield {"type": "done", "trust": trust_json, "elapsed_ms": elapsed_ms,
           "usage": {k: (round(v, 6) if isinstance(v, float) else v) for k, v in usage.items()}}


async def run_check_full(text: str, question: str = "", ui_lang: str | None = None,
                         channel: str = "api") -> dict:
    """То же самое, но одним JSON (для бенчмарка, API и бота)."""
    out: dict = {"claims": [], "results": [], "citations": [], "errors": []}
    async for ev in run_check(text, question, ui_lang, channel):
        t = ev["type"]
        if t == "start":
            out.update(lang=ev["lang"], sentences=ev["sentences"], provider=ev["provider"])
        elif t == "claims":
            out["claims"] = ev["claims"]
        elif t == "claim_result":
            out["results"].append(ev["result"])
        elif t == "citations":
            out["citations"] = ev["results"]
        elif t == "error":
            out["errors"].append(ev)
        elif t == "done":
            out.update(trust=ev["trust"], elapsed_ms=ev["elapsed_ms"], usage=ev.get("usage"))
    out["results"].sort(key=lambda r: r["claim_id"])
    return out
