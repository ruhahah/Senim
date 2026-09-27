"""Единый слой для ИИ-провайдеров.

Поддерживаются: Gemini, Anthropic (Claude), OpenAI, Groq, OpenRouter.
Провайдер выбирается в .env (LLM_PROVIDER), запасные — в LLM_FALLBACK.
Если основной провайдер упал по лимиту (429) или сбою (5xx), запрос
автоматически уходит к следующему. Ответы кешируются в SQLite.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from . import cache
from .config import Settings, get_settings

log = logging.getLogger("senim.llm")


class LLMError(RuntimeError):
    pass


class LLMNotConfigured(LLMError):
    pass


# ---------------------------------------------------------------- usage / cost
# Примерные цены за 1 млн токенов (вход, выход) в $ — для оценки стоимости одной проверки.
# Уточните в консоли провайдера; можно переопределить в .env: LLM_PRICE_IN / LLM_PRICE_OUT.
PRICES = {
    "anthropic": (1.00, 5.00),   # Claude Haiku 4.5
    "gemini": (0.30, 2.50),      # Flash-Lite, примерно
    "openai": (0.10, 0.50),      # мини-модели, примерно
    "groq": (0.15, 0.75),        # gpt-oss-120b, платный уровень, примерно
    "openrouter": (0.30, 2.50),
}

# Счётчик токенов текущей проверки (каждая проверка получает свой словарь)
USAGE: ContextVar[Optional[dict]] = ContextVar("senim_usage", default=None)


def new_usage() -> dict:
    u = {"calls": 0, "cache_hits": 0, "in": 0, "out": 0, "cost_usd": 0.0, "by_provider": {}}
    USAGE.set(u)
    return u


def _record(provider: str, tin: int, tout: int) -> None:
    u = USAGE.get()
    if u is None:
        return
    s = get_settings()
    pin, pout = PRICES.get(provider, (0.0, 0.0))
    if s.llm_price_in or s.llm_price_out:
        pin, pout = s.llm_price_in, s.llm_price_out
    u["calls"] += 1
    u["in"] += tin
    u["out"] += tout
    u["cost_usd"] += (tin * pin + tout * pout) / 1_000_000
    u["by_provider"][provider] = u["by_provider"].get(provider, 0) + 1


def _record_cache_hit() -> None:
    u = USAGE.get()
    if u is not None:
        u["cache_hits"] += 1


# ---------------------------------------------------------------- JSON utils
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_json(text: str) -> Any:
    """Достаёт JSON из ответа модели, даже если он обёрнут в ```json ... ```."""
    t = _FENCE.sub("", text.strip()).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    # ищем первый { ... } или [ ... ] верхнего уровня
    for open_c, close_c in (("{", "}"), ("[", "]")):
        s, e = t.find(open_c), t.rfind(close_c)
        if s != -1 and e > s:
            try:
                return json.loads(t[s : e + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError(f"Модель вернула не-JSON: {text[:200]!r}")


# ---------------------------------------------------------------- providers
@dataclass
class Provider:
    name: str
    model: str
    api_key: str
    timeout: float

    async def complete(self, system: str, user: str, json_mode: bool) -> str:  # pragma: no cover
        raise NotImplementedError


@dataclass
class OpenAICompatProvider(Provider):
    base_url: str = ""
    supports_json_mode: bool = True
    extra: dict = field(default_factory=dict)
    max_tokens_field: str = "max_tokens"

    async def complete(self, system: str, user: str, json_mode: bool) -> str:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            self.max_tokens_field: 4000,
        }
        if json_mode and self.supports_json_mode:
            body["response_format"] = {"type": "json_object"}
        body.update(self.extra)
        headers = {"Authorization": f"Bearer {self.api_key}"}
        url = self.base_url.rstrip("/") + "/chat/completions"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post(url, json=body, headers=headers)
            if r.status_code == 400 and (self.extra or "response_format" in body):
                # некоторые модели не принимают доп. параметры — пробуем «чистый» запрос
                for k in list(self.extra) + ["response_format"]:
                    body.pop(k, None)
                r = await client.post(url, json=body, headers=headers)
        _raise_for_status(self.name, r)
        data = r.json()
        usage = data.get("usage") or {}
        _record(self.name, int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0))
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError) as e:
            raise LLMError(f"{self.name}: неожиданный ответ {str(data)[:200]}") from e


@dataclass
class AnthropicProvider(Provider):
    async def complete(self, system: str, user: str, json_mode: bool) -> str:
        body = {
            "model": self.model,
            "max_tokens": 4000,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.post("https://api.anthropic.com/v1/messages", json=body, headers=headers)
        _raise_for_status(self.name, r)
        data = r.json()
        usage = data.get("usage") or {}
        _record(self.name, int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0))
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


class RetryableError(LLMError):
    """429 / 503 и т.п.: временно. retry_after — сколько секунд советует подождать сервер."""

    def __init__(self, msg: str, retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


def _short(e: Exception) -> str:
    m = re.search(r"HTTP (\d{3})", str(e))
    msg = re.search(r'"message":\s*"([^"]{0,90})', str(e))
    return " ".join(x for x in (m and f"HTTP {m.group(1)}", msg and msg.group(1)) if x) or str(e)[:100]


def _raise_for_status(name: str, r: httpx.Response) -> None:
    if r.status_code < 400:
        return
    msg = f"{name}: HTTP {r.status_code} {r.text[:300]}"
    if r.status_code in (408, 409, 429) or r.status_code >= 500:
        ra = r.headers.get("retry-after")
        try:
            retry_after = float(ra) if ra else None
        except ValueError:
            retry_after = None
        raise RetryableError(msg, retry_after)
    raise LLMError(msg)


def build_provider(name: str, s: Settings) -> Optional[Provider]:
    t = s.llm_timeout_s
    if name == "gemini" and s.gemini_api_key:
        return OpenAICompatProvider(
            name, s.gemini_model, s.gemini_api_key, t,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            extra={"reasoning_effort": "low"},
        )
    if name == "openai" and s.openai_api_key:
        return OpenAICompatProvider(
            name, s.openai_model, s.openai_api_key, t,
            base_url="https://api.openai.com/v1",
            extra={"reasoning_effort": "low"},
            max_tokens_field="max_completion_tokens",
        )
    if name == "groq" and s.groq_api_key:
        return OpenAICompatProvider(
            name, s.groq_model, s.groq_api_key, t,
            base_url="https://api.groq.com/openai/v1",
            extra={"reasoning_effort": "low"},
        )
    if name == "openrouter" and s.openrouter_api_key:
        return OpenAICompatProvider(
            name, s.openrouter_model, s.openrouter_api_key, t,
            base_url="https://openrouter.ai/api/v1", supports_json_mode=False,
        )
    if name == "anthropic" and s.anthropic_api_key:
        return AnthropicProvider(name, s.anthropic_model, s.anthropic_api_key, t)
    return None


# ---------------------------------------------------------------- router
class LLMRouter:
    COOLDOWN_S = 120.0  # сколько секунд не трогать провайдера после 429/503
    MAX_ROUNDS = 3      # сколько раз ждать, если заняты ВСЕ провайдеры

    def __init__(self, providers: list[Provider], max_concurrency: int = 4):
        self.providers = providers
        self._sem = asyncio.Semaphore(max_concurrency)
        self._cooldown_until: dict[str, float] = {}

    def _order(self) -> list[Provider]:
        """Перегруженные провайдеры уходят в конец очереди на COOLDOWN_S секунд."""
        now = time.monotonic()
        ready = [p for p in self.providers if self._cooldown_until.get(p.name, 0) <= now]
        cooling = [p for p in self.providers if p not in ready]
        return ready + cooling

    @property
    def available(self) -> bool:
        return bool(self.providers)

    @property
    def label(self) -> str:
        return " → ".join(f"{p.name}:{p.model}" for p in self.providers) or "не настроен"

    async def complete_json(self, system: str, user: str, *, cache_ns: str = "llm") -> Any:
        if not self.providers:
            raise LLMNotConfigured(
                "ИИ-провайдер не настроен: заполните LLM_PROVIDER и ключ в файле .env"
            )
        last_err: Optional[Exception] = None
        for rnd in range(self.MAX_ROUNDS):
            waits: list[float] = []
            for p in self._order():
                key = cache.make_key(cache_ns, [p.name, p.model, system, user])
                hit = cache.get(key)
                if hit is not None:
                    _record_cache_hit()
                    return hit
                for attempt in range(2):
                    try:
                        async with self._sem:
                            prompt = user if attempt == 0 else (
                                user + "\n\nВАЖНО: верни ТОЛЬКО корректный JSON без пояснений."
                            )
                            text = await p.complete(system, prompt, json_mode=True)
                        data = parse_json(text)
                        cache.put(key, data)
                        return data
                    except RetryableError as e:
                        last_err = e
                        waits.append(e.retry_after or 0)
                        first_time = self._cooldown_until.get(p.name, 0) <= time.monotonic()
                        self._cooldown_until[p.name] = time.monotonic() + self.COOLDOWN_S
                        if first_time:
                            log.warning("%s недоступен (%s) — %d с работаю через запасного провайдера",
                                        p.name, _short(e), int(self.COOLDOWN_S))
                        break  # к следующему провайдеру
                    except httpx.HTTPError as e:  # таймаут, обрыв сети
                        last_err = e
                        waits.append(0)
                        log.warning("%s: %s %s", p.name, type(e).__name__, str(e)[:120])
                        break
                    except LLMError as e:  # не-JSON или 4xx — вторая попытка, потом следующий
                        last_err = e
                        log.warning("%s (попытка %d)", _short(e), attempt + 1)
            # все провайдеры временно заняты (лимит в минуту) — ждём и пробуем ещё раз
            if rnd + 1 < self.MAX_ROUNDS and waits and len(waits) == len(self.providers):
                delay = min(30.0, max(max(waits), 5.0 * (rnd + 1)))
                log.warning("Все провайдеры заняты — жду %.0f с и повторяю", delay)
                await asyncio.sleep(delay)
                continue
            break
        raise LLMError(f"Все провайдеры недоступны. Последняя ошибка: {last_err}")


_router: Optional[LLMRouter] = None


def get_router() -> LLMRouter:
    global _router
    if _router is None:
        s = get_settings()
        names = [s.llm_provider] + s.fallback_list if s.llm_provider else s.fallback_list
        seen, providers = set(), []
        for n in names:
            n = n.strip().lower()
            if n and n not in seen:
                seen.add(n)
                p = build_provider(n, s)
                if p:
                    providers.append(p)
        _router = LLMRouter(providers, s.llm_max_concurrency)
    return _router


def reset_router() -> None:
    global _router
    _router = None
