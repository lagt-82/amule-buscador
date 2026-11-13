"""Buscador web propio para aMule: FastAPI delante de la API REST de amuleapi."""
import os
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

from .amule_client import AmuleClient, AmuleError

AMULE_API_URL = os.getenv("AMULE_API_URL", "http://amule:4711")
AMULE_WEBUI_PWD = os.getenv("AMULE_WEBUI_PWD", "")
APP_USER = os.getenv("APP_USER", "admin")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
STATIC = Path(__file__).parent / "static"
HASH_RE = re.compile(r"^[0-9a-fA-F]{32}$")
MB = 1024 * 1024


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.amule = AmuleClient(AMULE_API_URL, AMULE_WEBUI_PWD)
    yield
    await app.state.amule.close()


app = FastAPI(title="Buscador aMule", lifespan=lifespan, docs_url=None, redoc_url=None)
basic = HTTPBasic(auto_error=False)


def auth(creds: HTTPBasicCredentials | None = Depends(basic)):
    """Protección opcional con usuario/contraseña (APP_PASSWORD)."""
    if not APP_PASSWORD:
        return
    ok = creds is not None and secrets.compare_digest(creds.username, APP_USER) \
        and secrets.compare_digest(creds.password, APP_PASSWORD)
    if not ok:
        raise HTTPException(401, "Acceso no autorizado", headers={"WWW-Authenticate": "Basic"})


@app.exception_handler(AmuleError)
async def amule_error(_: Request, e: AmuleError):
    return JSONResponse({"error": e.code, "message": e.message}, status_code=e.status)


def amule(request: Request) -> AmuleClient:
    return request.app.state.amule


def check_hash(h: str) -> str:
    if not HASH_RE.match(h):
        raise HTTPException(400, "Hash no válido")
    return h.lower()


