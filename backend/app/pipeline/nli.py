"""NLI — «второе мнение» от небольшой мультиязычной модели (mDeBERTa-v3, 100+ языков).

По умолчанию выключено (NLI_BACKEND=off). Чтобы включить:
    pip install -r requirements-nli.txt
    NLI_BACKEND=local   в .env
Модель (~1 ГБ) скачается при первом запуске и работает на CPU.
"""
from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from typing import Optional

from ..config import get_settings

log = logging.getLogger("senim.nli")


def enabled() -> bool:
    return get_settings().nli_backend == "local" and _load() is not None


@lru_cache
def _load():
    try:
        from transformers import pipeline  # type: ignore
    except ImportError:
        log.warning("NLI_BACKEND=local, но transformers не установлен — NLI отключён")
        return None
    try:
        return pipeline("text-classification", model=get_settings().nli_model, top_k=None)
    except Exception as e:  # noqa: BLE001
        log.warning("Не удалось загрузить NLI-модель: %s", e)
        return None


def _classify_sync(premise: str, hypothesis: str) -> Optional[str]:
    clf = _load()
    if clf is None:
        return None
    out = clf({"text": premise[:1500], "text_pair": hypothesis[:500]})
    scores = out[0] if out and isinstance(out[0], list) else out
    best = max(scores, key=lambda x: x["score"])
    return str(best["label"]).lower()


async def classify(premise: str, hypothesis: str) -> Optional[str]:
    try:
        return await asyncio.to_thread(_classify_sync, premise, hypothesis)
    except Exception as e:  # noqa: BLE001
        log.warning("NLI error: %s", e)
        return None
