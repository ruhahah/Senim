"""Настройки Senim. Всё читается из файла .env в корне проекта.

Ключи ИИ пока пустые — пока их нет, работают:
  * проверка ссылок на литературу (Crossref / OpenAlex / Semantic Scholar),
  * демо-режим с заранее подготовленным результатом,
  * весь интерфейс.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---------- ИИ-провайдеры ----------
    # Основной провайдер: gemini | anthropic | openai | groq | openrouter | (пусто)
    llm_provider: str = ""
    # Запасные провайдеры через запятую, например: "groq,gemini"
    llm_fallback: str = ""

    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash"

    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-4-5"

    openai_api_key: str = ""
    openai_model: str = "gpt-6-luna"

    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-120b"

    openrouter_api_key: str = ""
    openrouter_model: str = "google/gemini-3.5-flash"

    # Цена за 1 млн токенов для оценки стоимости (0 = встроенная оценка для провайдера)
    llm_price_in: float = 0.0
    llm_price_out: float = 0.0

    llm_timeout_s: float = 60.0
    llm_max_concurrency: int = 4

    # ---------- Поиск доказательств ----------
    tavily_api_key: str = ""          # необязательно; без него — только Википедия + база Senim
    wikipedia_langs_for_kk: str = "kk,ru,en"
    evidence_per_claim: int = 6

    # ---------- Научные базы ----------
    contact_email: str = ""           # Crossref/OpenAlex просят e-mail (polite pool)
    openalex_api_key: str = ""
    semantic_scholar_api_key: str = ""

    # ---------- NLI («второе мнение») ----------
    # off | local  (local требует: pip install -r requirements-nli.txt)
    nli_backend: str = "off"
    nli_model: str = "MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7"

    # ---------- Telegram-бот ----------
    telegram_bot_token: str = ""
    # polling = бот запускается отдельно (scripts/telegram_bot.py) · webhook = бот живёт внутри сайта на хостинге
    telegram_mode: str = "polling"
    telegram_bot_username: str = "SenimAI_check_bot"   # ссылка «Telegram» на сайте (можно переопределить в .env)
    public_url: str = ""              # адрес сайта Senim для ссылок из бота/расширения

    # ---------- Защита баланса API ----------
    rate_limit_per_hour: int = 20        # проверок в час на одного пользователя (0 = без лимита)
    rate_limit_daily_total: int = 300    # проверок в сутки на весь сервис (0 = без лимита)

    # ---------- Прочее ----------
    max_claims: int = 12
    cache_path: str = str(ROOT_DIR / "data" / "cache.sqlite")
    cache_enabled: bool = True
    stats_path: str = str(ROOT_DIR / "data" / "stats.sqlite")
    stats_enabled: bool = True

    @property
    def valid_contact_email(self) -> str:
        """Почта для Wikipedia/Crossref: только настоящий латинский адрес, иначе пусто
        (заглушка вроде «ВАША_ПОЧТА» не должна ломать запросы)."""
        e = (self.contact_email or "").strip()
        if e.isascii() and re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", e):
            return e
        return ""

    @property
    def fallback_list(self) -> list[str]:
        return [p.strip() for p in self.llm_fallback.split(",") if p.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