# ---------- modelos ----------
class SearchIn(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    type: Literal["global", "kad", "all", "local"] = "global"
    file_type: Literal["", "audio", "video", "picture", "text", "program", "archive", "disc_image"] = ""
    extension: str = Field("", max_length=10)
    min_size_mb: float | None = Field(None, ge=0)
    max_size_mb: float | None = Field(None, ge=0)
    min_sources: int | None = Field(None, ge=0)


class LinksIn(BaseModel):
    links: list[str] = Field(min_length=1, max_length=200)


class ActionIn(BaseModel):
    action: Literal["pause", "resume", "stop"]


class DownloadIn(BaseModel):
    ecid: int | None = None


# ---------- web ----------
@app.get("/", dependencies=[Depends(auth)])
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/health")
async def health():
    return {"ok": True}


# ---------- API propia ----------
@app.get("/api/status", dependencies=[Depends(auth)])
async def status(request: Request):
    s = await amule(request).status()
    return {
        "ed2k": s.get("ed2k", {}).get("state"),
        "high_id": s.get("ed2k", {}).get("high_id"),
        "server": s.get("ed2k", {}).get("server_name"),
        "kad": s.get("kad", {}).get("state"),
        "kad_firewalled": s.get("kad", {}).get("firewalled_tcp"),
        "down": s.get("speeds", {}).get("download_speed_bytes_per_second", 0),
        "up": s.get("speeds", {}).get("upload_speed_bytes_per_second", 0),
        "free": s.get("disk", {}).get("incoming_free_bytes"),
        "search_all": s.get("search_all_supported", False),
    }


@app.post("/api/search", dependencies=[Depends(auth)])
async def search(body: SearchIn, request: Request):
    payload = {"query": body.query.strip(), "type": body.type}
    if body.file_type:
        payload["file_type"] = body.file_type
    if body.extension.strip():
        payload["extension"] = body.extension.strip().lstrip(".")
    if body.min_size_mb:
        payload["min_size_bytes"] = int(body.min_size_mb * MB)
    if body.max_size_mb:
        payload["max_size_bytes"] = int(body.max_size_mb * MB)
    if body.min_sources:
        payload["min_source_count"] = body.min_sources
    return await amule(request).start_search(payload)


@app.get("/api/searches", dependencies=[Depends(auth)])
async def searches(request: Request):
    data = await amule(request).list_searches()
    return [
        {k: s.get(k) for k in ("search_id", "query", "type", "state", "result_count")}
        for s in data.get("searches", []) if s.get("type") != "browse"
    ]


@app.get("/api/search/{search_id}", dependencies=[Depends(auth)])
async def results(search_id: int, request: Request):
    data = await amule(request).results(search_id)
    items = []
    for r in data.get("results", []):
        src = r.get("sources") or {}
        items.append({
            "hash": r["hash"],
            "name": r.get("name", ""),
            "size": r.get("size_bytes", 0),
            "sources": src.get("total", 0),
            "complete": src.get("complete", 0),
            "type": r.get("file_type", "unknown"),
            "status": r.get("status", "new"),
            "have": r.get("already_downloaded", False),
            "rating": r.get("rating", 0),
            "alternates": [
                {"ecid": a["ecid"], "name": a.get("name", ""), "sources": (a.get("sources") or {}).get("total", 0)}
                for a in r.get("alternate_names", [])
            ],
        })
    p = data.get("progress", {})
    return {
        "search_id": data.get("search_id", search_id),
        "query": data.get("query", ""),
        "state": p.get("state", "idle"),
        "percent": p.get("percent", 0),
        "kad_active": p.get("kad_active", False),
        "results": items,
    }


@app.post("/api/search/{search_id}/stop", dependencies=[Depends(auth)])
async def stop(search_id: int, request: Request):
    await amule(request).stop_search(search_id)
    return {"ok": True}


@app.post("/api/search/{search_id}/more", dependencies=[Depends(auth)])
async def more(search_id: int, request: Request):
    await amule(request).more(search_id)
    return {"ok": True}


@app.delete("/api/search/{search_id}", dependencies=[Depends(auth)])
async def delete_search(search_id: int, request: Request):
    await amule(request).delete_search(search_id)
    return {"ok": True}


@app.post("/api/results/{file_hash}/download", dependencies=[Depends(auth)])
async def download(file_hash: str, body: DownloadIn, request: Request):
    await amule(request).download_result(check_hash(file_hash), body.ecid)
    return {"ok": True}


@app.get("/api/downloads", dependencies=[Depends(auth)])
async def downloads(request: Request):
    data = await amule(request).downloads("all")
    out = []
    for d in data.get("downloads", []):
        src = d.get("sources") or {}
        out.append({
            "hash": d["hash"],
            "name": d.get("name", ""),
            "size": d.get("size_bytes", 0),
            "done": d.get("completed_bytes", 0),
            "percent": (d.get("progress") or {}).get("percent", 0),
            "speed": d.get("speed_bytes_per_second", 0),
            "status": d.get("status", "unknown"),
            "sources": src.get("total", 0),
            "transferring": src.get("transferring", 0),
            "link": d.get("ed2k_link", ""),
        })
    return out


@app.post("/api/downloads/links", dependencies=[Depends(auth)])
async def add_links(body: LinksIn, request: Request):
    links = [l.strip() for l in body.links if l.strip()]
    bad = [l for l in links if not l.startswith(("ed2k://", "magnet:"))]
    if bad:
        raise HTTPException(400, f"Enlace no válido: {bad[0][:80]}")
    return await amule(request).add_links(links)


@app.patch("/api/downloads/{file_hash}", dependencies=[Depends(auth)])
async def patch_download(file_hash: str, body: ActionIn, request: Request):
    await amule(request).patch_download(check_hash(file_hash), body.action)
    return {"ok": True}


@app.delete("/api/downloads/{file_hash}", dependencies=[Depends(auth)])
async def cancel_download(file_hash: str, request: Request):
    await amule(request).cancel_download(check_hash(file_hash))
    return {"ok": True}


@app.post("/api/downloads/clear-completed", dependencies=[Depends(auth)])
async def clear_completed(request: Request):
    await amule(request).clear_completed()
    return {"ok": True}
