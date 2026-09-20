import asyncio
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .domain import Persona, iso, uid

HERE = Path(__file__).parent


class Incoming(BaseModel):
    session: str
    content: str = Field(min_length=1, max_length=20000)
    source_key: str = Field(min_length=1, max_length=200)


def make_app(engine, run_worker=True):
    db, settings = engine.db, engine.settings
    token_path = settings.data_dir / "access.token"
    if not token_path.exists():
        token_path.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    token = token_path.read_text("utf-8").strip()

    async def drain_inbox():
        while True:
            for path in sorted((settings.data_dir / "inbox").glob("*.json")):
                try:
                    data = Incoming.model_validate_json(path.read_text("utf-8"))
                    await engine.receive(data.session, data.content, data.source_key)
                    path.unlink()
                except Exception as e:
                    db.audit("inbox.error", {"type": type(e).__name__, "file": path.name})
                    path.rename(path.with_suffix(".rejected"))
            await asyncio.sleep(2)

    @asynccontextmanager
    async def lifespan(app):
        tasks = [asyncio.create_task(engine.run()), asyncio.create_task(drain_inbox())] if run_worker else []
        yield
        engine.stop_event.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="AICHAT / cc-chat", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.middleware("http")
    async def access(request, call_next):
        if request.url.path.startswith(("/api/", "/internal/")):
            supplied = request.headers.get("authorization", "").removeprefix("Bearer ")
            supplied = supplied or request.headers.get("x-cc-chat-token", "")
            if not secrets.compare_digest(supplied, token):
                return JSONResponse({"detail": "本机访问令牌不匹配"}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "不接受跨站修改"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'"
        )
        return response

    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")

    @app.get("/health")
    def health():
        return {"ok": True, "service": "cc-chat", "transport": settings.transport}

    @app.get("/")
    @app.get("/persona")
    @app.get("/life")
    @app.get("/memories")
    def page(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="app.html",
            context={"page": request.url.path, "token": token, "transport": settings.transport},
        )

    @app.get("/api/status")
    def status():
        return {
            "persona": db.persona(),
            "state": db.get("state"),
            "paused": db.get("paused"),
            "error": db.get("last_error"),
            "transport": settings.transport,
            "revision": db.get("revision"),
            "plans": db.rows("SELECT * FROM jobs WHERE status='pending' ORDER BY due"),
            "outbox": db.rows("SELECT * FROM outbox ORDER BY rowid DESC LIMIT 30"),
            "messages": db.rows("SELECT * FROM messages ORDER BY at DESC,rowid DESC LIMIT 30"),
            "audit": db.rows("SELECT * FROM audit ORDER BY id DESC LIMIT 30"),
            "counts": {
                "memories": db.one("SELECT COUNT(*) AS n FROM memories WHERE status='active'")["n"],
                "diaries": db.one("SELECT COUNT(*) AS n FROM diaries")["n"],
            },
        }

    @app.post("/internal/incoming")
    async def incoming(body: Incoming):
        try:
            return await engine.receive(body.session, body.content, body.source_key)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/internal/cancel")
    async def cancel(request: Request):
        body = await request.json()
        if body.get("session") != db.get("session"):
            raise HTTPException(400, "会话不匹配")
        async with engine.gate:
            db.set("revision", db.get("revision") + 1)
            db.execute("UPDATE jobs SET status='cancelled' WHERE status='pending'")
            db.execute("UPDATE outbox SET status='cancelled' WHERE status='pending'")
        return {"ok": True}

    @app.post("/api/pause")
    async def pause(request: Request):
        body = await request.json()
        if not isinstance(body.get("paused"), bool):
            raise HTTPException(400, "paused 必须为布尔值")
        await engine.pause(body["paused"])
        return {"ok": True}

    @app.post("/api/chat")
    async def chat(request: Request):
        if settings.transport != "mock":
            raise HTTPException(400, "真实微信模式请从微信发消息；本地测试不会冒充微信用户")
        body = await request.json()
        return await engine.receive("mock:owner", str(body.get("content", "")), uid())

    @app.post("/api/retry")
    async def retry():
        engine.cooldown = None
        engine.model_failures = 0
        # Retry reasoning, never retry an uncertain send.
        if db.get("session"):
            db.add_job("reply", engine.clock(), {"recovery": True})
        return {"ok": True}

    @app.get("/api/persona")
    def persona():
        return {"current": db.persona(), "history": db.rows("SELECT * FROM personas ORDER BY id DESC")}

    @app.put("/api/persona")
    async def save_persona(body: Persona):
        async with engine.gate:
            db.save_persona(body.model_dump(), engine.clock())
            db.set("schedule_day", None)
            db.set("state", "个人设定已更新，正在重新安排今天的生活")
        return {"ok": True}

    @app.get("/api/life")
    def life():
        return {
            "events": db.rows("SELECT * FROM events ORDER BY at DESC LIMIT 150"),
            "diaries": db.rows("SELECT * FROM diaries ORDER BY created DESC LIMIT 60"),
        }

    @app.get("/api/memories")
    def memories(q: str = ""):
        return {
            "items": db.rows(
                "SELECT * FROM memories WHERE content LIKE ? ORDER BY created DESC LIMIT 200",
                ("%" + q + "%",),
            ),
            "links": db.rows("SELECT * FROM links"),
            "retrievals": db.rows("SELECT * FROM retrievals ORDER BY at DESC LIMIT 12"),
            "weights": settings.weights,
            "compress_days": settings.compress_days,
            "forget_days": settings.forget_days,
            "low_importance": settings.low_importance,
        }

    @app.post("/api/memories/search")
    async def search(request: Request):
        body = await request.json()
        return {"results": await engine.memory.search(str(body.get("query", "")), engine.clock())}

    @app.post("/api/memories/{memory_id}/pin")
    async def pin(memory_id: str, request: Request):
        body = await request.json()
        db.execute("UPDATE memories SET pinned=? WHERE id=?", (int(bool(body.get("pinned"))), memory_id))
        return {"ok": True}

    @app.put("/api/memories/{memory_id}")
    async def correct(memory_id: str, request: Request):
        from .domain import MemoryDraft

        body = await request.json()
        old = db.one("SELECT * FROM memories WHERE id=?", (memory_id,))
        if not old or old["status"] != "active":
            raise HTTPException(404)
        content = str(body.get("content", "")).strip()
        if not content:
            raise HTTPException(400, "内容不能为空")
        mid, now = uid(), engine.clock()
        db.execute(
            "INSERT INTO messages VALUES (?,?,?,?,?,?,0,?)",
            (mid, "admin", "user", content, iso(now), db.get("revision"), "admin:" + mid),
        )
        draft = MemoryDraft(
            content=content,
            subject=old["subject"],
            kind=old["kind"],
            source_type="user",
            source_ids=[mid],
            occurred_at=iso(now),
            importance=old["importance"],
            confidence=1,
            supersedes=memory_id,
            explicit_correction=True,
        )
        async with engine.gate:
            db.set("revision", db.get("revision") + 1)
            db.execute("UPDATE jobs SET status='cancelled' WHERE status='pending'")
            db.execute("UPDATE outbox SET status='cancelled' WHERE status='pending'")
            result = await engine.memory.add(draft, [mid], now)
            db.set("state", "正在继续今天的生活")
        return {"id": result}

    @app.delete("/api/memories/{memory_id}")
    async def delete(memory_id: str):
        old = db.one("SELECT * FROM memories WHERE id=?", (memory_id,))
        if not old:
            raise HTTPException(404)
        async with engine.gate:
            engine.memory.delete(old)
            db.set("revision", db.get("revision") + 1)
            db.execute("UPDATE jobs SET status='cancelled' WHERE status='pending'")
            db.execute("UPDATE outbox SET status='cancelled' WHERE status='pending'")
        return {"ok": True}

    @app.put("/api/settings")
    async def tune(request: Request):
        body = await request.json()
        candidate = settings.model_copy(deep=True)
        for key in ("compress_days", "forget_days", "low_importance", "weights"):
            if key in body:
                setattr(candidate, key, body[key])
        try:
            candidate = type(settings).model_validate(candidate.model_dump())
        except ValidationError as e:
            raise HTTPException(400, "参数类型错误") from e
        if not 0 < candidate.compress_days < candidate.forget_days or not 0 <= candidate.low_importance <= 1:
            raise HTTPException(400, "遗忘参数范围错误")
        if (
            set(candidate.weights) != {"semantic", "connection", "keyword", "time"}
            or any(v < 0 for v in candidate.weights.values())
            or sum(candidate.weights.values()) <= 0
        ):
            raise HTTPException(400, "权重必须非负且总和大于零")
        for key in ("compress_days", "forget_days", "low_importance", "weights"):
            setattr(settings, key, getattr(candidate, key))
        settings.save()
        return {"ok": True}

    return app
