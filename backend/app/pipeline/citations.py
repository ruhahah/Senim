"""Проверка ссылок на литературу — детерминированно, без ИИ.

DOI → Crossref, затем OpenAlex. Нет DOI → поиск по названию в Crossref и OpenAlex,
при необходимости Semantic Scholar. Сравниваем название, год, первого автора.

Важно: книг и казахскоязычных источников часто нет в научных базах,
поэтому «не найдено» ≠ «выдумано». «Выдумано почти наверняка» — только если
DOI не существует или DOI принадлежит совсем другой работе.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional

import httpx
from rapidfuzz import fuzz

from ..config import get_settings
from ..llm import LLMError, LLMRouter
from ..prompts import CITATION_SYSTEM
from ..schemas import Citation, CitationResult, CitationStatus
from .. import cache
from .text_utils import normalize

log = logging.getLogger("senim.citations")

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>«»,;]+)", re.IGNORECASE)
YEAR_RE = re.compile(r"\b(1[5-9]\d{2}|20\d{2})\b")
PAREN_YEAR_RE = re.compile(r"\(\s*(1[5-9]\d{2}|20\d{2})[a-z]?\s*\)")
QUOTED_RE = re.compile(r"[«\"“']([^«»\"“”']{8,250})[»\"”']")
REF_HINTS = re.compile(
    r"(et al|journal|review|press|university|вестник|журнал|жаршысы|издательство|баспа|"
    r"//|doi|pp\.|vol\.|№|т\.\s?\d|б\.\s?\d|с\.\s?\d|isbn|proceedings|conference)",
    re.IGNORECASE,
)


def clean_doi(doi: str) -> str:
    doi = doi.strip().rstrip(".)]")
    doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", doi, flags=re.IGNORECASE)
    return doi.lower()


# ------------------------------------------------------------------ extraction
def extract_citations_heuristic(text: str) -> list[Citation]:
    out: list[Citation] = []
    for line in re.split(r"\n+", text):
        line = line.strip(" -•*\t")
        # убираем нумерацию «1.», «[2]» и подписи «Дереккөз:», «Источник:», «References:»
        line = re.sub(r"^(\[?\d{1,3}[\].)]\s*)", "", line)
        line = re.sub(r"^[^\s:]{3,20}:\s+", "", line) if re.match(r"^[^\s:]{3,20}:\s", line) else line
        if len(line) < 15:
            continue
        doi_m = DOI_RE.search(line)
        year_m = PAREN_YEAR_RE.search(line) or YEAR_RE.search(line)
        quoted = QUOTED_RE.search(line)
        looks_like_ref = bool(doi_m) or bool(PAREN_YEAR_RE.search(line)) or bool(
            year_m and REF_HINTS.search(line) and len(line) < 400)
        if not looks_like_ref:
            continue
        title = quoted.group(1).strip() if quoted else ""
        if not title and year_m:
            # «Фамилия, И. (2019). Название статьи. Журнал...»
            after = line[year_m.end():].lstrip(").:, ")
            title = re.split(r"\.\s|//", after)[0].strip()
        authors_part = line[: year_m.start()] if year_m else ""
        authors = [
            a.strip(" ,.()") for a in re.split(r",|\band\b|\bи\b|&|;", authors_part)
            if re.match(r"^\s*[A-ZА-ЯӘҒҚҢӨҰҮҺІ][\w\-']{2,}", a.strip())
        ][:4]
        out.append(Citation(
            raw=line[:400], title=title[:300],
            authors=[a.split()[0] for a in authors if a],
            year=int(year_m.group(1)) if year_m else None,
            doi=clean_doi(doi_m.group(1)) if doi_m else "",
        ))
    return out


async def extract_citations(text: str, router: Optional[LLMRouter]) -> list[Citation]:
    heuristic = extract_citations_heuristic(text)
    if not router or not router.available:
        return heuristic
    try:
        data = await router.complete_json(CITATION_SYSTEM, text, cache_ns="citations")
        items = data.get("citations", []) if isinstance(data, dict) else []
    except LLMError as e:
        log.info("LLM citation extraction failed, using heuristic: %s", e)
        return heuristic
    cites: list[Citation] = []
    for c in items:
        if not isinstance(c, dict):
            continue
        doi = str(c.get("doi") or "")
        m = DOI_RE.search(doi) or DOI_RE.search(str(c.get("raw") or ""))
        try:
            year = int(c["year"]) if c.get("year") else None
        except (TypeError, ValueError):
            year = None
        cites.append(Citation(
            raw=str(c.get("raw") or "")[:400], title=str(c.get("title") or "")[:300],
            authors=[str(a) for a in (c.get("authors") or [])][:4], year=year,
            doi=clean_doi(m.group(1)) if m else "",
        ))
    # DOI, найденные регуляркой, но пропущенные моделью — добавляем
    known = {c.doi for c in cites if c.doi}
    for h in heuristic:
        if h.doi and h.doi not in known:
            cites.append(h)
    return cites or heuristic


# ------------------------------------------------------------------ lookups
def _crossref_meta(item: dict) -> dict:
    title = (item.get("title") or [""])[0]
    year = None
    for k in ("issued", "published-print", "published-online", "created"):
        parts = (item.get(k) or {}).get("date-parts") or [[None]]
        if parts and parts[0] and parts[0][0]:
            year = parts[0][0]
            break
    authors = [a.get("family") or a.get("name") or "" for a in item.get("author", [])]
    return {"title": title, "year": year, "authors": [a for a in authors if a],
            "url": f"https://doi.org/{item.get('DOI')}" if item.get("DOI") else item.get("URL", "")}


def _openalex_meta(item: dict) -> dict:
    authors = []
    for a in item.get("authorships", []):
        name = (a.get("author") or {}).get("display_name") or ""
        if name:
            authors.append(name.split()[-1])
    return {"title": item.get("display_name") or item.get("title") or "",
            "year": item.get("publication_year"), "authors": authors,
            "url": item.get("doi") or item.get("id") or ""}


def _polite(params: dict) -> dict:
    s = get_settings()
    if s.valid_contact_email:
        params["mailto"] = s.valid_contact_email
    return params


def _oa_params(params: dict) -> dict:
    s = get_settings()
    params = _polite(params)
    if s.openalex_api_key:
        params["api_key"] = s.openalex_api_key
    return params


class LookupUnavailable(Exception):
    """База недоступна (сеть, лимит, 5xx) — нельзя делать вывод «не существует»."""


NOT_FOUND = object()


async def _fetch(client: httpx.AsyncClient, url: str, params: dict):
    """200 → dict, 404 → NOT_FOUND, всё остальное → LookupUnavailable. Кешируем 200 и 404."""
    key = cache.make_key("http2", [url, params])
    hit = cache.get(key)
    if hit is not None:
        return NOT_FOUND if hit == "__404__" else hit
    try:
        r = await client.get(url, params=params)
    except httpx.HTTPError as e:
        raise LookupUnavailable(str(e)) from e
    if r.status_code == 404:
        cache.put(key, "__404__")
        return NOT_FOUND
    if r.status_code != 200:
        raise LookupUnavailable(f"{url} -> HTTP {r.status_code}")
    try:
        data = r.json()
    except ValueError as e:
        raise LookupUnavailable("bad json") from e
    cache.put(key, data)
    return data


async def _doi_exists(client, doi) -> bool:
    """Реестр doi.org: 200 + responseCode 1 → DOI есть; 404 / responseCode 100 → такого DOI нет."""
    try:
        r = await client.get(f"https://doi.org/api/handles/{doi}")
    except httpx.HTTPError as e:
        raise LookupUnavailable(str(e)) from e
    if r.status_code == 404:
        return False
    if r.status_code != 200:
        raise LookupUnavailable(f"doi.org -> HTTP {r.status_code}")
    try:
        code = r.json().get("responseCode")
    except ValueError as e:
        raise LookupUnavailable("doi.org bad json") from e
    if code == 1:
        return True
    if code == 100:
        return False
    raise LookupUnavailable(f"doi.org responseCode {code}")


async def _crossref_doi(client, doi):
    data = await _fetch(client, f"https://api.crossref.org/works/{doi}", _polite({}))
    return None if data is NOT_FOUND else _crossref_meta(data.get("message") or {})


async def _openalex_doi(client, doi):
    data = await _fetch(client, f"https://api.openalex.org/works/doi:{doi}", _oa_params({}))
    return None if data is NOT_FOUND or not data.get("id") else _openalex_meta(data)


async def _crossref_search(client, title):
    data = await _fetch(client, "https://api.crossref.org/works", _polite({
        "query.bibliographic": title, "rows": 3, "select": "DOI,title,author,issued,created,URL",
    }))
    if data is NOT_FOUND:
        return []
    items = (data.get("message") or {}).get("items") or []
    return [_crossref_meta(i) for i in items]


async def _openalex_search(client, title):
    data = await _fetch(client, "https://api.openalex.org/works", _oa_params({"search": title, "per_page": 3}))
    return [] if data is NOT_FOUND else [_openalex_meta(i) for i in data.get("results", [])]


async def _s2_search(client, title):
    s = get_settings()
    headers = {"x-api-key": s.semantic_scholar_api_key} if s.semantic_scholar_api_key else {}
    try:
        r = await client.get("https://api.semanticscholar.org/graph/v1/paper/search/match",
                             params={"query": title, "fields": "title,year,authors,url"}, headers=headers)
        if r.status_code != 200:
            return []
        items = r.json().get("data", [])
    except (httpx.HTTPError, ValueError):
        return []
    return [{"title": i.get("title", ""), "year": i.get("year"),
             "authors": [a.get("name", "").split()[-1] for a in i.get("authors", []) if a.get("name")],
             "url": i.get("url", "")} for i in items]


def title_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return float(fuzz.token_sort_ratio(normalize(a), normalize(b)))


def _compare(c: Citation, meta: dict) -> list[str]:
    notes = []
    if c.year and meta.get("year") and abs(int(meta["year"]) - c.year) > 1:
        notes.append("year_mismatch")
    if c.authors and meta.get("authors"):
        first = normalize(c.authors[0])
        known = [normalize(a) for a in meta["authors"]]
        if not any(first and (first in k or k in first or fuzz.ratio(first, k) > 80) for k in known):
            notes.append("author_mismatch")
    return notes


async def check_citation(client: httpx.AsyncClient, c: Citation) -> CitationResult:
    try:
        if c.doi:
            # 1) существует ли DOI вообще — спрашиваем сам реестр doi.org (охватывает все агентства:
            #    Crossref, DataCite, mEDRA...). Это надёжнее, чем «нет в Crossref».
            try:
                exists = await _doi_exists(client, c.doi)
            except LookupUnavailable as e:
                log.info("doi.org unavailable: %s", e)
                exists = None
            if exists is False:
                return CitationResult(citation=c, status=CitationStatus.doi_not_found, checked_in=["doi.org"],
                                      notes=["doi_not_found"])
            # 2) метаданные — из Crossref, затем OpenAlex
            meta, checked, unavailable = None, (["doi.org"] if exists else []), 0
            for name, fn in (("crossref", _crossref_doi), ("openalex", _openalex_doi)):
                try:
                    meta = await fn(client, c.doi)
                    checked.append(name)
                except LookupUnavailable as e:
                    log.info("%s unavailable: %s", name, e)
                    unavailable += 1
                if meta:
                    break
            if meta is None:
                if exists:  # DOI настоящий, но метаданных нет в базах (например, DataCite)
                    return CitationResult(citation=c, status=CitationStatus.verified, checked_in=checked,
                                          matched_url=f"https://doi.org/{c.doi}", notes=["no_metadata"])
                if unavailable:  # хотя бы одна база не ответила — не обвиняем источник
                    return CitationResult(citation=c, status=CitationStatus.error, checked_in=checked,
                                          notes=["network_error"])
                return CitationResult(citation=c, status=CitationStatus.doi_not_found, checked_in=checked,
                                      notes=["doi_not_found"])
            sim = title_similarity(c.title, meta["title"]) if c.title else 100.0
            notes = _compare(c, meta)
            if c.title and sim < 60:
                notes.insert(0, "doi_other_work")
            status = CitationStatus.verified if not notes else CitationStatus.mismatch
            return CitationResult(citation=c, status=status, matched_title=meta["title"], matched_year=meta["year"],
                                  matched_authors=meta["authors"][:5], matched_url=meta["url"],
                                  similarity=sim, checked_in=checked, notes=notes)

        if not c.title:
            return CitationResult(citation=c, status=CitationStatus.not_found, notes=["no_title"])

        found = await asyncio.gather(_crossref_search(client, c.title), _openalex_search(client, c.title),
                                     return_exceptions=True)
        checked = [n for n, f in zip(("crossref", "openalex"), found) if not isinstance(f, Exception)]
        if not checked:
            return CitationResult(citation=c, status=CitationStatus.error, notes=["network_error"])
        candidates = [m for f in found if not isinstance(f, Exception) for m in f]
        if not candidates or max(title_similarity(c.title, m["title"]) for m in candidates) < 75:
            candidates += await _s2_search(client, c.title)
            checked.append("semantic_scholar")
        best, best_sim = None, 0.0
        for m in candidates:
            sim = title_similarity(c.title, m["title"])
            if sim > best_sim:
                best, best_sim = m, sim
        if not best or best_sim < 75:
            return CitationResult(citation=c, status=CitationStatus.not_found, checked_in=checked,
                                  similarity=best_sim, notes=["not_in_scholarly_db"])
        notes = _compare(c, best)
        if best_sim < 90:
            notes.insert(0, "similar_title_only")
        status = CitationStatus.verified if not notes else CitationStatus.mismatch
        return CitationResult(citation=c, status=status, matched_title=best["title"], matched_year=best["year"],
                              matched_authors=best["authors"][:5], matched_url=best["url"], similarity=best_sim,
                              checked_in=checked, notes=notes)
    except Exception as e:  # noqa: BLE001
        log.warning("citation check failed: %s", e)
        return CitationResult(citation=c, status=CitationStatus.error, notes=["network_error"])


async def check_citations(client: httpx.AsyncClient, citations: list[Citation]) -> list[CitationResult]:
    sem = asyncio.Semaphore(3)  # Crossref просит не более ~3 одновременных запросов

    async def run(c):
        async with sem:
            return await check_citation(client, c)

    return list(await asyncio.gather(*(run(c) for c in citations[:10])))
