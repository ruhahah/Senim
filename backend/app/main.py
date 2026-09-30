"""Senim API. Запуск: uvicorn backend.app.main:app --reload (из корня проекта)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import cache, classroom, ratelimit, stats, trainer
from . import telegram as tg
from .config import ROOT_DIR, get_settings
from .llm import get_router
from .pipeline import citations as cit
from .pipeline import nli
from .pipeline.orchestrator import run_check, run_check_full
from .pipeline.search import http_client
from .pipeline.text_utils import split_sentences
from .schemas import (CheckRequest, CitationsRequest, ClassCreateRequest, FeedbackRequest,
                      ThinkResultRequest, TrainerAnswerRequest, TrainerClassRequest, TrainerNewRequest)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

app = FastAPI(title="Senim — AI Trust", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def no_stale_cache(request, call_next):
    """Браузер всегда сверяет страницу и её JS/CSS с сервером (по ETag): после обновления
    сайта никто не увидит смесь старых и новых файлов."""
    response = await call_next(request)
    path = request.url.path
    if path in ("/", "/teacher", "/rating", "/trainer", "/privacy") or path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response

FRONTEND = ROOT_DIR / "frontend"
EXTENSION = ROOT_DIR / "extension"
DATA = ROOT_DIR / "data"


def _guard(request: Request, lang: str | None, class_code: str = "", student: str = "") -> JSONResponse | None:
    """Лимит платных проверок. С этого компьютера (localhost) — без ограничений.
    Ученики класса считаются по классу и имени (весь класс может сидеть за одним IP школы)."""
    key = ratelimit.client_key(request)
    if ratelimit.is_local(key):
        return None
    try:
        if class_code and classroom.get_class(class_code):
            ratelimit.check_and_count_class(classroom.normalize_code(class_code), classroom.clean_student(student))
        else:
            ratelimit.check_and_count(key)
    except ratelimit.RateLimited as e:
        return JSONResponse(status_code=429, headers={"Retry-After": str(e.retry_after)},
                            content={"detail": {"code": f"rate_limit_{e.kind}", "message": e.message(lang or "ru")}})
    return None


# ---------------------------------------------------------------- Telegram webhook
_tg_bot: tg.Bot | None = None
_tg_tasks: set = set()


def _telegram_enabled() -> bool:
    s = get_settings()
    return s.telegram_mode == "webhook" and bool(s.telegram_bot_token)


@app.on_event("startup")
async def _setup_telegram_webhook():
    """В режиме webhook сообщаем Telegram адрес сайта — бот работает без отдельного процесса."""
    global _tg_bot
    s = get_settings()
    if not _telegram_enabled():
        return
    _tg_bot = tg.Bot(s.telegram_bot_token)
    if not s.public_url:
        logging.getLogger("senim.bot").warning("TELEGRAM_MODE=webhook, но PUBLIC_URL пуст — webhook не установлен")
        return
    try:
        await tg.set_webhook(_tg_bot, s.public_url, s.telegram_bot_token)
    except Exception as e:  # noqa: BLE001
        logging.getLogger("senim.bot").warning("setWebhook failed: %s", e)


_warm_tasks: set = set()


async def warm_examples() -> None:
    """Прогрев: примеры с сайта проверяются один раз при запуске сервера и ложатся в кэш.
    Когда жюри нажимает «Абай» → «Проверить», ответ приходит мгновенно."""
    log = logging.getLogger("senim.warmup")
    try:
        examples = json.loads((DATA / "examples.json").read_text("utf-8")).get("examples", [])
    except Exception:  # noqa: BLE001
        return
    for ui in ("ru", "kk", "en"):
        async def one(ex: dict) -> None:
            try:
                await run_check_full(ex["text"], ex.get("question", ""), ui, channel="warmup")
            except Exception as e:  # noqa: BLE001 — прогрев не должен ронять сервер
                log.info("warmup %s/%s failed: %s", ex.get("id"), ui, e)
        await asyncio.gather(*(one(ex) for ex in examples))
    log.info("warmup done: %d examples × 3 languages", len(examples))


@app.on_event("startup")
async def _start_warmup():
    s = get_settings()
    if not s.warm_examples or os.environ.get("PYTEST_CURRENT_TEST") or not get_router().available:
        return
    t = asyncio.create_task(warm_examples())
    _warm_tasks.add(t)
    t.add_done_callback(_warm_tasks.discard)


@app.post("/api/telegram/{secret}")
async def telegram_webhook(secret: str, request: Request):
    global _tg_bot
    s = get_settings()
    if not _telegram_enabled() or secret != tg.webhook_secret(s.telegram_bot_token):
        raise HTTPException(404)
    if _tg_bot is None:
        _tg_bot = tg.Bot(s.telegram_bot_token)
    update = await request.json()
    # отвечаем Telegram сразу, проверку делаем в фоне (она занимает 10–30 с)
    task = asyncio.create_task(tg.handle_update(_tg_bot, update))
    _tg_tasks.add(task)
    task.add_done_callback(_tg_tasks.discard)
    return {"ok": True}


@app.api_route("/api/health", methods=["GET", "HEAD"])
async def health():
    s = get_settings()
    r = get_router()
    return {
        "ok": True,
        "llm_available": r.available,
        "llm_chain": r.label,
        "keys": {
            "gemini": bool(s.gemini_api_key), "anthropic": bool(s.anthropic_api_key),
            "openai": bool(s.openai_api_key), "groq": bool(s.groq_api_key),
            "openrouter": bool(s.openrouter_api_key), "tavily": bool(s.tavily_api_key),
        },
        "nli": nli.enabled() if s.nli_backend == "local" else False,
        "telegram_mode": s.telegram_mode if s.telegram_bot_token else "off",
        "links": {
            "telegram_bot": f"https://t.me/{s.telegram_bot_username.lstrip('@')}" if s.telegram_bot_username else "",
            "extension_zip": "/api/extension.zip" if (EXTENSION / "manifest.json").exists() else "",
        },
    }


def _class_of(req: CheckRequest) -> str:
    code = classroom.normalize_code(req.class_code)
    return code if code and classroom.get_class(code) else ""


@app.post("/api/check")
async def check(req: CheckRequest, request: Request, channel: str = "api"):
    code = _class_of(req)
    if (blocked := _guard(request, req.ui_lang.value if req.ui_lang else None, code, req.student)):
        return blocked
    return await run_check_full(req.text, req.question, req.ui_lang.value if req.ui_lang else None,
                                channel=channel if channel in ("api", "extension", "telegram", "web") else "api", class_code=code, student=req.student,
                                source_ai=req.source_ai)


@app.post("/api/check/stream")
async def check_stream(req: CheckRequest, request: Request, channel: str = "web"):
    code = _class_of(req)
    if (blocked := _guard(request, req.ui_lang.value if req.ui_lang else None, code, req.student)):
        return blocked
    channel = channel if channel in ("web", "extension", "telegram") else "web"

    async def gen():
        async for ev in run_check(req.text, req.question, req.ui_lang.value if req.ui_lang else None, channel,
                                  code, req.student, req.source_ai):
            yield json.dumps(ev, ensure_ascii=False) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


# ---------------------------------------------------------------- режим учителя
@app.post("/api/class")
async def class_create(req: ClassCreateRequest, request: Request):
    """Учитель создаёт класс: код для учеников + секретный ключ для панели."""
    if not ratelimit.hit("class_create:" + ratelimit.client_key(request), 10):
        raise HTTPException(429, "Слишком много новых классов за час")
    c = classroom.create_class(req.name)
    return {**c, "join_path": f"/?class={c['code']}", "dashboard_path": f"/teacher?code={c['code']}&key={c['key']}"}


@app.get("/api/class/{code}")
async def class_info(code: str):
    c = classroom.get_class(code)
    if not c:
        raise HTTPException(404, "class not found")
    return {"code": c["code"], "name": c["name"]}


@app.get("/api/class/{code}/dashboard")
async def class_dashboard(code: str, key: str = ""):
    c = classroom.get_class(code)
    if not c:
        raise HTTPException(404, "class not found")
    if not classroom.check_key(c, key):
        raise HTTPException(403, "wrong key")
    return {"class": {"code": c["code"], "name": c["name"], "created": c["created"]}, **classroom.dashboard(c["code"]),
            "trainer": trainer.class_rounds(c["code"])}


@app.post("/api/class/{code}/think")
async def class_think(code: str, req: ThinkResultRequest):
    if not classroom.record_think(code, req.student, req.caught, req.missed, req.false_alarms):
        raise HTTPException(404, "class not found")
    return {"ok": True}


@app.get("/api/qr.svg")
async def qr_svg(text: str):
    """QR-код (SVG) для ссылки-приглашения в класс — показывается на доске/проекторе."""
    import io

    import qrcode
    import qrcode.image.svg
    from fastapi.responses import Response

    if len(text) > 300:
        raise HTTPException(400, "too long")
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


# ---------------------------------------------------------------- тренажёр «Найди ложь ИИ»
@app.post("/api/trainer/new")
async def trainer_new(req: TrainerNewRequest, request: Request):
    """Новый раунд для одиночной игры: сначала берём уже составленный, иначе составляем."""
    rid = trainer.reuse_round(req.lang, req.topic)
    if not rid:
        if (blocked := _guard(request, req.lang)):
            return blocked
        try:
            payload = await trainer.generate(req.topic, req.lang, req.n_errors)
        except trainer.TrainerError as e:
            raise HTTPException(503, str(e))
        rid = trainer.save_round(payload)
    return {"id": rid}


@app.post("/api/class/{code}/trainer")
async def trainer_for_class(code: str, req: TrainerClassRequest):
    """Учитель запускает раунд для своего класса."""
    c = classroom.get_class(code)
    if not c:
        raise HTTPException(404, "class not found")
    if not classroom.check_key(c, req.key):
        raise HTTPException(403, "wrong key")
    if not ratelimit.hit("trainer_class:" + c["code"], 30):
        raise HTTPException(429, "Слишком много раундов за час")
    try:
        payload = await trainer.generate(req.topic, req.lang, req.n_errors)
    except trainer.TrainerError as e:
        raise HTTPException(503, str(e))
    rid = trainer.save_round(payload, c["code"])
    return {"id": rid, "path": f"/trainer?r={rid}"}


@app.get("/api/trainer/topics")
async def trainer_topics(lang: str = "ru"):
    from .pipeline.search import load_kb
    return {"topics": [e["title"] for e in load_kb() if e.get("lang") == lang]}


@app.get("/api/trainer/{rid}")
async def trainer_get(rid: str):
    r = trainer.load_round(rid)
    if not r:
        raise HTTPException(404, "round not found")
    return trainer.public_view(r)


@app.post("/api/trainer/{rid}/answer")
async def trainer_answer(rid: str, req: TrainerAnswerRequest):
    r = trainer.load_round(rid)
    if not r:
        raise HTTPException(404, "round not found")
    res = trainer.score_answer(r, req.marked, req.seconds)
    trainer.record_result(r, req.student, res, req.seconds)
    return {**res, "leaderboard": trainer.leaderboard(rid)}


@app.get("/api/trainer/{rid}/leaderboard")
async def trainer_board(rid: str):
    if not trainer.load_round(rid):
        raise HTTPException(404, "round not found")
    return {"leaderboard": trainer.leaderboard(rid)}


@app.get("/trainer")
async def trainer_page():
    return FileResponse(FRONTEND / "trainer.html")


@app.get("/api/rating")
async def rating():
    """Рейтинг ИИ по достоверности: лабораторный тест (data/ai_rating.json) + живые проверки пользователей."""
    path = DATA / "ai_rating.json"
    lab = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    return {"lab": lab, "live": stats.live_rating(), "sources": stats.SOURCE_AI_NAMES}


@app.get("/rating")
async def rating_page():
    return FileResponse(FRONTEND / "rating.html")


@app.get("/privacy")
async def privacy_page():
    return FileResponse(FRONTEND / "privacy.html")


@app.get("/teacher")
async def teacher_page():
    return FileResponse(FRONTEND / "teacher.html")


@app.post("/api/citations")
async def citations(req: CitationsRequest, request: Request):
    if (blocked := _guard(request, None)):
        return blocked
    """Проверка ссылок — работает даже без ключа ИИ."""
    router = get_router()
    cites = await cit.extract_citations(req.text, router if router.available else None)
    async with http_client() as client:
        results = await cit.check_citations(client, cites)
    return {"citations": [r.model_dump(mode="json") for r in results]}


@app.get("/api/examples")
async def examples():
    return json.loads((DATA / "examples.json").read_text(encoding="utf-8"))


@app.get("/api/demo/{demo_id}")
async def demo(demo_id: str):
    """Воспроизводит заранее подготовленный результат с анимацией — для показа без ключей."""
    path = DATA / "demo" / f"{demo_id}.json"
    if not path.exists() or "/" in demo_id or ".." in demo_id:
        raise HTTPException(404, "demo not found")
    d = json.loads(path.read_text(encoding="utf-8"))

    async def gen():
        sentences = [s.model_dump(mode="json") for s in split_sentences(d["text"])]
        yield json.dumps({"type": "start", "lang": d["lang"], "sentences": sentences,
                          "provider": "demo", "llm_available": True, "demo": True}, ensure_ascii=False) + "\n"
        await asyncio.sleep(0.6)
        yield json.dumps({"type": "claims", "claims": d["claims"]}, ensure_ascii=False) + "\n"
        for r in d["results"]:
            await asyncio.sleep(0.45)
            yield json.dumps({"type": "claim_result", "result": r}, ensure_ascii=False) + "\n"
        await asyncio.sleep(0.4)
        yield json.dumps({"type": "citations", "results": d["citations"]}, ensure_ascii=False) + "\n"
        yield json.dumps({"type": "done", "trust": d["trust"], "elapsed_ms": d.get("elapsed_ms", 0)},
                         ensure_ascii=False) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


@app.post("/api/feedback")
async def feedback(req: FeedbackRequest):
    """Ответ на мини-опрос после проверки (только два ответа, без текста и личных данных)."""
    if req.useful is None and req.would_notice is None:
        raise HTTPException(400, "empty feedback")
    stats.record_feedback(req.channel, req.lang.value if req.lang else "", req.useful, req.would_notice)
    return {"ok": True}


@app.get("/api/extension.zip")
async def extension_zip(request: Request):
    """Архив расширения Chrome, уже настроенный на адрес этого сайта."""
    import io
    import zipfile
    from fastapi.responses import Response

    if not (EXTENSION / "manifest.json").exists():
        raise HTTPException(404, "extension not bundled")
    base = get_settings().public_url or str(request.base_url)
    base = base.rstrip("/")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in EXTENSION.rglob("*"):
            if f.is_file():
                data = f.read_bytes()
                if f.suffix in (".js", ".html"):
                    # адрес сервера по умолчанию → адрес именно этого сайта
                    data = re.sub(rb'const DEFAULT_SERVER = "[^"]*"', b'const DEFAULT_SERVER = "' + base.encode() + b'"', data)
                    data = re.sub(rb'placeholder="https?://[^"]*"', b'placeholder="' + base.encode() + b'"', data)
                z.writestr(f"senim-extension/{f.relative_to(EXTENSION).as_posix()}", data)
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="senim-extension.zip"'})


@app.get("/api/stats")
async def get_stats():
    """Эффект Senim в цифрах: проверки, пойманные ошибки, средняя стоимость проверки."""
    return stats.totals()


@app.post("/api/cache/clear")
async def cache_clear(request: Request):
    if not ratelimit.is_local(ratelimit.client_key(request)):
        raise HTTPException(403, "only from localhost")
    return {"deleted": cache.clear()}


@app.api_route("/", methods=["GET", "HEAD"])
async def index():
    return FileResponse(FRONTEND / "index.html")


app.mount("/static", StaticFiles(directory=FRONTEND), name="static")
