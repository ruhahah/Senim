"""Модели данных (Pydantic). Один формат для API, фронтенда и бенчмарка."""
from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


class Lang(str, Enum):
    kk = "kk"
    ru = "ru"
    en = "en"


class ClaimType(str, Enum):
    fact = "fact"
    number = "number"
    date = "date"
    citation = "citation"
    opinion = "opinion"
    advice = "advice"


class RawVerdict(str, Enum):
    """Что вернул LLM-судья."""
    SUPPORTED = "SUPPORTED"
    PARTIAL = "PARTIAL"
    CONTRADICTED = "CONTRADICTED"
    NOT_ENOUGH_EVIDENCE = "NOT_ENOUGH_EVIDENCE"


class Status(str, Enum):
    """Итоговый статус для пользователя (4 цвета)."""
    supported = "supported"        # 🟢 подтверждено
    disputed = "disputed"          # 🟡 спорно / частично
    contradicted = "contradicted"  # 🔴 опровергнуто
    unverifiable = "unverifiable"  # ⚪ мнение / нет данных


ErrorType = Literal[
    "wrong_date", "wrong_number", "wrong_entity", "fabricated_source",
    "outdated", "overgeneralization", "no_evidence", "none",
]


class Sentence(BaseModel):
    index: int
    text: str
    start: int
    end: int


class Claim(BaseModel):
    id: int
    text: str
    span: str = ""                       # дословный фрагмент исходного текста
    type: ClaimType = ClaimType.fact
    importance: int = Field(2, ge=1, le=3)
    search_queries: list[str] = []
    sentence_index: Optional[int] = None


class Evidence(BaseModel):
    id: int
    source: str                          # wikipedia:kk | tavily | senim_kb
    title: str
    url: str
    text: str


class ClaimResult(BaseModel):
    claim_id: int
    status: Status
    raw_verdict: Optional[RawVerdict] = None
    error_type: ErrorType = "none"
    quote: str = ""
    quote_verified: bool = False
    evidence: Optional[Evidence] = None
    explanation: str = ""
    correction: str = ""
    how_to_check: str = ""
    nli_label: Optional[str] = None      # entailment | contradiction | neutral
    nli_agrees: Optional[bool] = None
    flags: list[str] = []                # quote_unverified, models_disagree, ...
    rejected_quote: str = ""             # цитата модели, которой нет в источнике (для отладки)


class CitationStatus(str, Enum):
    verified = "verified"                # найдена, метаданные совпадают
    mismatch = "mismatch"                # существует, но автор/год/название искажены
    doi_not_found = "doi_not_found"      # DOI не существует → почти наверняка выдумка
    not_found = "not_found"              # не найдено в научных базах → проверь вручную
    error = "error"


class Citation(BaseModel):
    raw: str
    title: str = ""
    authors: list[str] = []
    year: Optional[int] = None
    doi: str = ""


class CitationResult(BaseModel):
    citation: Citation
    status: CitationStatus
    matched_title: str = ""
    matched_year: Optional[int] = None
    matched_authors: list[str] = []
    matched_url: str = ""
    similarity: float = 0.0
    checked_in: list[str] = []
    notes: list[str] = []


class TrustReport(BaseModel):
    trust_index: int
    band: Literal["high", "medium", "low", "na"]
    counts: dict[str, int]
    fabricated_citations: int = 0
    capped: bool = False


# ---------- запросы ----------
class CheckRequest(BaseModel):
    text: str = Field(..., min_length=10, max_length=12000)
    ui_lang: Optional[Lang] = None
    question: str = ""                   # исходный вопрос к ИИ (необязательно)
    class_code: str = Field("", max_length=12)   # код класса (режим учителя), необязательно
    student: str = Field("", max_length=60)      # имя ученика, которое он ввёл сам
    source_ai: str = Field("", max_length=20)    # чей это ответ: chatgpt, gemini… (для рейтинга ИИ)


class OCRRequest(BaseModel):
    image: str = Field(..., min_length=100, max_length=7_000_000)  # data:image/jpeg;base64,…


class CitationsRequest(BaseModel):
    text: str = Field(..., min_length=5, max_length=12000)


class FeedbackRequest(BaseModel):
    useful: Optional[bool] = None
    would_notice: Optional[Literal["yes", "no", "unsure"]] = None
    lang: Optional[Lang] = None
    channel: Literal["web", "extension", "telegram"] = "web"


class ClassCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=60)


class ThinkResultRequest(BaseModel):
    student: str = Field("", max_length=60)
    caught: int = Field(0, ge=0, le=100)
    missed: int = Field(0, ge=0, le=100)
    false_alarms: int = Field(0, ge=0, le=100)


class TrainerNewRequest(BaseModel):
    lang: Literal["kk", "ru", "en"] = "ru"
    topic: str = Field("", max_length=80)
    n_errors: int = Field(2, ge=1, le=3)


class TrainerClassRequest(TrainerNewRequest):
    key: str = Field(..., max_length=64)


class TrainerAnswerRequest(BaseModel):
    student: str = Field("", max_length=60)
    marked: list[int] = Field(default_factory=list, max_length=20)
    seconds: int = Field(0, ge=0, le=3600)
