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

from . import classroom, ratelimit, stats
from .config import get_settings
from .pipeline.orchestrator import run_check_full
from . import ocr
from .pipeline.text_utils import detect_lang

log = logging.getLogger("senim.bot")

ICON = {"supported": "🟢", "disputed": "🟡", "contradicted": "🔴", "unverifiable": "⚪"}
T = {
    "kk": {
        "start": "Сәлем! Мен <b>Senim</b> — ЖИ жауаптарын тексеремін.\n\nChatGPT, Gemini немесе басқа ЖИ жауабын маған жіберіңіз — мәтін, скриншот, фото, PDF немесе Word. Мен әр тұжырымды дереккөздермен салыстырып, неге сенуге болатынын не болмайтынын түсіндіремін.",
        "wait": "🔎 Тексеріп жатырмын… (10–30 сек)",
        "short": "Тексеру үшін ЖИ жауабының мәтінін жіберіңіз (кемінде 10 таңба).",
        "band": {"high": "Сүйенуге болады", "medium": "Мұқият тексеріңіз", "low": "Тексермей қолданбаңыз", "na": "Тексерілетін факт табылмады"},
        "why": "Неге", "fix": "Дұрысы", "src": "Дереккөз", "refs": "Сілтемелер",
        "ref": {"verified": "✅ бар", "mismatch": "🟡 бар, бірақ бұрмаланған", "doi_not_found": "❌ DOI жоқ — ойдан шығарылған",
                "not_found": "⚪ базалардан табылмады", "error": "⚪ тексеру мүмкін болмады"},
        "more": "Толық талдау", "fail": "Кешіріңіз, тексеру сәтсіз аяқталды. Кейінірек қайталап көріңіз.",
        "class_ok": "✅ Сіз «{name}» сыныбындасыз ({student}). Енді тексерулеріңізді мұғалім көреді. Шығу: /leave",
        "class_bad": "Сынып табылмады. Мұғалімнен кодты сұраңыз. Мысалы: /class ABC234 Айгерім",
        "class_left": "Сыныптан шықтыңыз.",
        "class_help": "Сыныпқа қосылу: /class КОД Атыңыз",
        "in_class": "📚 Сынып: {name}",
        "fb_useful": "Көмектесті ме?", "fb_notice": "Қатені өзіңіз байқар ма едіңіз?", "yes": "Иә", "no": "Жоқ", "thanks": "Рақмет!",
        "photo_wait": "📷 Суреттегі мәтінді оқып жатырмын…", "photo_none": "Суреттен мәтін табылмады. Анығырақ түсіріп көріңіз немесе мәтінді жіберіңіз.", "photo_fail": "Суретті тану мүмкін болмады. Мәтінді көшіріп жіберіңіз.",
        "file_wait": "📄 Файлды оқып жатырмын…", "file_scanned": "PDF ішінде мәтін жоқ (скан). Беттің скриншотын жіберіңіз.", "file_type": "Фото, PDF, Word (.docx) немесе TXT жіберіңіз.", "file_big": "Файл тым үлкен (10 МБ-қа дейін).",
    },
    "ru": {
        "start": "Привет! Я <b>Senim</b> — проверяю ответы ИИ.\n\nПришлите мне ответ ChatGPT, Gemini или другого ИИ — текстом, скриншотом, фото, PDF или Word. Я сверю каждое утверждение с источниками и объясню, почему ему можно или нельзя доверять.",
        "wait": "🔎 Проверяю… (10–30 сек)",
        "short": "Пришлите текст ответа ИИ для проверки (от 10 символов).",
        "band": {"high": "Можно опираться", "medium": "Проверяй внимательно", "low": "Не используй без проверки", "na": "Проверяемых фактов не найдено"},
        "why": "Почему", "fix": "Как правильно", "src": "Источник", "refs": "Ссылки",
        "ref": {"verified": "✅ существует", "mismatch": "🟡 существует, но искажена", "doi_not_found": "❌ DOI не существует — выдумка",
                "not_found": "⚪ нет в научных базах", "error": "⚪ не удалось проверить"},
        "more": "Полный разбор", "fail": "Извините, проверка не удалась. Попробуйте чуть позже.",
        "class_ok": "✅ Вы в классе «{name}» ({student}). Теперь ваши проверки увидит учитель. Выйти: /leave",
        "class_bad": "Класс не найден. Попросите код у учителя. Пример: /class ABC234 Айгерим",
        "class_left": "Вы вышли из класса.",
        "class_help": "Войти в класс: /class КОД Имя",
        "in_class": "📚 Класс: {name}",
        "fb_useful": "Помогло?", "fb_notice": "Заметили бы ошибку сами?", "yes": "Да", "no": "Нет", "thanks": "Спасибо!",
        "photo_wait": "📷 Читаю текст на фото…", "photo_none": "На фото не найден текст. Снимите чётче или пришлите текст.", "photo_fail": "Не удалось распознать фото. Пришлите текст ответа.",
        "file_wait": "📄 Читаю файл…", "file_scanned": "В PDF нет текста (это скан). Пришлите скриншот страницы.", "file_type": "Пришлите фото, PDF, Word (.docx) или TXT.", "file_big": "Файл слишком большой (до 10 МБ).",
    },
    "en": {
        "start": "Hi! I'm <b>Senim</b> — I check AI answers.\n\nSend me an answer from ChatGPT, Gemini or another AI — as text, a screenshot, a photo, PDF or Word. I'll check every claim against sources and explain why you can or can't trust it.",
        "wait": "🔎 Checking… (10–30 s)",
        "short": "Send the AI answer text to check (at least 10 characters).",
        "band": {"high": "Safe to rely on", "medium": "Check carefully", "low": "Don't use without checking", "na": "No checkable facts found"},
        "why": "Why", "fix": "Correct", "src": "Source", "refs": "References",
        "ref": {"verified": "✅ exists", "mismatch": "🟡 exists, but distorted", "doi_not_found": "❌ DOI doesn't exist — fabricated",
                "not_found": "⚪ not in databases", "error": "⚪ couldn't check"},
        "more": "Full report", "fail": "Sorry, the check failed. Please try again later.",
        "class_ok": "✅ You joined class “{name}” ({student}). Your teacher will see your checks. Leave: /leave",
        "class_bad": "Class not found. Ask your teacher for the code. Example: /class ABC234 Aigerim",
        "class_left": "You left the class.",
        "class_help": "Join a class: /class CODE Name",
        "in_class": "📚 Class: {name}",
        "fb_useful": "Helpful?", "fb_notice": "Would you have noticed?", "yes": "Yes", "no": "No", "thanks": "Thanks!",
        "photo_wait": "📷 Reading the text in the photo…", "photo_none": "No text found in the photo. Try a sharper shot or send the text.", "photo_fail": "Couldn't read the photo. Please send the text instead.",
        "file_wait": "📄 Reading the file…", "file_scanned": "This PDF has no text (it is a scan). Send a screenshot of the page.", "file_type": "Send a photo, PDF, Word (.docx) or TXT file.", "file_big": "The file is too large (up to 10 MB).",
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
        if (msg.get("photo") or msg.get("document")) and not text.startswith("/"):
            text = await self._photo_text(msg, chat)
            if text is None:
                return
        lang = detect_lang(text) if text and not text.startswith("/") else (
            "kk" if (msg.get("from") or {}).get("language_code") == "kk" else
            "en" if (msg.get("from") or {}).get("language_code") == "en" else "ru")
        t = T[lang]
        if not text or text.startswith("/start") or text.startswith("/help"):
            # ссылка вида t.me/bot?start=class_ABC234 сразу приглашает в класс
            arg = text.split(maxsplit=1)[1] if text.startswith("/start ") else ""
            if arg.lower().startswith("class_"):
                name = ((msg.get("from") or {}).get("first_name") or "")
                await self._join(chat, t, arg[6:], name)
                return
            await self.call("sendMessage", chat_id=chat, text=t["start"] + "\n\n" + t["class_help"], parse_mode="HTML")
            return
        if text.startswith("/class"):
            parts = text.split(maxsplit=2)
            if len(parts) < 2:
                await self.call("sendMessage", chat_id=chat, text=t["class_help"])
                return
            name = parts[2] if len(parts) > 2 else ((msg.get("from") or {}).get("first_name") or "")
            await self._join(chat, t, parts[1], name)
            return
        if text.startswith("/leave"):
            classroom.tg_leave(chat)
            await self.call("sendMessage", chat_id=chat, text=t["class_left"])
            return
        if len(text) < 10:
            await self.call("sendMessage", chat_id=chat, text=t["short"])
            return
        member = None
        try:
            member = classroom.tg_membership(chat)
        except Exception as e:  # noqa: BLE001
            log.warning("class lookup failed: %s", e)
        code, student = member if member else ("", "")
        try:
            if code:
                ratelimit.check_and_count_class(code, student)
            else:
                ratelimit.check_and_count(f"tg:{chat}")
        except ratelimit.RateLimited as e:
            await self.call("sendMessage", chat_id=chat, text=e.message(lang))
            return
        wait = await self.call("sendMessage", chat_id=chat, text=t["wait"], reply_to_message_id=msg["message_id"])
        async with self.sem:
            try:
                out = await run_check_full(text[:12000], channel="telegram", class_code=code, student=student)
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

    async def _photo_text(self, msg: dict, chat) -> str | None:
        """Фото, скриншот или файл (PDF, Word, TXT) → текст (бета). None — ответ пользователю уже отправлен."""
        lang = ("kk" if (msg.get("from") or {}).get("language_code") == "kk" else
                "en" if (msg.get("from") or {}).get("language_code") == "en" else "ru")
        t = T[lang]
        if msg.get("photo"):
            best = max(msg["photo"], key=lambda p: p.get("file_size") or 0)  # самое чёткое из размеров
            file_id, name, mime = best["file_id"], "photo.jpg", "image/jpeg"
        else:
            doc = msg["document"]
            file_id, name = doc["file_id"], doc.get("file_name") or ""
            mime = (doc.get("mime_type") or "").lower()
            if (doc.get("file_size") or 0) > ocr.MAX_BYTES:
                await self.call("sendMessage", chat_id=chat, text=t["file_big"])
                return None
            if mime not in ocr.ALLOWED_MIME | {"application/pdf", ocr.DOCX_MIME, "text/plain", "text/markdown"}:
                ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
                mime = ocr.EXT_MIME.get(ext, "")
            if not mime:
                await self.call("sendMessage", chat_id=chat, text=t["file_type"])
                return None
        if not ratelimit.hit(f"ocr:tg:{chat}", 40):
            await self.call("sendMessage", chat_id=chat, text=t["photo_fail"])
            return None
        is_image = mime in ocr.ALLOWED_MIME
        note = await self.call("sendMessage", chat_id=chat, text=t["photo_wait"] if is_image else t["file_wait"],
                               reply_to_message_id=msg["message_id"])
        err = None
        out = None
        try:
            f = await self.call("getFile", file_id=file_id)
            url = self.api.replace("/bot", "/file/bot", 1) + "/" + f["file_path"]
            r = await self.client.get(url)
            r.raise_for_status()
            if len(r.content) > ocr.MAX_BYTES:
                raise ocr.OCRError("too_big")
            out = await ocr.extract_any(mime, r.content)
        except ocr.OCRError as e:
            err = e.code
            log.warning("file extract failed: %s", e)
        except Exception as e:  # noqa: BLE001
            err = "fail"
            log.warning("file extract failed: %s", e)
        if note:
            await self.call("deleteMessage", chat_id=chat, message_id=note["message_id"])
        if out is None:
            key = {"scanned_pdf": "file_scanned", "too_big": "file_big", "bad_type": "file_type"}.get(err, "photo_fail")
            await self.call("sendMessage", chat_id=chat, text=t[key])
            return None
        if len(out["text"]) < 10:
            await self.call("sendMessage", chat_id=chat, text=t["photo_none"])
            return None
        return out["text"]

    async def _join(self, chat, t: dict, code: str, name: str):
        cls = None
        try:
            cls = classroom.tg_join(chat, code, name)
        except Exception as e:  # noqa: BLE001
            log.warning("class join failed: %s", e)
        if cls:
            await self.call("sendMessage", chat_id=chat,
                            text=t["class_ok"].format(name=esc(cls["name"]), student=esc(classroom.clean_student(name))))
        else:
            await self.call("sendMessage", chat_id=chat, text=t["class_bad"])

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
