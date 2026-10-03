"""Фото или скриншот ответа ИИ → текст (бета).

ИИ, который видит изображения (OpenAI или Gemini), только ПЕРЕПИСЫВАЕТ текст с картинки.
Проверка фактов идёт дальше обычным путём — по тексту, который ученик видит и может поправить.
"""
from __future__ import annotations

import base64
import binascii
import re

from .llm import LLMError, get_router

VISION_PROVIDERS = ("openai", "gemini", "openrouter")
ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp"}
MAX_BYTES = 5_000_000

OCR_SYSTEM = """Ты распознаёшь текст на фото или скриншоте для системы фактчекинга Senim.
Обычно на изображении ответ чат-бота (ChatGPT, Gemini и др.), страница учебника или текст ученика.
Перепиши текст ДОСЛОВНО, на том же языке (казахский, русский или английский). Ничего не исправляй,
не переводи, не сокращай и не дополняй — даже если в тексте ошибки: их потом проверит Senim.
- Если это переписка с чат-ботом: в "text" — только ответ бота, в "question" — вопрос пользователя, если он виден.
- Пропусти элементы интерфейса: кнопки, меню, время, значки, подписи-ссылки на источники рядом с текстом.
- Абзацы и пункты списка разделяй переносом строки. Ссылки на литературу (авторы, год, DOI) переписывай полностью.
- Если текста нет или его нельзя разобрать — "text": "" и "readable": false.
Верни ТОЛЬКО JSON: {"text": "...", "question": "...", "readable": true}"""


class OCRError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


_DATA_URL = re.compile(r"^data:(image/[a-z+]+);base64,(.+)$", re.S)


def decode_image(data_url: str) -> tuple[str, bytes]:
    """data:image/jpeg;base64,… → (mime, байты). Проверяет тип и размер."""
    m = _DATA_URL.match((data_url or "").strip())
    if not m:
        raise OCRError("bad_image")
    mime = m.group(1).lower()
    if mime not in ALLOWED_MIME:
        raise OCRError("bad_type")
    try:
        raw = base64.b64decode(m.group(2), validate=True)
    except (binascii.Error, ValueError) as e:
        raise OCRError("bad_image") from e
    if len(raw) > MAX_BYTES:
        raise OCRError("too_big")
    if len(raw) < 100:
        raise OCRError("bad_image")
    return mime, raw


async def extract_text(mime: str, raw: bytes) -> dict:
    """Возвращает {"text", "question"}; пустой text — на фото нет читаемого текста."""
    b64 = base64.b64encode(raw).decode("ascii")
    content = [
        {"type": "text", "text": "Распознай текст на изображении."},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
    ]
    try:
        data = await get_router().complete_json(OCR_SYSTEM, content, cache_ns="ocr", only=VISION_PROVIDERS)
    except LLMError as e:
        raise OCRError("llm_failed", str(e)) from e
    if not isinstance(data, dict):
        data = {}
    text = str(data.get("text") or "").strip()[:12000]
    question = str(data.get("question") or "").strip()[:500]
    return {"text": text, "question": question}
