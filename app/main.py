"""WhaleMirror — FastAPI app.

Auth model: single shared APP_PASSWORD (env) → HttpOnly session cookie.
This app custodies a trading key; run it behind HTTPS (see README) and
use a long random password.
"""

import asyncio
import hmac
import os
import secrets
import time

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import consensus, mirror, store, tracker

APP_PASSWORD = os.environ.get("APP_PASSWORD")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

store.init()  # ensure tables exist even before the startup event runs

app = FastAPI(title="WhaleMirror", docs_url=None, redoc_url=None)
# Secure cookies by default (Railway/any HTTPS proxy). Set COOKIE_SECURE=false
# only for plain-HTTP local dev or SSH-tunnel access.
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").lower() != "false"
_state = {"refreshing": False, "progress": "", "last_error": None, "auto_results": []}


# ── Auth ──────────────────────────────────────────────────────────────────
def require_session(request: Request):
    token = request.cookies.get("wm_session", "")
    if not store.session_valid(token):
        raise HTTPException(status_code=401, detail="Not signed in")


class LoginBody(BaseModel):
    password: str


@app.post("/api/login")
def login(body: LoginBody, response: Response):
    if not APP_PASSWORD:
        raise HTTPException(500, "APP_PASSWORD is not set on the server")
    if not hmac.compare_digest(body.password, APP_PASSWORD):
        time.sleep(1)  # slow brute force
        raise HTTPException(401, "Wrong password")
    token = secrets.token_urlsafe(32)
    store.add_session(token)
    response.set_cookie("wm_session", token, httponly=True, samesite="strict",
                        secure=COOKIE_SECURE, max_age=store.SESSION_TTL)
    return {"ok": True}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    store.remove_session(request.cookies.get("wm_session", ""))
    response.delete_cookie("wm_session")
    return {"ok": True}


# ── Signals ───────────────────────────────────────────────────────────────
def _run_refresh():
    settings = store.get_settings()
    engine = consensus.ConsensusEngine({
        "min_whales": int(settings["min_whales"]),
        "dominance": float(settings["dominance"]),
    })

    def progress(i, n, name):
        _state["progress"] = f"Scanning whale {i}/{n} ({name})"

    signals = engine.run(progress=progress, followed=store.followed_whales())
    store.upsert_signals(signals)
    _state["auto_results"] = mirror.auto_mirror_pass(signals)
    return signals


@app.post("/api/refresh")
async def refresh(request: Request):
    require_session(request)
    if _state["refreshing"]:
        return {"ok": False, "detail": "Refresh already running"}
    _state.update(refreshing=True, last_error=None, progress="Fetching leaderboards")

    async def task():
        try:
            await asyncio.to_thread(_run_refresh)
        except Exception as e:  # noqa: BLE001
            _state["last_error"] = str(e)
        finally:
            _state.update(refreshing=False, progress="")

    asyncio.create_task(task())
    return {"ok": True}


@app.get("/api/signals")
def signals(request: Request):
    require_session(request)
    return {
        "signals": store.get_signals(),
        "mirrored_ids": sorted(store.mirrored_signal_ids()),
        "followed": store.followed_whales(),
        "last_refresh": store.last_refresh(),
        "refreshing": _state["refreshing"],
        "progress": _state["progress"],
        "last_error": _state["last_error"],
        "auto_results": _state["auto_results"],
    }


# ── Settings & credentials ────────────────────────────────────────────────
@app.get("/api/settings")
def get_settings(request: Request):
    require_session(request)
    return {"settings": store.get_settings(),
            "credentials": store.credentials_status(),
            "clob_available": mirror.CLOB_AVAILABLE,
            "spent_today": store.spent_today_usd()}


@app.post("/api/settings")
async def post_settings(request: Request):
    require_session(request)
    patch = await request.json()
    # Going live requires credentials on file
    if patch.get("dry_run") is False and not store.credentials_status()["configured"]:
        raise HTTPException(400, "Add trading credentials before turning off dry run")
    return {"settings": store.save_settings(patch)}


class CredsBody(BaseModel):
    private_key: str
    funder_address: str
    signature_type: int = 1


@app.post("/api/credentials")
def set_credentials(body: CredsBody, request: Request):
    require_session(request)
    if len(body.private_key.strip()) < 32 or not body.funder_address.startswith("0x"):
        raise HTTPException(400, "That doesn't look like a valid key / address pair")
    store.save_credentials(body.private_key, body.funder_address, body.signature_type)
    return store.credentials_status()


@app.delete("/api/credentials")
def delete_credentials(request: Request):
    require_session(request)
    store.clear_credentials()
    store.save_settings({"dry_run": True, "auto_mirror": False})
    return {"ok": True}


# ── Mirroring ─────────────────────────────────────────────────────────────
class MirrorBody(BaseModel):
    usd: float | None = None


@app.post("/api/mirror/{signal_id}")
async def mirror_signal(signal_id: str, body: MirrorBody, request: Request):
    require_session(request)
    signal = next((s for s in store.get_signals() if s["id"] == signal_id), None)
    if not signal:
        raise HTTPException(404, "Signal not found or stale — refresh first")
    result = await asyncio.to_thread(mirror.execute_mirror, signal, body.usd, True)
    return result


class FollowBody(BaseModel):
    address: str
    name: str


@app.post("/api/whales/follow")
def follow(body: FollowBody, request: Request):
    require_session(request)
    store.follow_whale(body.address, body.name)
    return {"followed": store.followed_whales()}


@app.delete("/api/whales/follow/{address}")
def unfollow(address: str, request: Request):
    require_session(request)
    store.unfollow_whale(address)
    return {"followed": store.followed_whales()}


@app.get("/api/activity")
def activity(request: Request):
    require_session(request)
    return {"mirrors": store.mirror_history()}


# ── Scheduler ─────────────────────────────────────────────────────────────
async def scheduler():
    await asyncio.sleep(5)
    last_track = 0.0
    last_housekeeping = 0.0
    while True:
        settings = store.get_settings()
        interval = max(10, int(settings["refresh_minutes"])) * 60
        last = store.last_refresh() or 0
        if not _state["refreshing"] and time.time() - last > interval:
            _state.update(refreshing=True, progress="Scheduled refresh")
            try:
                await asyncio.to_thread(_run_refresh)
                _state["last_error"] = None
            except Exception as e:  # noqa: BLE001
                _state["last_error"] = str(e)
            finally:
                _state.update(refreshing=False, progress="")
        if time.time() - last_housekeeping > 86400:
            last_housekeeping = time.time()
            try:
                await asyncio.to_thread(store.housekeeping)
            except Exception:  # noqa: BLE001
                pass
        if time.time() - last_track > 300:
            last_track = time.time()
            try:
                await asyncio.to_thread(tracker.refresh_positions)
            except Exception:  # noqa: BLE001 — tracking must never kill the loop
                pass
        await asyncio.sleep(60)


@app.on_event("startup")
async def startup():
    store.init()
    asyncio.create_task(scheduler())


@app.get("/api/performance")
def performance(request: Request):
    require_session(request)
    return {
        "summary": store.performance_summary(),
        "positions": store.all_positions(),
        "snapshots": {"dry_run": store.snapshots("dry_run"),
                      "live": store.snapshots("live")},
    }


@app.get("/healthz")
def healthz():
    return {"ok": True, "signals": len(store.get_signals()),
            "refreshing": _state["refreshing"], "db_mb": store.db_size_mb()}


# ── Static ────────────────────────────────────────────────────────────────
@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.exception_handler(HTTPException)
def http_exc(request, exc):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
