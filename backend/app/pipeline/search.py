"""Поиск доказательств: учебная база Senim + Википедия (kk/ru/en) + Tavily (необязательно).

Почему несколько языков: на казахском веб-источников мало, поэтому
для казахских утверждений ищем ещё на русском и английском.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from functools import lru_cache
from typing import Optional

import httpx

from .. import cache
from ..config import ROOT_DIR, get_settings
from ..schemas import Claim, Evidence
from .text_utils import detect_lang, tokens

log = logging.getLogger("senim.search")
# Wikimedia требует понятный User-Agent с контактом, иначе может ответить 403
UA = "SenimFactChecker/0.1 ({contact}; student fact-checking project for WIT Teens Hackathon) python-httpx"
LAST_ERRORS: dict[str, str] = {}  # host -> последняя ошибка (для диагностики в smoke_test)
PASSAGE_CHARS = 700


def _stem(tok: str) -> str:
    # грубый «стемминг» для kk/ru: первые 5 символов покрывают большинство окончаний
    return tok[:5] if len(tok) > 5 else tok


def _stems(text: str) -> list[str]:
    return [_stem(t) for t in tokens(text) if len(t) > 1]


def lexical_score(query: str, passage: str) -> float:
    q = set(_stems(query))
    if not q:
        return 0.0
    p = Counter(_stems(passage))
    hit = sum(1 for t in q if t in p)
    # числа (годы, даты) важнее обычных слов
    nums = [t for t in q if t.isdigit()]
    num_hit = sum(1 for t in nums if t in p)
    return (hit + num_hit) / (len(q) + len(nums)) if q else 0.0


def split_passages(text: str, max_chars: int = PASSAGE_CHARS) -> list[str]:
    paras = [p.strip() for p in text.split("\n") if p.strip() and not p.strip().startswith("==")]
    out: list[str] = []
    for p in paras:
        while len(p) > max_chars:
            cut = p.rfind(". ", 0, max_chars)
            cut = cut + 1 if cut > max_chars // 2 else max_chars
            out.append(p[:cut].strip())
            p = p[cut:].strip()
        if len(p) > 40:
            out.append(p)
    return out


# ------------------------------------------------------------------ Senim KB
@lru_cache
def load_kb() -> list[dict]:
    path = ROOT_DIR / "data" / "senim_kb.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("entries", [])


@lru_cache
def _kb_idf() -> dict[str, float]:
    import math
    docs = [set(_stems(e["title"] + " " + e["text"])) for e in load_kb()]
    df = Counter(t for d in docs for t in d)
    n = max(len(docs), 1)
    return {t: math.log(1 + n / c) for t, c in df.items()}


def _kb_score(query: str, entry: dict) -> float:
    """Доля «веса» запроса, найденного в записи; редкие слова и совпадения с заголовком важнее."""
    idf = _kb_idf()
    q = set(_stems(query))
    if not q:
        return 0.0
    body = set(_stems(entry["text"]))
    title = set(_stems(entry["title"]))
    total = sum(idf.get(t, 2.0) for t in q)
    got = sum(idf.get(t, 2.0) for t in q if t in body or t in title)
    title_bonus = 0.35 if q & title else 0.0  # запрос про ту же тему, что и заголовок записи
    return (got / total if total else 0.0) + title_bonus


def search_kb(query_texts: list[str], limit: int = 2, min_score: float = 0.34) -> list[dict]:
    scored = []
    for e in load_kb():
        s = max(_kb_score(q, e) for q in query_texts)
        if s >= min_score:
            scored.append((s, e))
    scored.sort(key=lambda x: -x[0])
    return [
        {"source": f"senim_kb:{e['lang']}", "title": e["title"], "url": f"senim-kb://{e['id']}", "text": e["text"], "score": s}
        for s, e in scored[:limit]
    ]


# ------------------------------------------------------------------ HTTP helper
async def _get_json(client: httpx.AsyncClient, url: str, params: dict) -> Optional[dict]:
    key = cache.make_key("http", [url, params])
    hit = cache.get(key)
    if hit is not None:
        return hit
    try:
        r = await client.get(url, params=params)
        if r.status_code != 200:
            host = httpx.URL(url).host
            if host not in LAST_ERRORS:  # предупреждаем один раз на сайт, без спама
                log.warning("GET %s -> HTTP %s", host, r.status_code)
            LAST_ERRORS[host] = f"HTTP {r.status_code}: {r.text[:160]}"
            return None
        data = r.json()
        cache.put(key, data)
        return data
    except (httpx.HTTPError, ValueError) as e:
        host = httpx.URL(url).host
        if host not in LAST_ERRORS:
            log.warning("GET %s failed: %s: %s", host, type(e).__name__, str(e)[:120])
        LAST_ERRORS[host] = f"{type(e).__name__}: {str(e)[:160]}"
        return None


# ------------------------------------------------------------------ Wikipedia
async def search_wikipedia(client: httpx.AsyncClient, query: str, lang: str, pages: int = 2) -> list[dict]:
    api = f"https://{lang}.wikipedia.org/w/api.php"
    data = await _get_json(client, api, {
        "action": "query", "list": "search", "srsearch": query,
        "srlimit": pages, "format": "json", "utf8": 1,
    })
    hits = (data or {}).get("query", {}).get("search", [])
    if not hits:
        return []
    ids = "|".join(str(h["pageid"]) for h in hits)
    data = await _get_json(client, api, {
        "action": "query", "prop": "extracts|info", "inprop": "url", "explaintext": 1,
        "exsectionformat": "plain", "pageids": ids, "format": "json", "utf8": 1,
    })
    out = []
    for page in (data or {}).get("query", {}).get("pages", {}).values():
        text = page.get("extract", "")
        passages = split_passages(text)
        ranked = sorted(passages, key=lambda p: -lexical_score(query, p))[:2]
        for p in ranked:
            out.append({
                "source": f"wikipedia:{lang}", "title": page.get("title", ""),
                "url": page.get("fullurl", f"https://{lang}.wikipedia.org/?curid={page.get('pageid')}"),
                "text": p, "score": lexical_score(query, p),
            })
    return out


# ------------------------------------------------------------------ Tavily
async def search_tavily(client: httpx.AsyncClient, query: str) -> list[dict]:
    s = get_settings()
    if not s.tavily_api_key:
        return []
    key = cache.make_key("tavily", query)
    hit = cache.get(key)
    if hit is None:
        try:
            r = await client.post("https://api.tavily.com/search", json={
                "query": query, "max_results": 3, "search_depth": "basic", "include_answer": False,
            }, headers={"Authorization": f"Bearer {s.tavily_api_key}"})
            if r.status_code != 200:
                return []
            hit = r.json()
            cache.put(key, hit)
        except httpx.HTTPError:
            return []
    return [
        {"source": "web", "title": x.get("title", ""), "url": x.get("url", ""),
         "text": (x.get("content") or "")[:PASSAGE_CHARS], "score": float(x.get("score") or 0.5)}
        for x in hit.get("results", [])
    ]


# ------------------------------------------------------------------ main entry
def _wiki_lang_for(query: str, claim_lang: str) -> str:
    ql = detect_lang(query)
    return ql if ql in ("kk", "ru", "en") else claim_lang


async def gather_evidence(claim: Claim, claim_lang: str, client: httpx.AsyncClient) -> list[Evidence]:
    s = get_settings()
    queries = [q for q in (claim.search_queries or []) if q.strip()][:3] or [claim.text]
    all_q = [claim.text] + queries

    results: list[dict] = search_kb(all_q)

    tasks = []
    seen_pairs = set()
    for q in queries:
        lang = _wiki_lang_for(q, claim_lang)
        if (q, lang) not in seen_pairs:
            seen_pairs.add((q, lang))
            tasks.append(search_wikipedia(client, q, lang))
    # для казахского текста дополнительно ищем по-русски, даже если модель не дала запрос
    if claim_lang == "kk" and not any(_wiki_lang_for(q, "kk") == "ru" for q in queries):
        tasks.append(search_wikipedia(client, claim.text, "ru"))
    if queries:
        tasks.append(search_tavily(client, queries[-1] if len(queries) > 1 else queries[0]))

    for batch in await asyncio.gather(*tasks, return_exceptions=True):
        if isinstance(batch, list):
            results.extend(batch)

    # переоцениваем относительно самого утверждения и всех запросов, убираем дубли
    for r in results:
        r["rank"] = max(lexical_score(q, r["text"]) for q in all_q) + (0.15 if r["source"].startswith("senim_kb") else 0)
    results.sort(key=lambda r: -r["rank"])
    uniq, seen = [], set()
    for r in results:
        k = r["text"][:120]
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)
    return [
        Evidence(id=i + 1, source=r["source"], title=r["title"], url=r["url"], text=r["text"])
        for i, r in enumerate(uniq[: s.evidence_per_claim])
    ]


def http_client() -> httpx.AsyncClient:
    email = get_settings().valid_contact_email
    contact = f"mailto:{email}" if email else "https://www.mediawiki.org/wiki/API:Etiquette"
    return httpx.AsyncClient(timeout=20.0, headers={"User-Agent": UA.format(contact=contact)}, follow_redirects=True)
