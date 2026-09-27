"""Senim API. Запуск: uvicorn backend.app.main:app --reload (из корня проекта)."""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import cache, ratelimit, stats
from . import telegram as tg
from .config import ROOT_DIR, get_settings
from .llm import get_router
from .pipeline import citations as cit
from .pipeline import nli
from .pipeline.orchestrator import run_check, run_check_full
from .pipeline.search import http_client
from .pipeline.text_utils import split_sentences
from .schemas import CheckRequest, CitationsRequest, FeedbackRequest

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

app = FastAPI(title="Senim — AI Trust", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def no_stale_cache(request, call_next):
    """Браузер всегда сверяет страницу и её JS/CSS с сервером (по ETag): после обновления
    сайта никто не увидит смесь старых и новых файлов."""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response

FRONTEND = ROOT_DIR / "frontend"
EXTENSION = ROOT_DIR / "extension"
DATA = ROOT_DIR / "data"


def _guard(request: Request, lang: str | None) -> JSONResponse | None:
    """Лимит платных проверок. С этого компьютера (localhost) — без ограничений."""
    key = ratelimit.client_key(request)
    if ratelimit.is_local(key):
        return None
    try:
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


@app.get("/api/health")
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


@app.post("/api/check")
async def check(req: CheckRequest, request: Request):
    if (blocked := _guard(request, req.ui_lang.value if req.ui_lang else None)):
        return blocked
    return await run_check_full(req.text, req.question, req.ui_lang.value if req.ui_lang else None, channel="api")


@app.post("/api/check/stream")
async def check_stream(req: CheckRequest, request: Request, channel: str = "web"):
    if (blocked := _guard(request, req.ui_lang.value if req.ui_lang else None)):
        return blocked
    channel = channel if channel in ("web", "extension", "telegram") else "web"

    async def gen():
        async for ev in run_check(req.text, req.question, req.ui_lang.value if req.ui_lang else None, channel):
            yield json.dumps(ev, ensure_ascii=False) + "\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


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
                if f.suffix == ".js":
                    data = data.replace(b"http://127.0.0.1:8000", base.encode())
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


@app.get("/")
async def index():
    return FileResponse(FRONTEND / "index.html")


app.mount("/static", StaticFiles(directory=FRONTEND), name="static")
