"""Язык, разбиение на предложения, нормализация, поиск фрагментов."""
from __future__ import annotations

import re
import unicodedata

from rapidfuzz import fuzz

from ..schemas import Sentence

KK_LETTERS = set("әғқңөұүһіӘҒҚҢӨҰҮҺІ")
_CYR = re.compile(r"[а-яёА-ЯЁ]")
_LAT = re.compile(r"[a-zA-Z]")


_CYR_WORD = re.compile(r"[а-яёА-ЯЁәғқңөұүһіӘҒҚҢӨҰҮҺІ]{2,}")


def detect_lang(text: str) -> str:
    """kk / ru / en. Считаем долю слов с казахскими буквами:
    в казахском тексте их много, а в русском тексте с казахским именем («Құнанбайұлы») — единицы."""
    words = _CYR_WORD.findall(text)
    kk_words = sum(1 for w in words if any(c in KK_LETTERS for c in w))
    cyr = len(_CYR.findall(text)) + sum(1 for c in text if c in KK_LETTERS)
    lat = len(_LAT.findall(text))
    if cyr == 0 and lat == 0:
        return "en"
    if kk_words and kk_words / len(words) >= 0.12:
        return "kk"
    return "ru" if cyr >= lat else "en"


# Сокращения, после которых точка — не конец предложения: «т.е.», «ж.», «г.», «J.», «et al.»
_ABBR = re.compile(
    r"(?:^|[\s(])(?:т|е|г|гг|в|вв|др|пр|см|им|ж|жж|б|т\.б|т\.е|al|Dr|Mr|Mrs|vs|No|pp|Vol|[A-ZА-ЯӘҒҚҢӨҰҮҺІ])\.$"
)


_REF_LINE = re.compile(r"(10\.\d{4,9}/|\(\s*(1[5-9]|20)\d{2}\s*\)|et al\.|https?://)", re.IGNORECASE)


def split_sentences(text: str) -> list[Sentence]:
    """Режем по переносам строк, затем по . ! ? …
    Строка-ссылка на литературу (DOI, «(2019)», URL) остаётся одним фрагментом."""
    sentences: list[Sentence] = []
    pos = 0
    for line in text.split("\n"):
        if _REF_LINE.search(line):
            _push(sentences, text, pos, pos + len(line))
        else:
            _split_line(sentences, text, pos, pos + len(line))
        pos += len(line) + 1
    for i, s in enumerate(sentences):
        s.index = i
    return sentences


def _split_line(sentences: list[Sentence], text: str, lo: int, hi: int) -> None:
    start, n = lo, hi
    for i in range(lo, hi):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        boundary = False
        if ch in "!?…" and nxt in ("", " ", "\n", "\t", "»", '"', ")"):
            boundary = True
        elif ch == "." and nxt in ("", " ", "\n", "\t", "»", '"', ")"):
            chunk = text[start : i + 1]
            after = text[i + 1 : hi].lstrip(" \t")
            is_abbr = bool(_ABBR.search(chunk))
            # «1465 ж. құрылды» — после числа+точки идёт строчная буква → не конец
            lower_next = bool(after) and after[0].islower()
            boundary = not is_abbr and not lower_next
        if boundary:
            _push(sentences, text, start, i + 1)
            start = i + 1
    _push(sentences, text, start, n)


def _push(out: list[Sentence], text: str, s: int, e: int) -> None:
    seg = text[s:e]
    stripped = seg.strip()
    if not stripped:
        return
    lead = len(seg) - len(seg.lstrip())
    s2 = s + lead
    e2 = s2 + len(stripped)
    out.append(Sentence(index=len(out), text=stripped, start=s2, end=e2))


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text).lower()
    t = t.replace("ё", "е")
    t = re.sub(r"[«»“”„\"'`’‘()\[\]]", "", t)
    t = re.sub(r"[‐‑‒–—−]", "-", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip(" .,;:")


def tokens(text: str) -> list[str]:
    return re.findall(r"\w+", normalize(text))


def quote_in_text(quote: str, text: str, threshold: float = 90.0) -> bool:
    """Есть ли цитата в тексте источника (дословно или почти дословно)."""
    t = normalize(text)
    # «кусок1 … кусок2» — каждый кусок должен быть в тексте
    parts = [normalize(p) for p in re.split(r"\.\.\.|…", quote)]
    parts = [p for p in parts if len(p) >= 8]
    if not parts:
        return False
    return all(p in t or fuzz.partial_ratio(p, t) >= threshold for p in parts)


def locate_sentence(span: str, sentences: list[Sentence]) -> int | None:
    """Находит предложение, из которого взят span."""
    if not span or not sentences:
        return None
    q = normalize(span)
    best, best_score = None, 0.0
    for s in sentences:
        st = normalize(s.text)
        if q and q in st:
            return s.index
        score = fuzz.partial_ratio(q, st)
        if score > best_score:
            best, best_score = s.index, score
    return best if best_score >= 70 else None
