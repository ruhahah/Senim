"""Telegram-бот Senim (общая логика).

Два режима работы:
  * polling  — scripts/telegram_bot.py на вашем компьютере (бот работает, пока открыто окно);
  * webhook  — внутри сайта на хостинге (Render и т.п.): Telegram сам присылает сообщения
               на /api/telegram/<секрет>, отдельный процесс не нужен.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import logging

import httpx

from . import ratelimit, stats
from .config import get_settings
from .pipeline.orchestrator import run_check_full
from .pipeline.text_utils import detect_lang

log = logging.getLogger("senim.bot")

ICON = {"supported": "🟢", "disputed": "🟡", "contradicted": "🔴", "unverifiable": "⚪"}
T = {
    "kk": {
        "start": "Сәлем! Мен <b>Senim</b> — ЖИ жауаптарын тексеремін.\n\nChatGPT, Gemini немесе басқа ЖИ жауабын маған жіберіңіз (немесе қайта жіберіңіз). Мен әр тұжырымды дереккөздермен салыстырып, неге сенуге болатынын не болмайтынын түсіндіремін.",
        "wait": "🔎 Тексеріп жатырмын… (10–30 сек)",
        "short": "Тексеру үшін ЖИ жауабының мәтінін жіберіңіз (кемінде 10 таңба).",
        "band": {"high": "Сүйенуге болады", "medium": "Мұқият тексеріңіз", "low": "Тексермей қолданбаңыз", "na": "Тексерілетін факт табылмады"},
        "why": "Неге", "fix": "Дұрысы", "src": "Дереккөз", "refs": "Сілтемелер",
        "ref": {"verified": "✅ бар", "mismatch": "🟡 бар, бірақ бұрмаланған", "doi_not_found": "❌ DOI жоқ — ойдан шығарылған",
                "not_found": "⚪ базалардан табылмады", "error": "⚪ тексеру мүмкін болмады"},
        "more": "Толық талдау", "fail": "Кешіріңіз, тексеру сәтсіз аяқталды. Кейінірек қайталап көріңіз.",
        "fb_useful": "Көмектесті ме?", "fb_notice": "Қатені өзіңіз байқар ма едіңіз?", "yes": "Иә", "no": "Жоқ", "thanks": "Рақмет!",
    },
    "ru": {
        "start": "Привет! Я <b>Senim</b> — проверяю ответы ИИ.\n\nПришлите или перешлите мне ответ ChatGPT, Gemini или другого ИИ. Я сверю каждое утверждение с источниками и объясню, почему ему можно или нельзя доверять.",
        "wait": "🔎 Проверяю… (10–30 сек)",
        "short": "Пришлите текст ответа ИИ для проверки (от 10 символов).",
        "band": {"high": "Можно опираться", "medium": "Проверяй внимательно", "low": "Не используй без проверки", "na": "Проверяемых фактов не найдено"},
        "why": "Почему", "fix": "Как правильно", "src": "Источник", "refs": "Ссылки",
        "ref": {"verified": "✅ существует", "mismatch": "🟡 существует, но искажена", "doi_not_found": "❌ DOI не существует — выдумка",
                "not_found": "⚪ нет в научных базах", "error": "⚪ не удалось проверить"},
        "more": "Полный разбор", "fail": "Извините, проверка не удалась. Попробуйте чуть позже.",
        "fb_useful": "Помогло?", "fb_notice": "Заметили бы ошибку сами?", "yes": "Да", "no": "Нет", "thanks": "Спасибо!",
    },
    "en": {
        "start": "Hi! I'm <b>Senim</b> — I check AI answers.\n\nSend or forward me an answer from ChatGPT, Gemini or another AI. I'll check every claim against sources and explain why you can or can't trust it.",
        "wait": "🔎 Checking… (10–30 s)",
        "short": "Send the AI answer text to check (at least 10 characters).",
        "band": {"high": "Safe to rely on", "medium": "Check carefully", "low": "Don't use without checking", "na": "No checkable facts found"},
        "why": "Why", "fix": "Correct", "src": "Source", "refs": "References",
        "ref": {"verified": "✅ exists", "mismatch": "🟡 exists, but distorted", "doi_not_found": "❌ DOI doesn't exist — fabricated",
                "not_found": "⚪ not in databases", "error": "⚪ couldn't check"},
        "more": "Full report", "fail": "Sorry, the check failed. Please try again later.",
        "fb_useful": "Helpful?", "fb_notice": "Would you have noticed?", "yes": "Yes", "no": "No", "thanks": "Thanks!",
    },
}


def esc(s: str) -> str:
    return html.escape(s or "", quote=False)


def format_report(out: dict, lang: str) -> str:
    t = T.get(lang, T["ru"])
    tr = out.get("trust") or {"band": "na", "trust_index": 0}
    band_icon = {"high": "🟢", "medium": "🟡", "low": "🔴", "na": "⚪"}[tr["band"]]
    head = f"{band_icon} <b>{t['band'][tr['band']]}</b>"
    if tr["band"] != "na":
        head += f" — {tr['trust_index']}/100"
    lines = [head, ""]
    claims = {c["id"]: c for c in out.get("claims", [])}
    for r in out.get("results", []):
        c = claims.get(r["claim_id"])
        if not c:
            continue
        lines.append(f"{ICON.get(r['status'], '⚪')} {esc(c['text'])}")
        if r["status"] in ("contradicted", "disputed") and r.get("explanation"):
            lines.append(f"   <i>{t['why']}:</i> {esc(r['explanation'])[:300]}")
            if r.get("correction"):
                lines.append(f"   <i>{t['fix']}:</i> {esc(r['correction'])[:200]}")
            if r.get("quote") and r.get("evidence"):
                src = r["evidence"]["url"]
                src_txt = f'<a href="{esc(src)}">{esc(r["evidence"]["title"])}</a>' if src.startswith("http") else esc(r["evidence"]["title"])
                lines.append(f"   «{esc(r['quote'])[:200]}» — {src_txt}")
    cits = out.get("citations") or []
    if cits:
        lines += ["", f"📚 <b>{t['refs']}</b>"]
        for cr in cits:
            status = "doi_not_found" if "doi_other_work" in cr.get("notes", []) else cr["status"]
            title = cr["citation"].get("title") or cr["citation"].get("raw", "")[:80]
            lines.append(f"{t['ref'].get(status, status)}: {esc(title)[:120]}")
    url = get_settings().public_url
    if url:
        lines += ["", f'<a href="{esc(url)}">{t["more"]} → Senim</a>']
    text = "\n".join(lines)
    return text if len(text) < 4000 else text[:3990] + "…"


def feedback_keyboard(lang: str) -> dict:
    t = T.get(lang, T["ru"])
    return {"inline_keyboard": [
        [{"text": f"👍 {t['fb_useful']} {t['yes']}", "callback_data": f"fb|useful|1|{lang}"},
         {"text": f"👎 {t['no']}", "callback_data": f"fb|useful|0|{lang}"}],
        [{"text": f"🧐 {t['fb_notice']} {t['yes']}", "callback_data": f"fb|notice|yes|{lang}"},
         {"text": t["no"], "callback_data": f"fb|notice|no|{lang}"}],
    ]}


class Bot:
    def __init__(self, token: str):
        self.api = f"https://api.telegram.org/bot{token}"
        self.client = httpx.AsyncClient(timeout=60)
        self.sem = asyncio.Semaphore(2)

    async def call(self, method: str, **params):
        r = await self.client.post(f"{self.api}/{method}", json=params)
        data = r.json()
        if not data.get("ok"):
            log.warning("%s failed: %s", method, str(data)[:200])
        return data.get("result")

    async def handle(self, msg: dict):
        chat = msg["chat"]["id"]
        text = (msg.get("text") or msg.get("caption") or "").strip()
        lang = detect_lang(text) if text and not text.startswith("/") else (
            "kk" if (msg.get("from") or {}).get("language_code") == "kk" else
            "en" if (msg.get("from") or {}).get("language_code") == "en" else "ru")
        t = T[lang]
        if not text or text.startswith("/start") or text.startswith("/help"):
            await self.call("sendMessage", chat_id=chat, text=t["start"], parse_mode="HTML")
            return
        if len(text) < 10:
            await self.call("sendMessage", chat_id=chat, text=t["short"])
            return
        try:
            ratelimit.check_and_count(f"tg:{chat}")
        except ratelimit.RateLimited as e:
            await self.call("sendMessage", chat_id=chat, text=e.message(lang))
            return
        wait = await self.call("sendMessage", chat_id=chat, text=t["wait"], reply_to_message_id=msg["message_id"])
        async with self.sem:
            try:
                out = await run_check_full(text[:12000], channel="telegram")
                report = format_report(out, lang)
            except Exception as e:  # noqa: BLE001
                log.exception("check failed: %s", e)
                report = t["fail"]
        kb = feedback_keyboard(lang) if report != t["fail"] else None
        extra = {"reply_markup": kb} if kb else {}
        if wait:
            await self.call("editMessageText", chat_id=chat, message_id=wait["message_id"], text=report,
                            parse_mode="HTML", disable_web_page_preview=True, **extra)
        else:
            await self.call("sendMessage", chat_id=chat, text=report, parse_mode="HTML",
                            disable_web_page_preview=True, **extra)

    async def handle_callback(self, cq: dict):
        """Кнопки мини-опроса под разбором: записываем ответ и убираем отвеченный ряд."""
        parts = (cq.get("data") or "").split("|")
        if len(parts) != 4 or parts[0] != "fb":
            return
        _, q, v, lang = parts
        try:
            if q == "useful":
                stats.record_feedback("telegram", lang, v == "1", None)
            elif q == "notice" and v in ("yes", "no"):
                stats.record_feedback("telegram", lang, None, v)
        except Exception as e:  # noqa: BLE001
            log.warning("feedback not saved: %s", e)
        await self.call("answerCallbackQuery", callback_query_id=cq["id"], text=T.get(lang, T["ru"])["thanks"])
        msg = cq.get("message") or {}
        rows = (msg.get("reply_markup") or {}).get("inline_keyboard", [])
        left = [r for r in rows if not any(b.get("callback_data", "").startswith(f"fb|{q}|") for b in r)]
        if msg:
            await self.call("editMessageReplyMarkup", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
                            reply_markup={"inline_keyboard": left})

    async def run(self):
        """Режим polling (на своём компьютере). Снимает webhook, если он был установлен."""
        await self.call("deleteWebhook", drop_pending_updates=False)
        me = await self.call("getMe")
        if not me:
            print("Не удалось подключиться к Telegram — проверьте TELEGRAM_BOT_TOKEN в .env")
            return
        print(f"Бот @{me['username']} запущен. Откройте его в Telegram и отправьте ответ ИИ. Ctrl+C — остановить.")
        offset = 0
        while True:
            try:
                updates = await self.call("getUpdates", offset=offset, timeout=30) or []
            except httpx.HTTPError as e:
                log.warning("getUpdates: %s — повтор через 5 с", e)
                await asyncio.sleep(5)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                asyncio.create_task(handle_update(self, u))




def webhook_secret(token: str) -> str:
    """Секретная часть адреса webhook — чтобы чужие не могли слать боту поддельные сообщения."""
    return hashlib.sha256(("senim-webhook:" + token).encode()).hexdigest()[:32]


async def handle_update(bot: "Bot", update: dict) -> None:
    msg = update.get("message") or update.get("edited_message")
    if msg:
        await bot.handle(msg)
    elif update.get("callback_query"):
        await bot.handle_callback(update["callback_query"])


async def set_webhook(bot: "Bot", public_url: str, token: str) -> bool:
    url = f"{public_url.rstrip('/')}/api/telegram/{webhook_secret(token)}"
    res = await bot.call("setWebhook", url=url, allowed_updates=["message", "edited_message", "callback_query"],
                         drop_pending_updates=False)
    log.info("Telegram webhook %s: %s", "set" if res else "NOT set", url.rsplit("/", 1)[0] + "/…")
    return bool(res)
