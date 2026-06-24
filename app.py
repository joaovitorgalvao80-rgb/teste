"""NWRCH Studio - plataforma web de coleta e curadoria de B-rolls."""
import logging
import os
import secrets
import sys
from contextlib import asynccontextmanager

import database as db
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from app_shared import (
    APP_ENV,
    ERROR_RESPONSES,
    ROOT,
    _log_startup_config,
    _wants_json,
    current_user,
    render_template,
)
# Re-exports for test compatibility (tests access these via `import app as webapp`)
from app_shared import DATA_DIR, WORK_DIR  # noqa: F401
from app_shared import ENFORCE_CSRF, ALLOW_REGISTRATION, ALLOW_FIRST_USER, INVITE_CODE  # noqa: F401
from app_shared import DEFAULT_CONFIG, EDIT_PLAN_FILENAME  # noqa: F401
from app_shared import detect_api_keys, find_input_media, project_inputs_dir, project_work_dir  # noqa: F401
from app_shared import kaggle_service, local_output_videos, normalize_project_config  # noqa: F401
from app_shared import packager, run_package_job, run_search_job, run_vision_job  # noqa: F401
from app_shared import curation_report_path, safe_next_url, _take_sort_key  # noqa: F401
from app_shared import annotate_assets_with_vision, analyze_pending_vision, require_user  # noqa: F401
from services import asset_search  # noqa: F401

_logger = logging.getLogger(__name__)


def _require_secret() -> str:
    key = os.getenv("APP_SECRET_KEY", "").strip()
    unsafe = not key or len(key) < 32 or "change" in key.lower() or "troque" in key.lower()
    if APP_ENV == "production" and unsafe:
        _logger.warning(
            "APP_SECRET_KEY invalida ou ausente em producao. "
            "Sessoes serao instaveis entre restarts. "
            "Defina APP_SECRET_KEY com uma chave aleatoria de 32+ caracteres."
        )
    if not key:
        key = secrets.token_hex(32)
        print(
            "[AVISO] APP_SECRET_KEY nao definida; usando chave gerada para esta sessao.",
            file=sys.stderr,
        )
    return key


@asynccontextmanager
async def lifespan(_app):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    # DATA_DIR and WORK_DIR are read from THIS module's globals at call time,
    # so tests can override via webapp.DATA_DIR = ... before creating TestClient.
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    db.DATA_DIR = DATA_DIR
    db.DB_PATH = DATA_DIR / "plataforma.db"
    db.init_db()
    stale = db.fail_stale_jobs()
    if stale:
        _logger.warning("%s job(s) pendentes de processo anterior marcados como erro", stale)
    _log_startup_config()
    yield


app = FastAPI(title="NWRCH Studio", lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    secret_key=_require_secret(),
    session_cookie="nwrch_session",
    max_age=60 * 60 * 24 * 7,
    https_only=APP_ENV == "production",
    same_site="lax",
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    content_type = response.headers.get("content-type", "")
    if not request.url.path.startswith("/static") and "text/html" in content_type:
        response.headers["Cache-Control"] = "no-store"
    return response


_static_dir = ROOT / "static"
_static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.exception_handler(StarletteHTTPException)
async def html_error_handler(request: Request, exc: StarletteHTTPException):
    if _wants_json(request):
        detail = exc.detail if exc.status_code != 401 else "Sessao expirada. Faca login novamente."
        return JSONResponse({"detail": detail}, status_code=exc.status_code)
    if exc.status_code == 401:
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
    user = current_user(request)
    return render_template(
        request,
        "error.html",
        {"user": user, "status_code": exc.status_code, "detail": exc.detail},
        status_code=exc.status_code,
    )


@app.get("/health", responses=ERROR_RESPONSES)
def health():
    return {"status": "ok"}


from routes import auth, settings, projects, search, curation, package  # noqa: E402

app.include_router(auth.router)
app.include_router(settings.router)
app.include_router(projects.router)
app.include_router(search.router)
app.include_router(curation.router)
app.include_router(package.router)
