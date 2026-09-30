"""Защита кошелька: ограничение частоты платных проверок.

- на одного пользователя (IP сайта или чат Telegram): RATE_LIMIT_PER_HOUR в час;
- на весь сервис: RATE_LIMIT_DAILY_TOTAL в сутки.
0 = без ограничения. Счётчики в памяти (сбрасываются при перезапуске) — этого
достаточно, чтобы никто не «выжег» баланс API за вечер.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from .config import get_settings

_lock = threading.Lock()
_per_user: dict[str, deque] = defaultdict(deque)
_daily: deque = deque()

HOUR, DAY = 3600.0, 86400.0

MESSAGES = {
    "user": {
        "kk": "Сағатына тексеру лимитіне жеттіңіз. {minutes} минуттан кейін қайталаңыз.",
        "ru": "Достигнут лимит проверок в час. Попробуйте через {minutes} мин.",
        "en": "Hourly check limit reached. Try again in {minutes} min.",
    },
    "total": {
        "kk": "Бүгінгі жалпы тексеру лимиті таусылды. Ертең қайталаңыз немесе «Демо» батырмасын басыңыз.",
        "ru": "Дневной лимит проверок сервиса исчерпан. Попробуйте завтра или нажмите «Демо».",
        "en": "The service's daily check limit is reached. Try tomorrow or press “Demo”.",
    },
}


class RateLimited(Exception):
    def __init__(self, kind: str, retry_after: int):
        super().__init__(kind)
        self.kind = kind
        self.retry_after = retry_after

    def message(self, lang: str = "ru") -> str:
        m = MESSAGES[self.kind].get(lang, MESSAGES[self.kind]["ru"])
        return m.format(minutes=max(1, round(self.retry_after / 60)))


def _trim(q: deque, window: float, now: float) -> None:
    while q and now - q[0] >= window:
        q.popleft()


def check_and_count(user_key: str) -> None:
    """Засчитывает одну проверку или бросает RateLimited."""
    s = get_settings()
    per_hour, per_day = s.rate_limit_per_hour, s.rate_limit_daily_total
    now = time.time()
    with _lock:
        uq = _per_user[user_key]
        _trim(uq, HOUR, now)
        _trim(_daily, DAY, now)
        if per_day and len(_daily) >= per_day:
            raise RateLimited("total", int(DAY - (now - _daily[0])))
        if per_hour and len(uq) >= per_hour:
            raise RateLimited("user", int(HOUR - (now - uq[0])))
        uq.append(now)
        _daily.append(now)


def check_and_count_class(code: str, student: str) -> None:
    """Для класса: весь класс часто сидит за одним IP школы, поэтому лимит считаем
    на ученика и на класс, а не на IP. Дневной лимит всего сервиса действует как обычно."""
    s = get_settings()
    now = time.time()
    limits = [(f"class:{code}", s.class_rate_limit_per_hour),
              (f"class:{code}:{student.lower()}", s.class_student_per_hour)]
    with _lock:
        _trim(_daily, DAY, now)
        if s.rate_limit_daily_total and len(_daily) >= s.rate_limit_daily_total:
            raise RateLimited("total", int(DAY - (now - _daily[0])))
        for key, lim in limits:
            q = _per_user[key]
            _trim(q, HOUR, now)
            if lim and len(q) >= lim:
                raise RateLimited("user", int(HOUR - (now - q[0])))
        for key, _ in limits:
            _per_user[key].append(now)
        _daily.append(now)


def hit(key: str, per_hour: int) -> bool:
    """Простой счётчик для бесплатных действий (например, создание класса). True = можно."""
    now = time.time()
    with _lock:
        q = _per_user["misc:" + key]
        _trim(q, HOUR, now)
        if per_hour and len(q) >= per_hour:
            return False
        q.append(now)
        return True


def client_key(request) -> str:
    """IP клиента. За прокси хостинга (Hugging Face, Render) — первый адрес из X-Forwarded-For."""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return "ip:" + fwd.split(",")[0].strip()
    return "ip:" + (request.client.host if request.client else "unknown")


def is_local(key: str) -> bool:
    return key in ("ip:127.0.0.1", "ip:::1", "ip:localhost")


def reset() -> None:
    with _lock:
        _per_user.clear()
        _daily.clear()
