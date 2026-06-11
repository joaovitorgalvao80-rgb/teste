"""NWRCH Studio - plataforma web de coleta e curadoria de B-rolls."""
from __future__ import annotations

import json
import hmac
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import BackgroundTasks, FastAPI, File, Form, Request, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

import database as db
from services import asset_search, auto_select, diagnostics, edit_plan, groq_service, packager, kaggle_service
from services.script_parser import assign_parts, parse_script

ROOT = Path(__file__).resolve().parent

# ------------------------------------------------------------------
# Configuracao via variaveis de ambiente
# ------------------------------------------------------------------
def _load_env_file(path: Path = ROOT / ".env") -> None:
    """Carrega .env local simples sem sobrescrever variaveis ja exportadas."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip().strip('"').strip("'")


_load_env_file()
APP_ENV = os.getenv("APP_ENV", "dev").strip().lower()
ENFORCE_CSRF = os.getenv("ENFORCE_CSRF", "1" if APP_ENV == "production" else "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
ALLOW_REGISTRATION = os.getenv("ALLOW_REGISTRATION", "1" if APP_ENV != "production" else "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
ALLOW_FIRST_USER = os.getenv("ALLOW_FIRST_USER", "1").strip().lower() in {"1", "true", "yes", "on"}
INVITE_CODE = os.getenv("INVITE_CODE", "").strip()
STATIC_VERSION = (
    os.getenv("STATIC_VERSION")
    or os.getenv("RAILWAY_GIT_COMMIT_SHA", "")[:12]
    or "20260611-curation-fix"
)


def _require_secret() -> str:
    key = os.getenv("APP_SECRET_KEY", "").strip()
    unsafe = not key or len(key) < 32 or "change" in key.lower() or "troque" in key.lower()
    if APP_ENV == "production" and unsafe:
        raise RuntimeError(
            "APP_SECRET_KEY obrigatoria em producao. "
            "Use uma chave aleatoria com 32+ caracteres/bytes."
        )
    if not key:
        key = "dev-insecure-key-change-in-production-please"
        print(
            "[AVISO] APP_SECRET_KEY nao definida; usando chave fixa apenas para dev.",
            file=sys.stderr,
        )
    return key

DATA_DIR = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
WORK_DIR = DATA_DIR / "work"
BUSY_PROJECT_STATUSES = {"mapping", "searching", "packaging", "auto_selecting", "researching"}
# estados de take que contam como "escolhido" para pacote/diagnostico
CHOSEN_ASSET_STATES = ["selected", "accepted"]

DEFAULT_CONFIG = {
    "format": "16:9",
    "resolution": "1920x1080",
    "avatar_safe_area": "right",
    "avatar_safe_width_ratio": 0.30,
    "asset_type_priority": "video",
    "image_fallback": False,
    "visual_style": "realistic editorial YouTube B-roll, concrete scenes, rural Brazil when relevant",
    "script_language": "pt-BR",
    "keyword_language": "english",
    "scene_duration": 4.0,
    "per_keyword": 8,
    "max_download_mb": 90,
    "long_mode": False,
    "part_target_seconds": 150,
}

EDIT_PLAN_FILENAME = "edit_plan.json"

ALLOWED_RESOLUTIONS = {"1920x1080", "1280x720"}
ALLOWED_SAFE_AREAS = {"left", "right"}
MIN_SCENE_DURATION = 2.0
MAX_SCENE_DURATION = 8.0
MIN_AVATAR_SAFE_RATIO = 0.10
MAX_AVATAR_SAFE_RATIO = 0.45


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "sim"}
    return bool(value)


def _coerce_float(value: object, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return min(max(number, minimum), maximum)


def _coerce_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return min(max(number, minimum), maximum)


def normalize_project_config(raw_config: Optional[dict] = None) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if raw_config:
        cfg.update(raw_config)
    if cfg.get("resolution") not in ALLOWED_RESOLUTIONS:
        cfg["resolution"] = DEFAULT_CONFIG["resolution"]
    if cfg.get("avatar_safe_area") not in ALLOWED_SAFE_AREAS:
        cfg["avatar_safe_area"] = DEFAULT_CONFIG["avatar_safe_area"]
    cfg["scene_duration"] = _coerce_float(
        cfg.get("scene_duration"),
        DEFAULT_CONFIG["scene_duration"],
        MIN_SCENE_DURATION,
        MAX_SCENE_DURATION,
    )
    cfg["avatar_safe_width_ratio"] = _coerce_float(
        cfg.get("avatar_safe_width_ratio"),
        DEFAULT_CONFIG["avatar_safe_width_ratio"],
        MIN_AVATAR_SAFE_RATIO,
        MAX_AVATAR_SAFE_RATIO,
    )
    cfg["per_keyword"] = _coerce_int(cfg.get("per_keyword"), DEFAULT_CONFIG["per_keyword"], 1, 20)
    cfg["max_download_mb"] = _coerce_int(
        cfg.get("max_download_mb"), DEFAULT_CONFIG["max_download_mb"], 5, 500
    )
    cfg["image_fallback"] = _coerce_bool(cfg.get("image_fallback"))
    cfg["long_mode"] = _coerce_bool(cfg.get("long_mode"))
    cfg["part_target_seconds"] = _coerce_int(
        cfg.get("part_target_seconds"), DEFAULT_CONFIG["part_target_seconds"], 60, 300
    )
    visual_style = str(cfg.get("visual_style") or "").strip()
    cfg["visual_style"] = visual_style or DEFAULT_CONFIG["visual_style"]
    return cfg

# ------------------------------------------------------------------
# App
# ------------------------------------------------------------------
logger = logging.getLogger("nwrch.app")

_DEFAULT_DATA_DIR = str(ROOT / "data")


def _log_startup_config():
    using_default = str(DATA_DIR) == _DEFAULT_DATA_DIR
    logger.info("DATA_DIR: %s", DATA_DIR)
    if APP_ENV == "production" and using_default:
        logger.warning(
            "ATENCAO: DATA_DIR nao configurado — usando pasta local '%s'. "
            "No Railway, crie um Volume e defina DATA_DIR=/data nas variaveis de ambiente; "
            "sem isso os dados sao apagados a cada redeploy.",
            DATA_DIR,
        )
    if not os.getenv("APP_SECRET_KEY"):
        logger.warning("APP_SECRET_KEY nao definida — sessoes nao persistem entre restarts.")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    # garante pastas e banco antes de servir qualquer request
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    db.DATA_DIR = DATA_DIR
    db.DB_PATH = DATA_DIR / "plataforma.db"
    db.init_db()
    stale = db.fail_stale_jobs()
    if stale:
        logger.warning("%s job(s) pendentes de processo anterior marcados como erro", stale)
    _log_startup_config()
    yield


app = FastAPI(title="NWRCH Studio", lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    secret_key=_require_secret(),
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
    # HTML pages show live pipeline state, while static assets use versioned URLs.
    content_type = response.headers.get("content-type", "")
    if not request.url.path.startswith("/static") and "text/html" in content_type:
        response.headers["Cache-Control"] = "no-store"
    return response

# static/ e criada antes do mount para evitar crash na inicializacao
_static_dir = ROOT / "static"
_static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

templates = Jinja2Templates(directory=str(ROOT / "templates"))

# Labels de status em PT-BR para a UI (o valor cru segue nas classes CSS/data-attrs)
STATUS_LABELS = {
    "created": "Criado",
    "mapping": "Mapeando roteiro...",
    "mapped": "Mapa visual pronto",
    "map_failed": "Falha no mapa visual",
    "searching": "Buscando assets...",
    "searched": "Assets buscados",
    "search_failed": "Falha na busca",
    "auto_selecting": "Selecionando takes...",
    "reviewing": "Em revisão",
    "researching": "Buscando melhores...",
    "reviewed": "Revisão concluída",
    "packaging": "Gerando pacote...",
    "packaged": "Pacote pronto",
    "package_failed": "Falha no pacote",
    "needs_package": "Repacotar",
}


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status or "", status or "—")


templates.env.globals["status_label"] = status_label


def csrf_token_for(request: Request) -> str:
    token = request.session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["_csrf_token"] = token
    return token


def verify_csrf(request: Request, token: str = "") -> None:
    if not ENFORCE_CSRF:
        return
    expected = request.session.get("_csrf_token", "")
    supplied = token or request.headers.get("x-csrf-token", "")
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        raise HTTPException(403, "CSRF token invalido. Recarregue a pagina e tente novamente.")


def registration_state() -> dict:
    user_count = db.count_users()
    first_user_allowed = ALLOW_FIRST_USER and user_count == 0
    invite_required = bool(INVITE_CODE) and not first_user_allowed
    enabled = first_user_allowed or ALLOW_REGISTRATION or bool(INVITE_CODE)
    return {
        "enabled": enabled,
        "invite_required": invite_required,
        "first_user_allowed": first_user_allowed,
    }


def render_template(
    request: Request,
    template_name: str,
    context: Optional[dict] = None,
    status_code: int = 200,
):
    payload = dict(context or {})
    payload.setdefault("csrf_token", csrf_token_for(request))
    payload.setdefault("registration", registration_state())
    payload.setdefault("static_version", STATIC_VERSION)
    return templates.TemplateResponse(request, template_name, payload, status_code=status_code)


def mask_secret(value: str) -> str:
    value = value or ""
    if not value:
        return ""
    if len(value) <= 8:
        return "configurada"
    return f"{value[:4]}...{value[-4:]}"


def secret_from_form(current: str, submitted: str, clear: str = "") -> str:
    if _coerce_bool(clear):
        return ""
    submitted = (submitted or "").strip()
    return submitted if submitted else (current or "")


async def read_upload_limited(upload: UploadFile, max_bytes: int, what: str = "Arquivo") -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(400, f"{what} muito grande — máximo {max_bytes // (1024 * 1024)} MB.")
        chunks.append(chunk)
    return b"".join(chunks)

# ------------------------------------------------------------------
# Error handlers (mostra pagina HTML em vez de JSON cru)
# ------------------------------------------------------------------
def _wants_json(request: Request) -> bool:
    """Detecta chamadas fetch/XHR para responder JSON em vez de pagina HTML."""
    fetch_mode = request.headers.get("sec-fetch-mode", "")
    if fetch_mode and fetch_mode != "navigate":
        return True
    return "application/json" in request.headers.get("accept", "")


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

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
def current_user(request: Request) -> Optional[dict]:
    uid = request.session.get("user_id")
    return db.get_user(uid) if uid else None


def require_user(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    return user


def project_config(project: dict) -> dict:
    try:
        stored = json.loads(project.get("config_json") or "{}")
    except json.JSONDecodeError:
        stored = {}
    return normalize_project_config(stored)


def resolution_width(config: dict) -> int:
    return int(str(config.get("resolution") or DEFAULT_CONFIG["resolution"]).split("x", 1)[0])


def missing_selected_scene_ids(scenes: list[dict], selected_by_scene: dict[int, dict]) -> list[str]:
    return [s["scene_id"] for s in scenes if s["id"] not in selected_by_scene]


def project_work_dir(project_id: int) -> Path:
    return WORK_DIR / f"project_{project_id}"


def _safe_child_dir(root: Path, child: Path) -> Optional[Path]:
    root_resolved = root.resolve()
    child_resolved = child.resolve()
    try:
        child_resolved.relative_to(root_resolved)
    except ValueError:
        return None
    return child_resolved


def remove_project_artifacts(project_id: int, include_generated: bool = False) -> None:
    project_work = _safe_child_dir(WORK_DIR, project_work_dir(project_id))
    if not project_work or not project_work.exists():
        return
    for zip_file in project_work.glob("*.zip"):
        zip_file.unlink(missing_ok=True)
    (project_work / EDIT_PLAN_FILENAME).unlink(missing_ok=True)
    for folder_name in ["assets_tmp", "kaggle_output"]:
        folder = _safe_child_dir(project_work, project_work / folder_name)
        if folder and folder.exists():
            shutil.rmtree(folder, ignore_errors=True)
    if include_generated:
        generated = _safe_child_dir(project_work, project_work / GENERATED_DIR_NAME)
        if generated and generated.exists():
            shutil.rmtree(generated, ignore_errors=True)


def remove_project_workspace(project_id: int) -> None:
    project_work = _safe_child_dir(WORK_DIR, project_work_dir(project_id))
    if project_work and project_work.exists():
        shutil.rmtree(project_work, ignore_errors=True)


def mark_project_dirty(project_id: int, include_generated: bool = False) -> None:
    db.mark_project_needs_package(project_id)
    remove_project_artifacts(project_id, include_generated=include_generated)


def ensure_project_not_busy(project: dict) -> None:
    if project.get("status") in BUSY_PROJECT_STATUSES:
        raise HTTPException(409, "Aguarde o job atual terminar antes de alterar este projeto.")
    if project.get("kaggle_status") == "uploading":
        raise HTTPException(409, "Upload ao Kaggle em andamento; aguarde terminar antes de alterar o projeto.")


def ensure_no_active_job(project_id: int, kind: str) -> None:
    """Evita jobs duplicados quando dois POSTs chegam quase juntos (duplo clique)."""
    if db.has_active_job(project_id, kind):
        raise HTTPException(409, "Esse passo ja esta em execucao. Aguarde o job atual terminar.")


def expected_duration_from_scenes(scenes: list[dict]) -> float:
    return max((float(s.get("end_time") or 0) for s in scenes), default=0.0)


def project_diagnostics_snapshot(
    project_id: int,
    scenes: list[dict],
    selected_count: int,
) -> dict:
    project_work = project_work_dir(project_id)
    return diagnostics.build_snapshot(
        project_work=project_work,
        zip_path=latest_zip(project_work),
        selected_count=selected_count,
        scene_count=len(scenes),
        expected_duration=expected_duration_from_scenes(scenes),
    )


def safe_next_url(raw_next: str) -> str:
    """Aceita apenas redirects internos, evitando open redirect no login."""
    if not raw_next:
        return "/projects"
    # navegadores tratam '\' como '/': "/\evil.com" viraria "//evil.com"
    if "\\" in raw_next or any(ord(ch) < 0x20 for ch in raw_next):
        return "/projects"
    parsed = urlparse(raw_next)
    if parsed.scheme or parsed.netloc or not raw_next.startswith("/") or raw_next.startswith("//"):
        return "/projects"
    return raw_next


def latest_zip(project_work: Path) -> Optional[Path]:
    return max(project_work.glob("*.zip"), key=lambda p: p.stat().st_mtime, default=None)


def latest_kaggle_video(project_work: Path) -> Optional[Path]:
    output_dir = project_work / "kaggle_output"
    return kaggle_service.choose_preferred_video_path(output_dir.rglob("*.mp4")) if output_dir.exists() else None


NARRATION_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
AVATAR_EXTS = {".webm", ".mov", ".mp4"}
MEDIA_KINDS = {"narration": NARRATION_EXTS, "avatar": AVATAR_EXTS}
MAX_MEDIA_UPLOAD_MB = 200
MAX_TRANSCRIBE_UPLOAD_MB = 500  # videos sao convertidos em MP3 antes de salvar/transcrever


def project_inputs_dir(project_id: int) -> Path:
    return WORK_DIR / f"project_{project_id}" / "inputs"


def find_input_media(project_id: int, kind: str) -> Optional[Path]:
    folder = project_inputs_dir(project_id)
    exts = MEDIA_KINDS.get(kind, set())
    if not folder.exists():
        return None
    for f in sorted(folder.iterdir()):
        if f.is_file() and f.stem == kind and f.suffix.lower() in exts:
            return f
    return None


def save_input_media_bytes(project_id: int, kind: str, data: bytes, suffix: str) -> Path:
    exts = MEDIA_KINDS.get(kind)
    suffix = (suffix or "").lower()
    if not exts or suffix not in exts:
        raise HTTPException(400, f"Extensao nao suportada para {kind}: use {', '.join(sorted(exts or []))}.")
    if len(data) > MAX_MEDIA_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(400, f"Arquivo muito grande (maximo {MAX_MEDIA_UPLOAD_MB} MB).")
    if not data:
        raise HTTPException(400, "Arquivo vazio.")
    folder = project_inputs_dir(project_id)
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob(f"{kind}.*"):
        old.unlink(missing_ok=True)
    dest = folder / f"{kind}{suffix}"
    dest.write_bytes(data)
    return dest


def prepare_narration_media(raw: bytes, filename: str) -> tuple[bytes, str]:
    """Normaliza upload inicial de narracao.

    A tela de novo projeto aceita audio ou video para transcricao; para o
    render final guardamos audio. Quando vier video, extraimos MP3.
    Video usa o mesmo teto da transcricao (500 MB), ja que so o MP3 e salvo.
    """
    suffix = Path(filename or "").suffix.lower()
    if suffix in NARRATION_EXTS:
        if len(raw) > MAX_MEDIA_UPLOAD_MB * 1024 * 1024:
            raise HTTPException(400, f"Arquivo muito grande (maximo {MAX_MEDIA_UPLOAD_MB} MB).")
        return raw, suffix
    if suffix in _VIDEO_EXTS:
        if len(raw) > MAX_TRANSCRIBE_UPLOAD_MB * 1024 * 1024:
            raise HTTPException(400, f"Video muito grande (maximo {MAX_TRANSCRIBE_UPLOAD_MB} MB).")
        audio_bytes, out_name = _extract_audio_bytes(raw, filename or "narration.mp4")
        return audio_bytes, Path(out_name).suffix.lower() or ".mp3"
    if raw:
        raise HTTPException(400, "Extensao nao suportada para narracao: use audio ou video comum.")
    return b"", ""


def local_output_videos(project_work: Path) -> dict:
    """Separa base e master entre os MP4 baixados do Kaggle."""
    outputs: dict = {"base": None, "master": None}
    output_dir = project_work / "kaggle_output"
    if not output_dir.exists():
        return outputs
    for p in output_dir.rglob("*.mp4"):
        rel = str(p.relative_to(output_dir)).replace("\\", "/")
        if rel.startswith("assets/") or "/assets/" in rel:
            continue
        name = p.name.lower()
        if name == kaggle_service.MASTER_VIDEO_NAME:
            outputs["master"] = p
        elif name in {kaggle_service.BASE_VIDEO_NAME, kaggle_service.BASE_VIDEO_ALIAS}:
            outputs["base"] = p
    return outputs


def local_edit_plan(project_id: int) -> Optional[dict]:
    plan_path = project_work_dir(project_id) / EDIT_PLAN_FILENAME
    if not plan_path.exists():
        return None
    try:
        return json.loads(plan_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def local_hyperframes_status(project_work: Path) -> Optional[dict]:
    status_file = project_work / "kaggle_output" / "hyperframes_status.json"
    if not status_file.exists():
        return None
    try:
        return json.loads(status_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def run_kaggle_send_job(
    job_id: int,
    project_id: int,
    project_name: str,
    username: str,
    token: str,
    zip_path_str: str,
) -> None:
    try:
        db.update_job(job_id, status="running", message="Enviando dataset para o Kaggle")
        # copia o ZIP antes do upload: mark_project_dirty pode apagar o original
        # se o usuario alterar o projeto enquanto o envio roda em background
        with tempfile.TemporaryDirectory(prefix="nwrch_kaggle_") as tmp:
            zip_path = Path(tmp) / Path(zip_path_str).name
            shutil.copy2(zip_path_str, zip_path)
            ds_slug = kaggle_service.upload_dataset(zip_path, project_name, username, token, project_id=project_id)
        db.update_job(
            job_id,
            status="running",
            message="Criando kernel de render no Kaggle",
            result={"dataset_slug": ds_slug},
        )
        k_slug, push_out = kaggle_service.push_kernel(ds_slug, project_name, username, token, project_id=project_id)
        db.update_kaggle_job(project_id, ds_slug, k_slug, "queued")
        kernel_url = f"https://www.kaggle.com/code/{username}/{k_slug}"
        db.finish_job(
            job_id,
            message="Render enviado ao Kaggle",
            result={
                "dataset_slug": ds_slug,
                "kernel_slug": k_slug,
                "kernel_url": kernel_url,
                "push_out": push_out,
            },
        )
    except Exception as exc:  # noqa: BLE001 - registra falha operacional para a UI
        db.update_kaggle_status(project_id, "error")
        db.fail_job(job_id, "Falha ao enviar para o Kaggle", str(exc))


# ------------------------------------------------------------------
# Health check (Railway / load balancer)
# ------------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok"}


# ------------------------------------------------------------------
# Auth
# ------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    if current_user(request):
        return RedirectResponse("/projects", status_code=303)
    return RedirectResponse("/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str = "", next: str = ""):
    return render_template(request, "login.html", {"error": error, "next": next})


@app.post("/login")
def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form(""),
):
    user = db.get_user_by_name(username.strip())
    if not user or not db.verify_password(password, user["password_hash"]):
        return RedirectResponse("/login?error=Credenciais+invalidas", status_code=303)
    request.session.clear()  # evita session fixation
    request.session["user_id"] = user["id"]
    return RedirectResponse(safe_next_url(next), status_code=303)


@app.post("/register")
def register(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    invite_code: str = Form(""),
):
    state = registration_state()
    if not state["enabled"]:
        return RedirectResponse("/login?error=Cadastro+desativado", status_code=303)
    if state["invite_required"] and not hmac.compare_digest(invite_code.strip(), INVITE_CODE):
        return RedirectResponse("/login?error=Convite+invalido", status_code=303)
    username = username.strip()
    if not username or len(password) < 8:
        return RedirectResponse("/login?error=Usuario+ou+senha+invalidos", status_code=303)
    if db.get_user_by_name(username):
        return RedirectResponse("/login?error=Usuario+ja+existe", status_code=303)
    uid = db.create_user(username, password)
    request.session.clear()
    request.session["user_id"] = uid
    return RedirectResponse("/settings", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ------------------------------------------------------------------
# Settings (APIs)
# ------------------------------------------------------------------
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: str = ""):
    user = require_user(request)
    secret_masks = {
        "pexels": mask_secret(user.get("pexels_key", "")),
        "pixabay": mask_secret(user.get("pixabay_key", "")),
        "groq": mask_secret(user.get("groq_key", "")),
        "openrouter": mask_secret(user.get("openrouter_key", "")),
        "kaggle_token": mask_secret(user.get("kaggle_token", "")),
    }
    return render_template(
        request,
        "settings.html",
        {
            "user": user,
            "saved": saved,
            "groq_models": groq_service.GROQ_MODELS,
            "secret_masks": secret_masks,
        },
    )


@app.get("/settings/test-kaggle")
def test_kaggle(request: Request):
    user = require_user(request)
    username = user.get("kaggle_username", "")
    token = user.get("kaggle_token", "")
    if not username or not token:
        return JSONResponse({"ok": False, "detail": "Username ou token não configurados."})
    import requests as req
    from requests.auth import HTTPBasicAuth
    try:
        r = req.get(
            "https://www.kaggle.com/api/v1/competitions/list",
            auth=HTTPBasicAuth(username, token),
            params={"page": 1, "pageSize": 1},
            timeout=15,
        )
        if r.status_code == 200:
            return JSONResponse({"ok": True, "detail": "Credenciais válidas ✓"})
        return JSONResponse({"ok": False, "detail": f"HTTP {r.status_code}: {r.text[:400]}"})
    except Exception as exc:
        return JSONResponse({"ok": False, "detail": str(exc)})


@app.post("/settings")
def settings_save(
    request: Request,
    pexels: str = Form(""),
    pixabay: str = Form(""),
    groq: str = Form(""),
    groq_model: str = Form(""),
    openrouter: str = Form(""),
    kaggle_username: str = Form(""),
    kaggle_token: str = Form(""),
    clear_pexels: str = Form(""),
    clear_pixabay: str = Form(""),
    clear_groq: str = Form(""),
    clear_openrouter: str = Form(""),
    clear_kaggle_token: str = Form(""),
    csrf_token: str = Form(""),
):
    user = require_user(request)
    verify_csrf(request, csrf_token)
    db.update_api_keys(
        user["id"],
        secret_from_form(user.get("pexels_key", ""), pexels, clear_pexels),
        secret_from_form(user.get("pixabay_key", ""), pixabay, clear_pixabay),
        secret_from_form(user.get("groq_key", ""), groq, clear_groq),
        groq_model.strip(),
        openrouter=secret_from_form(user.get("openrouter_key", ""), openrouter, clear_openrouter),
    )
    db.update_kaggle_keys(
        user["id"],
        kaggle_username.strip(),
        secret_from_form(user.get("kaggle_token", ""), kaggle_token, clear_kaggle_token),
    )
    return RedirectResponse("/settings?saved=1", status_code=303)


# ------------------------------------------------------------------
# Importação de chaves por arquivo .txt (detecção automática)
# ------------------------------------------------------------------
MAX_KEYS_FILE_BYTES = 64 * 1024

# Formatos conhecidos de cada provedor; usados quando a linha não tem rótulo.
_KEY_GUESS_PATTERNS = [
    ("groq", re.compile(r"^gsk_[A-Za-z0-9_-]{20,}$")),
    ("openrouter", re.compile(r"^sk-or-[A-Za-z0-9_-]{20,}$")),
    ("kaggle_token", re.compile(r"^KGAT[A-Za-z0-9_-]{10,}$", re.IGNORECASE)),
    ("pixabay", re.compile(r"^\d{6,10}-[0-9a-f]{20,40}$", re.IGNORECASE)),
    ("kaggle_token", re.compile(r"^[0-9a-f]{32}$")),
    ("pexels", re.compile(r"^[A-Za-z0-9]{45,60}$")),
]

KEY_FIELD_LABELS = {
    "pexels": "Pexels",
    "pixabay": "Pixabay",
    "groq": "Groq",
    "openrouter": "OpenRouter",
    "kaggle_username": "Kaggle username",
    "kaggle_token": "Kaggle token",
}


def _key_field_from_label(label: str) -> Optional[str]:
    low = label.lower()
    if "pexels" in low:
        return "pexels"
    if "pixabay" in low:
        return "pixabay"
    if "groq" in low:
        return "groq"
    if "openrouter" in low or "open router" in low or "open_router" in low:
        return "openrouter"
    if "kaggle" in low:
        return "kaggle_username" if "user" in low else "kaggle_token"
    if low.strip() in {"username", "user"}:
        return "kaggle_username"
    return None


def detect_api_keys(text: str) -> dict[str, str]:
    """Lê um .txt (ou kaggle.json) e descobre qual chave pertence a qual API.

    Aceita linhas rotuladas ("pexels: CHAVE", "groq = CHAVE"), o kaggle.json
    oficial e chaves soltas reconhecidas pelo formato (gsk_, sk-or-, etc).
    """
    detected: dict[str, str] = {}

    # kaggle.json oficial: {"username": "...", "key": "..."}
    m_user = re.search(r'"username"\s*:\s*"([^"\s]+)"', text)
    m_key = re.search(r'"key"\s*:\s*"([^"\s]+)"', text)
    if m_user and m_key:
        detected["kaggle_username"] = m_user.group(1)
        detected["kaggle_token"] = m_key.group(1)

    for raw_line in text.splitlines():
        line = raw_line.strip().strip(",;")
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        match = re.match(r"^[-*\s]*([A-Za-z _\-]{2,40}?)\s*[:=]\s*(\S+)\s*$", line)
        if match:
            field = _key_field_from_label(match.group(1))
            value = match.group(2).strip().strip('"').strip("'")
            min_len = 3 if field == "kaggle_username" else 8
            if field and len(value) >= min_len:
                detected.setdefault(field, value)
                continue
        for token in re.split(r"[\s,;]+", line):
            token = token.strip().strip('"').strip("'")
            if not token:
                continue
            for field, pattern in _KEY_GUESS_PATTERNS:
                if field not in detected and pattern.match(token):
                    detected[field] = token
                    break
    return detected


@app.post("/settings/import-keys")
async def import_keys(
    request: Request,
    keys_file: UploadFile = File(...),
    csrf_token: str = Form(""),
):
    user = require_user(request)
    verify_csrf(request, csrf_token)
    raw = await keys_file.read()
    if len(raw) > MAX_KEYS_FILE_BYTES:
        return JSONResponse({"error": "Arquivo grande demais (máx. 64 KB)."}, status_code=400)
    text = raw.decode("utf-8", errors="replace")
    detected = detect_api_keys(text)
    if not detected:
        return JSONResponse(
            {"error": "Nenhuma chave reconhecida. Use linhas como 'pexels: SUA_CHAVE' ou envie o kaggle.json."},
            status_code=400,
        )
    db.update_api_keys(
        user["id"],
        detected.get("pexels", user.get("pexels_key", "")),
        detected.get("pixabay", user.get("pixabay_key", "")),
        detected.get("groq", user.get("groq_key", "")),
        user.get("groq_model", ""),
        openrouter=detected.get("openrouter", user.get("openrouter_key", "")),
    )
    if detected.get("kaggle_username") or detected.get("kaggle_token"):
        db.update_kaggle_keys(
            user["id"],
            detected.get("kaggle_username", user.get("kaggle_username", "")),
            detected.get("kaggle_token", user.get("kaggle_token", "")),
        )
    saved = [KEY_FIELD_LABELS.get(field, field) for field in sorted(detected)]
    return JSONResponse({"saved": saved, "detail": f"{len(saved)} chave(s) detectadas e salvas: " + ", ".join(saved)})


# ------------------------------------------------------------------
# Transcrição de áudio (Groq Whisper → timestamps)
# ------------------------------------------------------------------
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".flv", ".wmv"}
_GROQ_MAX_BYTES = 24 * 1024 * 1024  # 25 MB hard limit da API; 24 MB de margem


def _extract_audio_bytes(raw: bytes, filename: str) -> tuple[bytes, str]:
    """Se for vídeo ou arquivo grande, extrai/comprime para MP3 mono 64k via FFmpeg."""
    ext = Path(filename).suffix.lower()
    is_video = ext in _VIDEO_EXTS
    if not is_video and len(raw) <= _GROQ_MAX_BYTES:
        return raw, filename
    if not shutil.which("ffmpeg"):
        raise HTTPException(
            500,
            "FFmpeg não encontrado no servidor; necessário para extrair áudio de vídeo. "
            "Instale o FFmpeg ou envie um arquivo de áudio (mp3/wav) direto.",
        )
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / ("input" + (ext or ".mp4"))
        src.write_bytes(raw)
        out = Path(tmp) / "audio.mp3"
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-vn", "-ar", "16000", "-ac", "1",
             "-ab", "64k", str(out)],
            capture_output=True, timeout=300,
        )
        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg falhou: {result.stderr.decode()[:400]}")
        extracted = out.read_bytes()
    if len(extracted) > _GROQ_MAX_BYTES:
        raise HTTPException(
            400,
            f"Áudio muito longo para transcrever de uma vez "
            f"({len(extracted)//1024//1024} MB após compressão; limite Groq: 24 MB). "
            "Divida em partes menores."
        )
    return extracted, "audio.mp3"


@app.post("/transcribe-audio")
async def transcribe_audio(
    request: Request,
    audio: UploadFile = File(...),
    csrf_token: str = Form(""),
):
    user = require_user(request)
    verify_csrf(request, csrf_token)
    if not user.get("groq_key"):
        raise HTTPException(400, "Configure a chave Groq em /settings para usar transcrição.")
    raw = await read_upload_limited(audio, MAX_TRANSCRIBE_UPLOAD_MB * 1024 * 1024)
    try:
        data, fname = _extract_audio_bytes(raw, audio.filename or "audio.mp4")
        transcript = groq_service.transcribe_audio(data, fname, user["groq_key"])
        return JSONResponse({"transcript": transcript})
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Transcrição falhou: {exc}") from exc


# ------------------------------------------------------------------
# Projects
# ------------------------------------------------------------------
@app.get("/projects", response_class=HTMLResponse)
def projects_page(request: Request):
    user = require_user(request)
    projects = db.list_projects(user["id"])
    return render_template(request, "projects.html", {"user": user, "projects": projects})


@app.get("/projects/new", response_class=HTMLResponse)
def new_project_page(request: Request):
    user = require_user(request)
    return render_template(request, "new_project.html", {"user": user, "config": DEFAULT_CONFIG})


@app.post("/projects/new")
async def new_project(
    request: Request,
    name: str = Form(...),
    script: str = Form(...),
    avatar_safe_area: str = Form("right"),
    visual_style: str = Form(DEFAULT_CONFIG["visual_style"]),
    resolution: str = Form("1920x1080"),
    scene_duration: float = Form(4.0),
    image_fallback: str = Form(""),
    long_mode: str = Form(""),
    narration_media: Optional[UploadFile] = File(None),
    csrf_token: str = Form(""),
):
    user = require_user(request)
    verify_csrf(request, csrf_token)
    prepared_narration: Optional[tuple[bytes, str]] = None
    if narration_media and narration_media.filename:
        raw = await read_upload_limited(narration_media, MAX_TRANSCRIBE_UPLOAD_MB * 1024 * 1024, "Narração")
        if raw:
            prepared_narration = prepare_narration_media(raw, narration_media.filename)
    is_long = _coerce_bool(long_mode)
    if is_long and scene_duration == DEFAULT_CONFIG["scene_duration"]:
        # video longo: cenas maiores reduzem o numero de buscas/renderizacoes
        scene_duration = 7.0
    config = normalize_project_config({
        "avatar_safe_area": avatar_safe_area,
        "visual_style": visual_style.strip() or DEFAULT_CONFIG["visual_style"],
        "resolution": resolution,
        "scene_duration": scene_duration,
        "image_fallback": image_fallback,
        "long_mode": is_long,
    })
    pid = db.create_project(user["id"], name.strip() or "projeto", script, config)
    if prepared_narration:
        save_input_media_bytes(pid, "narration", prepared_narration[0], prepared_narration[1])
    return RedirectResponse(f"/projects/{pid}", status_code=303)


@app.post("/projects/{project_id}/delete")
def delete_project(request: Request, project_id: int, csrf_token: str = Form("")):
    user = require_user(request)
    verify_csrf(request, csrf_token)
    project = db.get_project(project_id, user["id"])
    if project:
        ensure_project_not_busy(project)
        db.delete_project(project_id, user["id"])
        remove_project_workspace(project_id)
    return RedirectResponse("/projects", status_code=303)


@app.get("/projects/{project_id}", response_class=HTMLResponse)
def project_page(request: Request, project_id: int):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    config = project_config(project)
    scenes = db.list_scenes(project_id)
    assets_by_scene = db.list_assets_for_project(project_id)
    for s in scenes:
        s["assets"] = assets_by_scene.get(s["id"], [])
        s["selected"] = next((a for a in s["assets"] if a["state"] in CHOSEN_ASSET_STATES), None)
    asset_count = sum(len(s["assets"]) for s in scenes)
    selected_count = sum(1 for s in scenes if s.get("selected"))
    accepted_count = sum(1 for s in scenes if s.get("selected") and s["selected"]["state"] == "accepted")
    project_work = project_work_dir(project_id)
    narration_file = find_input_media(project_id, "narration")
    avatar_file = find_input_media(project_id, "avatar")
    outputs = local_output_videos(project_work)
    jobs = db.list_project_jobs(project_id, user["id"])
    active_jobs = [job for job in jobs if job.get("status") in {"queued", "running"}]
    return render_template(
        request,
        "project.html",
        {
            "user": user,
            "project": project,
            "config": config,
            "scenes": scenes,
            "asset_count": asset_count,
            "selected_count": selected_count,
            "accepted_count": accepted_count,
            "has_keys": bool(user["pexels_key"] or user["pixabay_key"]),
            "narration_name": narration_file.name if narration_file else "",
            "avatar_name": avatar_file.name if avatar_file else "",
            "has_base_video": outputs["base"] is not None,
            "has_master_video": outputs["master"] is not None,
            "edit_plan": local_edit_plan(project_id),
            "hyperframes_status": local_hyperframes_status(project_work) or {},
            "diagnostics": project_diagnostics_snapshot(project_id, scenes, selected_count),
            "jobs": jobs,
            "active_jobs": active_jobs,
            "parts": db.list_parts(project_id) if config.get("long_mode") else [],
            "parts_job_active": any(
                j["kind"] in {"kaggle_parts", "concat_parts"} and j["status"] in {"queued", "running"}
                for j in jobs
            ),
        },
    )


# ------------------------------------------------------------------
# Gerar mapa visual
# ------------------------------------------------------------------
def run_generate_map_job(
    job_id: int,
    project_id: int,
    user_id: int,
    groq_key: str,
    groq_model: str,
) -> None:
    try:
        db.update_job(job_id, status="running", message="Gerando mapa visual")
        project = db.get_project(project_id, user_id)
        if not project:
            raise RuntimeError("Projeto nao encontrado.")
        config = project_config(project)
        base_scenes = parse_script(project["script"], config["scene_duration"])
        if not base_scenes:
            raise RuntimeError("Roteiro vazio ou invalido.")
        briefs = groq_service.generate_briefs(
            base_scenes,
            groq_key=groq_key,
            style=config["visual_style"],
            avatar_safe_area=config["avatar_safe_area"],
            safe_ratio=config["avatar_safe_width_ratio"],
            model=groq_model or groq_service.DEFAULT_MODEL,
        )
        brief_by_id = {b["scene_id"]: b for b in briefs}
        merged = []
        for s in base_scenes:
            b = brief_by_id.get(s["scene_id"], {})
            merged.append({
                **s,
                "visual_goal": b.get("visual_goal", ""),
                "keywords": b.get("keywords", []),
                "must_show": b.get("must_show", []),
                "must_not_show": b.get("must_not_show", []),
                "asset_type": b.get("asset_type", "video"),
                "overlay_text": b.get("overlay_text", ""),
                "avatar_safe_area": b.get("avatar_safe_area", config["avatar_safe_area"]),
            })
        total_parts = 1
        if config.get("long_mode"):
            total_parts = assign_parts(merged, config.get("part_target_seconds") or 150)
        db.replace_scenes(project_id, merged)
        if config.get("long_mode"):
            summaries = []
            for part_idx in range(1, total_parts + 1):
                part_scenes = [s for s in merged if s.get("part") == part_idx]
                duration = max((s["end_time"] for s in part_scenes), default=0.0) - min(
                    (s["start_time"] for s in part_scenes), default=0.0
                )
                summaries.append({
                    "part_idx": part_idx,
                    "scene_count": len(part_scenes),
                    "duration": round(duration, 3),
                })
            db.replace_parts(project_id, summaries)
        else:
            db.replace_parts(project_id, [])
        remove_project_artifacts(project_id, include_generated=True)
        db.set_project_status(project_id, "mapped")
        db.clear_kaggle_job(project_id)
        db.finish_job(job_id, "Mapa visual pronto", {"scenes": len(merged), "parts": total_parts})
    except Exception as exc:  # noqa: BLE001
        db.set_project_status(project_id, "map_failed")
        db.fail_job(job_id, "Falha ao gerar mapa visual", str(exc))


@app.post("/projects/{project_id}/generate-map")
def generate_map(
    request: Request,
    project_id: int,
    background_tasks: BackgroundTasks,
    csrf_token: str = Form(""),
):
    user = require_user(request)
    verify_csrf(request, csrf_token)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    ensure_project_not_busy(project)
    if not (project.get("script") or "").strip():
        raise HTTPException(400, "Roteiro vazio ou invalido.")
    ensure_no_active_job(project_id, "generate_map")
    job_id = db.create_job(user["id"], "generate_map", project_id, "Mapa visual na fila")
    db.set_project_status(project_id, "mapping")
    background_tasks.add_task(
        run_generate_map_job,
        job_id,
        project_id,
        user["id"],
        user.get("groq_key", ""),
        user.get("groq_model") or groq_service.DEFAULT_MODEL,
    )
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


# ------------------------------------------------------------------
# Buscar assets + selecao automatica opcional
# ------------------------------------------------------------------
def auto_select_for_project(
    project_id: int,
    config: dict,
    groq_key: str,
    groq_model: str,
    job_id: Optional[int] = None,
    review_round: int = 0,
) -> int:
    """Escolhe o melhor take pendente para cada cena sem take aceito.

    Cenas com asset 'accepted' (aprovado na revisao) nunca sao tocadas.
    Retorna o numero de cenas com take selecionado automaticamente.
    """
    scenes = db.list_scenes(project_id)
    assets_by_scene = db.list_assets_for_project(project_id)
    target_scenes = []
    candidates_by_scene: dict[int, list[dict]] = {}
    for scene in scenes:
        assets = assets_by_scene.get(scene["id"], [])
        if any(a["state"] == "accepted" for a in assets):
            continue
        pending = [a for a in assets if a["state"] in {"pending", "selected", "favorite"}]
        if not pending:
            continue
        target_scenes.append(scene)
        candidates_by_scene[scene["id"]] = pending

    if not target_scenes:
        return 0

    def progress(done: int, total: int) -> None:
        if job_id:
            db.update_job(job_id, status="running", message=f"Selecionando takes ({done}/{total} cenas)")

    choices = auto_select.choose_best_takes(
        target_scenes,
        candidates_by_scene,
        config,
        groq_key=groq_key,
        model=groq_model or groq_service.DEFAULT_MODEL,
        progress=progress,
    )
    for scene_db_id, (asset_id, score, reason) in choices.items():
        db.set_asset_state(
            asset_id,
            "selected",
            auto_score=score,
            auto_reason=reason,
            review_round=review_round,
        )
    return len(choices)


def run_search_job(
    job_id: int,
    project_id: int,
    user_id: int,
    pexels_key: str,
    pixabay_key: str,
    groq_key: str = "",
    groq_model: str = "",
) -> None:
    try:
        db.update_job(job_id, status="running", message="Buscando assets")
        project = db.get_project(project_id, user_id)
        if not project:
            raise RuntimeError("Projeto nao encontrado.")
        config = project_config(project)
        max_w = resolution_width(config)
        scenes = db.list_scenes(project_id)
        if not scenes:
            raise RuntimeError("Gere o mapa visual antes da busca.")
        seen: set = set()
        total_added = 0
        empty_scenes: list[str] = []
        for scene in scenes:
            db.update_job(job_id, status="running", message=f"Buscando {scene['scene_id']}")
            results = asset_search.search_scene(
                scene["keywords"],
                pexels_key,
                pixabay_key,
                max_w=max_w,
                per_keyword=config["per_keyword"],
                allow_images=bool(config["image_fallback"]),
                seen_urls=seen,
            )
            added = db.add_assets(scene["id"], results)
            total_added += added
            if added == 0:
                empty_scenes.append(scene["scene_id"])
        if total_added <= 0:
            raise RuntimeError("Busca retornou zero assets. Verifique chaves, keywords ou disponibilidade das APIs.")

        db.set_project_status(project_id, "searched")
        db.finish_job(
            job_id,
            "Busca concluida",
            {
                "added": total_added,
                "empty_scenes": empty_scenes,
                "scenes": len(scenes),
                "auto_selected": 0,
            },
        )
    except Exception as exc:  # noqa: BLE001
        db.set_project_status(project_id, "search_failed")
        db.fail_job(job_id, "Falha na busca de assets", str(exc))


@app.post("/projects/{project_id}/search")
def search_all(
    request: Request,
    project_id: int,
    background_tasks: BackgroundTasks,
    csrf_token: str = Form(""),
):
    user = require_user(request)
    verify_csrf(request, csrf_token)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    ensure_project_not_busy(project)
    if not user["pexels_key"] and not user["pixabay_key"]:
        raise HTTPException(400, "Cadastre ao menos uma chave de API em /settings.")
    scenes = db.list_scenes(project_id)
    if not scenes:
        raise HTTPException(400, "Gere o mapa visual antes de buscar assets.")
    ensure_no_active_job(project_id, "search_assets")
    job_id = db.create_job(user["id"], "search_assets", project_id, "Busca de assets na fila")
    db.set_project_status(project_id, "searching")
    background_tasks.add_task(
        run_search_job,
        job_id,
        project_id,
        user["id"],
        user.get("pexels_key", ""),
        user.get("pixabay_key", ""),
        user.get("groq_key", ""),
        user.get("groq_model") or groq_service.DEFAULT_MODEL,
    )
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/scenes/{scene_db_id}/search-more")
def search_more(
    request: Request,
    scene_db_id: int,
    media: str = Form("all"),
    csrf_token: str = Form(""),
):
    user = require_user(request)
    verify_csrf(request, csrf_token)
    scene = db.get_scene(scene_db_id)
    if not scene:
        raise HTTPException(404)
    project = db.get_project(scene["project_id"], user["id"])
    if not project:
        raise HTTPException(404)
    ensure_project_not_busy(project)
    if not user["pexels_key"] and not user["pixabay_key"]:
        raise HTTPException(400, "Cadastre ao menos uma chave de API em /settings.")
    config = project_config(project)
    if media not in {"all", "video", "image"}:
        media = "all"
    max_w = resolution_width(config)
    existing = {a["download_url"] for a in db.list_assets(scene_db_id)}
    results = asset_search.search_scene(
        scene["keywords"],
        user["pexels_key"],
        user["pixabay_key"],
        max_w=max_w,
        per_keyword=config["per_keyword"] + 4,
        allow_images=True,
        seen_urls=existing,
        media=media,
    )
    added = db.add_assets(scene_db_id, results)
    if added:
        mark_project_dirty(project["id"])
    return JSONResponse({"added": added, "media": media})


@app.post("/scenes/{scene_db_id}/regen-keywords")
def regen_keywords(request: Request, scene_db_id: int, csrf_token: str = Form("")):
    user = require_user(request)
    verify_csrf(request, csrf_token)
    scene = db.get_scene(scene_db_id)
    if not scene:
        raise HTTPException(404)
    project = db.get_project(scene["project_id"], user["id"])
    if not project:
        raise HTTPException(404)
    ensure_project_not_busy(project)
    config = project_config(project)
    kws = groq_service.regenerate_keywords(
        scene.get("narration", ""),
        scene.get("visual_goal", ""),
        user["groq_key"],
        config["visual_style"],
        model=user.get("groq_model") or groq_service.DEFAULT_MODEL,
    )
    db.update_scene_keywords(scene_db_id, kws)
    mark_project_dirty(project["id"])
    return JSONResponse({"keywords": kws})


# ------------------------------------------------------------------
# Curadoria
# ------------------------------------------------------------------
@app.post("/assets/{asset_id}/state")
def asset_state(
    request: Request,
    asset_id: int,
    state: str = Form(...),
    csrf_token: str = Form(""),
    redirect: str = Form(""),
):
    user = require_user(request)
    verify_csrf(request, csrf_token)
    if state not in {"pending", "selected", "rejected", "favorite", "accepted"}:
        raise HTTPException(400, "Estado invalido.")
    owner = db.get_asset_project(asset_id)
    if not owner or owner["user_id"] != user["id"]:
        raise HTTPException(404)
    project = db.get_project(owner["project_id"], user["id"])
    if not project:
        raise HTTPException(404)
    ensure_project_not_busy(project)
    updated = db.set_asset_state(asset_id, state)
    if not updated:
        raise HTTPException(404)
    status = project.get("status")
    project_status = status
    if status == "reviewed":
        # Changing a take after finishing makes the review report stale.
        db.set_project_status(owner["project_id"], "reviewing")
        curation_report_path(owner["project_id"]).unlink(missing_ok=True)
        project_status = "reviewing"
    elif status != "reviewing":
        # Outside review, invalidate any existing package as before.
        mark_project_dirty(owner["project_id"])
        curation_report_path(owner["project_id"]).unlink(missing_ok=True)
        fresh_project = db.get_project(owner["project_id"], user["id"])
        project_status = (fresh_project or project).get("status", status)
    # Native form fallback sends redirect; fetch-based JS does not.
    if redirect and redirect.startswith("/") and not redirect.startswith("//"):
        return RedirectResponse(redirect, status_code=303)
    return JSONResponse({"id": asset_id, "state": updated["state"], "project_status": project_status})


# ------------------------------------------------------------------
# Revisao: auto-selecao, tela de revisao, re-busca e relatorio
# ------------------------------------------------------------------
CURATION_REPORT_NAME = "curation_report.md"


def curation_report_path(project_id: int) -> Path:
    return project_work_dir(project_id) / CURATION_REPORT_NAME


def run_auto_select_job(
    job_id: int,
    project_id: int,
    user_id: int,
    groq_key: str,
    groq_model: str,
) -> None:
    try:
        db.update_job(job_id, status="running", message="Selecionando os melhores takes")
        project = db.get_project(project_id, user_id)
        if not project:
            raise RuntimeError("Projeto nao encontrado.")
        config = project_config(project)
        chosen = auto_select_for_project(
            project_id, config, groq_key, groq_model, job_id=job_id,
            review_round=int(project.get("review_round") or 0),
        )
        if chosen <= 0:
            raise RuntimeError("Nenhuma cena com candidatos pendentes para selecionar.")
        db.set_project_status(project_id, "reviewing")
        db.finish_job(job_id, f"{chosen} takes selecionados", {"auto_selected": chosen})
    except Exception as exc:  # noqa: BLE001
        db.set_project_status(project_id, "searched")
        db.fail_job(job_id, "Falha na selecao automatica", str(exc))


@app.post("/projects/{project_id}/auto-select")
def auto_select_route(
    request: Request,
    project_id: int,
    background_tasks: BackgroundTasks,
    csrf_token: str = Form(""),
):
    user = require_user(request)
    verify_csrf(request, csrf_token)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    ensure_project_not_busy(project)
    scenes = db.list_scenes(project_id)
    if not scenes:
        raise HTTPException(400, "Gere o mapa visual e busque assets antes da selecao automatica.")
    assets_by_scene = db.list_assets_for_project(project_id)
    if not any(assets_by_scene.get(scene["id"]) for scene in scenes):
        raise HTTPException(400, "Busque assets antes de usar a selecao automatica.")
    ensure_no_active_job(project_id, "auto_select")
    job_id = db.create_job(user["id"], "auto_select", project_id, "Selecao automatica na fila")
    db.set_project_status(project_id, "auto_selecting")
    background_tasks.add_task(
        run_auto_select_job,
        job_id,
        project_id,
        user["id"],
        user.get("groq_key", ""),
        user.get("groq_model") or groq_service.DEFAULT_MODEL,
    )
    return RedirectResponse(f"/projects/{project_id}/review", status_code=303)


@app.get("/projects/{project_id}/review", response_class=HTMLResponse)
def review_page(request: Request, project_id: int):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    config = project_config(project)
    scenes = db.list_scenes(project_id)
    assets_by_scene = db.list_assets_for_project(project_id)
    review_scenes = []
    accepted = rejected_waiting = pending_review = 0
    for scene in scenes:
        assets = assets_by_scene.get(scene["id"], [])
        chosen = next((a for a in assets if a["state"] in CHOSEN_ASSET_STATES), None)
        scene["chosen"] = chosen
        scene["rejected_count"] = sum(1 for a in assets if a["state"] == "rejected")
        if chosen and chosen["state"] == "accepted":
            accepted += 1
        elif chosen:
            pending_review += 1
        else:
            rejected_waiting += 1
        review_scenes.append(scene)
    return render_template(
        request,
        "review.html",
        {
            "user": user,
            "project": project,
            "config": config,
            "scenes": review_scenes,
            "accepted": accepted,
            "pending_review": pending_review,
            "rejected_waiting": rejected_waiting,
            "total": len(review_scenes),
            "review_round": int(project.get("review_round") or 0),
            "has_report": project.get("status") in {"reviewed", "packaging", "packaged", "package_failed"}
            and curation_report_path(project_id).exists(),
        },
    )


def run_research_job(
    job_id: int,
    project_id: int,
    user_id: int,
    pexels_key: str,
    pixabay_key: str,
    groq_key: str,
    groq_model: str,
) -> None:
    try:
        db.update_job(job_id, status="running", message="Buscando takes melhores para as cenas rejeitadas")
        project = db.get_project(project_id, user_id)
        if not project:
            raise RuntimeError("Projeto nao encontrado.")
        config = project_config(project)
        max_w = resolution_width(config)
        new_round = int(project.get("review_round") or 0) + 1
        db.set_project_review_round(project_id, new_round)

        scenes = db.list_scenes(project_id)
        assets_by_scene = db.list_assets_for_project(project_id)
        targets = [
            s for s in scenes
            if not any(a["state"] in CHOSEN_ASSET_STATES for a in assets_by_scene.get(s["id"], []))
        ]
        if not targets:
            raise RuntimeError("Nenhuma cena rejeitada aguardando nova busca.")

        added_total = 0
        for i, scene in enumerate(targets, 1):
            db.update_job(
                job_id, status="running",
                message=f"Nova busca {i}/{len(targets)}: {scene['scene_id']}",
            )
            # keywords novas para fugir dos resultados que o usuario rejeitou
            kws = groq_service.regenerate_keywords(
                scene.get("narration", ""),
                scene.get("visual_goal", ""),
                groq_key,
                config["visual_style"],
                model=groq_model or groq_service.DEFAULT_MODEL,
            )
            if kws:
                db.update_scene_keywords(scene["id"], kws)
                scene["keywords"] = kws
            existing = {a["download_url"] for a in assets_by_scene.get(scene["id"], [])}
            results = asset_search.search_scene(
                scene["keywords"],
                pexels_key,
                pixabay_key,
                max_w=max_w,
                per_keyword=config["per_keyword"] + 4,
                allow_images=True,
                seen_urls=existing,
            )
            added_total += db.add_assets(scene["id"], results)

        db.update_job(job_id, status="running", message="Selecionando os melhores takes novos")
        chosen = auto_select_for_project(
            project_id, config, groq_key, groq_model, job_id=job_id, review_round=new_round,
        )
        db.set_project_status(project_id, "reviewing")
        db.finish_job(
            job_id,
            f"Rodada {new_round}: {added_total} takes novos, {chosen} selecionados",
            {"round": new_round, "added": added_total, "selected": chosen, "scenes": len(targets)},
        )
    except Exception as exc:  # noqa: BLE001
        db.set_project_status(project_id, "reviewing")
        db.fail_job(job_id, "Falha na nova busca", str(exc))


@app.post("/projects/{project_id}/research-rejected")
def research_rejected(
    request: Request,
    project_id: int,
    background_tasks: BackgroundTasks,
    csrf_token: str = Form(""),
):
    user = require_user(request)
    verify_csrf(request, csrf_token)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    ensure_project_not_busy(project)
    if not user["pexels_key"] and not user["pixabay_key"]:
        raise HTTPException(400, "Cadastre ao menos uma chave de API em /settings.")
    ensure_no_active_job(project_id, "research_rejected")
    job_id = db.create_job(user["id"], "research_rejected", project_id, "Nova busca na fila")
    db.set_project_status(project_id, "researching")
    background_tasks.add_task(
        run_research_job,
        job_id,
        project_id,
        user["id"],
        user.get("pexels_key", ""),
        user.get("pixabay_key", ""),
        user.get("groq_key", ""),
        user.get("groq_model") or groq_service.DEFAULT_MODEL,
    )
    return RedirectResponse(f"/projects/{project_id}/review", status_code=303)


@app.post("/projects/{project_id}/finish-review")
def finish_review(request: Request, project_id: int, csrf_token: str = Form("")):
    user = require_user(request)
    verify_csrf(request, csrf_token)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    ensure_project_not_busy(project)
    if project.get("status") == "reviewed" and curation_report_path(project_id).exists():
        return RedirectResponse(f"/projects/{project_id}", status_code=303)
    scenes = db.list_scenes(project_id)
    if not scenes:
        raise HTTPException(400, "Projeto sem cenas.")
    assets_by_scene = db.list_assets_for_project(project_id)
    chosen_by_scene: dict[int, dict] = {}
    not_accepted: list[str] = []
    for scene in scenes:
        assets = assets_by_scene.get(scene["id"], [])
        accepted = next((a for a in assets if a["state"] == "accepted"), None)
        if accepted:
            chosen_by_scene[scene["id"]] = accepted
        else:
            not_accepted.append(scene["scene_id"])
    if not_accepted:
        preview = ", ".join(not_accepted[:8])
        suffix = "..." if len(not_accepted) > 8 else ""
        raise HTTPException(400, f"Aceite um take para todas as cenas antes de concluir. Faltando: {preview}{suffix}")

    rejected_by_scene = {
        scene["id"]: [a for a in assets_by_scene.get(scene["id"], []) if a["state"] == "rejected"]
        for scene in scenes
    }
    report = packager.build_curation_report(
        project,
        scenes,
        chosen_by_scene,
        rejected_by_scene,
        review_round=int(project.get("review_round") or 0),
    )
    path = curation_report_path(project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report, encoding="utf-8")
    db.set_project_status(project_id, "reviewed")
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.get("/projects/{project_id}/curation-report")
def download_curation_report(request: Request, project_id: int):
    user = require_user(request)
    if not db.get_project(project_id, user["id"]):
        raise HTTPException(404)
    path = curation_report_path(project_id)
    if not path.exists():
        raise HTTPException(404, "Relatorio de curadoria ainda nao gerado. Conclua a revisao primeiro.")
    return FileResponse(path, filename=path.name, media_type="text/markdown")


# ------------------------------------------------------------------
# Imagens geradas por IA (Puter.js no browser -> salvas como asset)
# ------------------------------------------------------------------
GENERATED_DIR_NAME = "generated"
MAX_GENERATED_UPLOAD_MB = 15
_GENERATED_NAME_RE = re.compile(r"^gen_[0-9a-f]{32}\.(png|jpg|webp)$")
_GENERATED_MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}


def project_generated_dir(project_id: int) -> Path:
    return project_work_dir(project_id) / GENERATED_DIR_NAME


def _jpeg_size(data: bytes) -> tuple[int, int]:
    """Extrai (width, height) dos marcadores SOF de um JPEG; (0,0) se falhar."""
    i = 2
    try:
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                return (
                    int.from_bytes(data[i + 7:i + 9], "big"),
                    int.from_bytes(data[i + 5:i + 7], "big"),
                )
            seg_len = int.from_bytes(data[i + 2:i + 4], "big")
            i += 2 + max(seg_len, 2)
    except Exception:  # noqa: BLE001 - dimensao e best-effort, nunca bloqueia o upload
        pass
    return 0, 0


def _image_kind_and_size(data: bytes) -> tuple[str, int, int]:
    """Detecta o formato por magic bytes e le as dimensoes quando possivel.

    Retorna (extensao, width, height); width/height = 0 quando nao deu para ler.
    Levanta ValueError para formatos nao suportados (nao confiamos no mimetype
    enviado pelo browser).
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        w = h = 0
        if len(data) >= 24:
            w = int.from_bytes(data[16:20], "big")
            h = int.from_bytes(data[20:24], "big")
        return ".png", w, h
    if data.startswith(b"\xff\xd8\xff"):
        w, h = _jpeg_size(data)
        return ".jpg", w, h
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp", 0, 0
    raise ValueError("Arquivo nao e PNG, JPEG ou WebP.")


@app.post("/scenes/{scene_db_id}/generated-image")
async def save_generated_image(
    request: Request,
    scene_db_id: int,
    image: UploadFile = File(...),
    prompt: str = Form(""),
    width: str = Form("0"),
    height: str = Form("0"),
    csrf_token: str = Form(""),
):
    user = require_user(request)
    verify_csrf(request, csrf_token)
    scene = db.get_scene(scene_db_id)
    if not scene:
        raise HTTPException(404)
    project = db.get_project(scene["project_id"], user["id"])
    if not project:
        raise HTTPException(404)
    ensure_project_not_busy(project)
    data = await read_upload_limited(image, MAX_GENERATED_UPLOAD_MB * 1024 * 1024, "Imagem")
    if not data:
        raise HTTPException(400, "Imagem vazia.")
    try:
        ext, parsed_w, parsed_h = _image_kind_and_size(data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # dimensoes do header tem prioridade; as do browser sao fallback
    w = parsed_w or _coerce_int(width, 0, 0, 16384)
    h = parsed_h or _coerce_int(height, 0, 0, 16384)
    prompt = (prompt or "").strip()[:500]

    folder = project_generated_dir(project["id"])
    folder.mkdir(parents=True, exist_ok=True)
    filename = f"gen_{uuid.uuid4().hex}{ext}"
    dest = folder / filename
    try:
        dest.write_bytes(data)
        url = f"/projects/{project['id']}/generated/{filename}"
        added = db.add_assets(scene_db_id, [{
            "source": "generated",
            "source_id": filename,
            "asset_type": "image",
            "preview_url": url,
            "download_url": url,
            "page_url": "",
            "width": w,
            "height": h,
            "duration": 0,
            "keyword": prompt[:60] or "imagem gerada",
            "author": "",
            "author_url": "",
        }])
    except Exception:
        # nao deixa arquivo orfao se o INSERT falhar
        dest.unlink(missing_ok=True)
        raise
    if added != 1:
        dest.unlink(missing_ok=True)
        raise HTTPException(500, "Falha ao registrar a imagem gerada.")
    mark_project_dirty(project["id"])
    return JSONResponse({"added": added, "url": url})


@app.get("/projects/{project_id}/generated/{filename}")
def serve_generated_image(request: Request, project_id: int, filename: str):
    user = require_user(request)
    if not db.get_project(project_id, user["id"]):
        raise HTTPException(404)
    # regex estrito impede path traversal e acesso a outros arquivos do work dir
    if not _GENERATED_NAME_RE.fullmatch(filename):
        raise HTTPException(404)
    path = project_generated_dir(project_id) / filename
    if not path.is_file():
        raise HTTPException(404, "Imagem gerada nao encontrada.")
    return FileResponse(
        path,
        media_type=_GENERATED_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream"),
    )


# ------------------------------------------------------------------
# Midias do refinamento (narracao / avatar)
# ------------------------------------------------------------------
@app.post("/projects/{project_id}/upload-media")
async def upload_media(
    request: Request,
    project_id: int,
    kind: str = Form(...),
    media: UploadFile = File(...),
    csrf_token: str = Form(""),
):
    user = require_user(request)
    verify_csrf(request, csrf_token)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    ensure_project_not_busy(project)
    exts = MEDIA_KINDS.get(kind)
    if not exts:
        raise HTTPException(400, "Tipo de midia invalido.")
    suffix = Path(media.filename or "").suffix.lower()
    if suffix not in exts:
        raise HTTPException(400, f"Extensao nao suportada para {kind}: use {', '.join(sorted(exts))}.")
    data = await read_upload_limited(media, MAX_MEDIA_UPLOAD_MB * 1024 * 1024)
    save_input_media_bytes(project_id, kind, data, suffix)
    mark_project_dirty(project_id)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/projects/{project_id}/remove-media")
def remove_media(
    request: Request,
    project_id: int,
    kind: str = Form(...),
    csrf_token: str = Form(""),
):
    user = require_user(request)
    verify_csrf(request, csrf_token)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    ensure_project_not_busy(project)
    if kind not in MEDIA_KINDS:
        raise HTTPException(400, "Tipo de midia invalido.")
    existing = find_input_media(project_id, kind)
    if existing:
        existing.unlink(missing_ok=True)
        mark_project_dirty(project_id)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


# ------------------------------------------------------------------
# Gerar pacote (ZIP)
# ------------------------------------------------------------------
def parts_dir(project_id: int) -> Path:
    return project_work_dir(project_id) / "parts"


def part_dir(project_id: int, part_idx: int) -> Path:
    return parts_dir(project_id) / f"part_{part_idx:02d}"


def _rebase_scenes(scenes: list[dict]) -> list[dict]:
    """Clona as cenas da parte com a timeline movida para t=0 (contrato do montador)."""
    if not scenes:
        return []
    offset = min(float(s.get("start_time") or 0) for s in scenes)
    rebased = []
    for s in scenes:
        clone = dict(s)
        clone["start_time"] = round(float(s["start_time"]) - offset, 3)
        clone["end_time"] = round(float(s["end_time"]) - offset, 3)
        rebased.append(clone)
    return rebased


def run_package_job(
    job_id: int,
    project_id: int,
    user_id: int,
    openrouter_key: str,
) -> None:
    try:
        db.update_job(job_id, status="running", message="Gerando edit_plan e baixando assets")
        project = db.get_project(project_id, user_id)
        if not project:
            raise RuntimeError("Projeto nao encontrado.")
        config = project_config(project)
        scenes = db.list_scenes(project_id)
        selected_rows = db.list_assets_by_state(project_id, CHOSEN_ASSET_STATES)
        selected_by_scene = {row["scene_id"]: row for row in selected_rows}
        rejected = db.list_assets_by_state(project_id, ["rejected"])
        missing = missing_selected_scene_ids(scenes, selected_by_scene)
        if not selected_by_scene or missing:
            raise RuntimeError("Selecao incompleta; escolha um asset para cada cena.")
        project_work = project_work_dir(project_id)
        remove_project_artifacts(project_id)
        rejected_payload = [
            {"scene_id": r["scene_code"], "source": r["source"], "url": r["download_url"], "keyword": r["keyword"]}
            for r in rejected
        ]

        if config.get("long_mode"):
            # um ZIP por parte; narracao/avatar entram so na concatenacao local
            parts = db.list_parts(project_id)
            if not parts:
                raise RuntimeError("Projeto longo sem partes; gere o mapa visual novamente.")
            if parts_dir(project_id).exists():
                shutil.rmtree(parts_dir(project_id), ignore_errors=True)
            zip_names = []
            for part in parts:
                idx = part["part_idx"]
                db.update_job(
                    job_id, status="running",
                    message=f"Baixando assets da parte {idx}/{len(parts)}",
                )
                part_scenes = [s for s in scenes if int(s.get("part") or 1) == idx]
                if not part_scenes:
                    db.update_part(project_id, idx, status="error", error="parte sem cenas")
                    continue
                zip_path = packager.build_zip(
                    project=project,
                    config=config,
                    scenes=_rebase_scenes(part_scenes),
                    selected_by_scene=selected_by_scene,
                    rejected_assets=rejected_payload,
                    work_dir=part_dir(project_id, idx),
                    max_download_mb=config["max_download_mb"],
                    zip_basename=f"{project['name']}_pt{idx:02d}",
                )
                db.update_part(
                    project_id, idx,
                    zip_name=zip_path.name, status="zipped",
                    error="", video_path="", dataset_slug="", kernel_slug="",
                )
                zip_names.append(zip_path.name)
            if not zip_names:
                raise RuntimeError("Nenhuma parte gerou pacote.")
            db.set_project_status(project_id, "packaged")
            db.clear_kaggle_job(project_id)
            db.finish_job(
                job_id,
                f"{len(zip_names)} pacotes prontos (1 por parte)",
                {"parts": len(zip_names), "zips": zip_names, "scenes": len(scenes)},
            )
            return

        narration_file = find_input_media(project_id, "narration")
        avatar_file = find_input_media(project_id, "avatar")
        plan = edit_plan.build_edit_plan_with_llm(
            project,
            config,
            scenes,
            openrouter_key=openrouter_key,
            narration_file=narration_file.name if narration_file else "",
            avatar_file=avatar_file.name if avatar_file else "",
        )
        # copia local do plano: permite revisar a edicao antes de enviar ao Kaggle
        project_work.mkdir(parents=True, exist_ok=True)
        (project_work / EDIT_PLAN_FILENAME).write_text(
            json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        zip_path = packager.build_zip(
            project=project,
            config=config,
            scenes=scenes,
            selected_by_scene=selected_by_scene,
            rejected_assets=rejected_payload,
            work_dir=project_work,
            max_download_mb=config["max_download_mb"],
            edit_plan=plan,
            extra_files=[
                f for f in (narration_file, avatar_file, curation_report_path(project_id)) if f and f.exists()
            ],
        )
        db.set_project_status(project_id, "packaged")
        db.clear_kaggle_job(project_id)
        db.finish_job(
            job_id,
            "Pacote ZIP pronto",
            {"zip": zip_path.name, "scenes": len(scenes), "selected": len(selected_by_scene)},
        )
    except Exception as exc:  # noqa: BLE001
        db.set_project_status(project_id, "package_failed")
        db.fail_job(job_id, "Falha ao gerar pacote", str(exc))


@app.post("/projects/{project_id}/package")
def package(
    request: Request,
    project_id: int,
    background_tasks: BackgroundTasks,
    csrf_token: str = Form(""),
):
    user = require_user(request)
    verify_csrf(request, csrf_token)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    ensure_project_not_busy(project)
    scenes = db.list_scenes(project_id)
    selected_rows = db.list_assets_by_state(project_id, CHOSEN_ASSET_STATES)
    selected_by_scene = {row["scene_id"]: row for row in selected_rows}

    if not selected_by_scene:
        raise HTTPException(400, "Selecione ao menos um asset antes de gerar o pacote.")
    missing = missing_selected_scene_ids(scenes, selected_by_scene)
    if missing:
        preview = ", ".join(missing[:8])
        suffix = "..." if len(missing) > 8 else ""
        raise HTTPException(
            400,
            f"Selecione um asset para todas as cenas antes de gerar o pacote. Faltando: {preview}{suffix}",
        )
    ensure_no_active_job(project_id, "package")
    job_id = db.create_job(user["id"], "package", project_id, "Preparando pacote ZIP")
    db.set_project_status(project_id, "packaging")
    background_tasks.add_task(
        run_package_job,
        job_id,
        project_id,
        user["id"],
        user.get("openrouter_key", ""),
    )
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.get("/projects/{project_id}/download-zip")
def download_zip(request: Request, project_id: int):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    project_work = project_work_dir(project_id)
    zip_path = latest_zip(project_work)
    if project.get("status") != "packaged" or not zip_path:
        raise HTTPException(404, "ZIP não encontrado. Gere o pacote primeiro.")
    return FileResponse(zip_path, filename=zip_path.name, media_type="application/zip")


@app.get("/projects/{project_id}/edit-plan")
def get_edit_plan(request: Request, project_id: int):
    """Plano de edição gerado no pacote — para revisão antes do envio ao Kaggle."""
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    plan = local_edit_plan(project_id)
    if not plan:
        raise HTTPException(404, "Plano de edição não encontrado. Gere o pacote (etapa 03) primeiro.")
    return JSONResponse(plan)


# ------------------------------------------------------------------
# Kaggle - enviar para render
# ------------------------------------------------------------------
@app.post("/projects/{project_id}/send-to-kaggle")
def send_to_kaggle(
    request: Request,
    project_id: int,
    background_tasks: BackgroundTasks,
    csrf_token: str = Form(""),
):
    user = require_user(request)
    verify_csrf(request, csrf_token)
    project = db.get_project(project_id, user["id"])
    if not project:
        return JSONResponse({"error": "Projeto não encontrado."}, status_code=404)
    if not user.get("kaggle_username") or not user.get("kaggle_token"):
        return JSONResponse({"error": "Configure Kaggle username e token em /settings."}, status_code=400)

    if project.get("status") != "packaged":
        return JSONResponse(
            {"error": "Gere um pacote valido em '03 Preparar pacote' antes de enviar ao Kaggle."},
            status_code=400,
        )

    project_work = project_work_dir(project_id)
    zip_path = latest_zip(project_work)
    if not zip_path:
        return JSONResponse({"error": "ZIP não encontrado. Clique em '03 Preparar pacote' novamente."}, status_code=400)

    job_id = db.create_job(user["id"], "kaggle_send", project_id, "Envio ao Kaggle na fila")
    db.update_kaggle_status(project_id, "uploading")
    background_tasks.add_task(
        run_kaggle_send_job,
        job_id,
        project_id,
        project["name"],
        user["kaggle_username"],
        user["kaggle_token"],
        str(zip_path),
    )
    return JSONResponse(
        {
            "status": "uploading",
            "job_id": job_id,
            "message": "Envio ao Kaggle iniciado em segundo plano.",
        }
    )


# ------------------------------------------------------------------
# Video longo: render por partes + concatenacao final
# ------------------------------------------------------------------
PART_POLL_SECONDS = 30
PART_RENDER_TIMEOUT = 45 * 60  # por parte


def run_kaggle_parts_job(
    job_id: int,
    project_id: int,
    user_id: int,
    username: str,
    token: str,
) -> None:
    try:
        project = db.get_project(project_id, user_id)
        if not project:
            raise RuntimeError("Projeto nao encontrado.")
        parts = [p for p in db.list_parts(project_id) if p["status"] != "done"]
        total = len(db.list_parts(project_id))
        if not parts:
            raise RuntimeError("Todas as partes ja foram renderizadas.")
        ok = 0
        failed = 0
        for part in parts:
            idx = part["part_idx"]
            label = f"parte {idx}/{total}"
            try:
                zip_path = part_dir(project_id, idx) / (part.get("zip_name") or "")
                if not part.get("zip_name") or not zip_path.exists():
                    raise RuntimeError("ZIP da parte nao encontrado; gere os pacotes novamente.")
                db.update_job(job_id, status="running", message=f"Enviando {label} ao Kaggle")
                db.update_part(project_id, idx, status="uploading", error="")
                part_name = f"{project['name']} pt{idx:02d}"
                ds_slug = kaggle_service.upload_dataset(zip_path, part_name, username, token, project_id=project_id)
                k_slug, _push = kaggle_service.push_kernel(ds_slug, part_name, username, token, project_id=project_id)
                db.update_part(project_id, idx, dataset_slug=ds_slug, kernel_slug=k_slug, status="running")

                db.update_job(job_id, status="running", message=f"Renderizando {label} no Kaggle")
                deadline = time.time() + PART_RENDER_TIMEOUT
                final_status = "timeout"
                error_detail = ""
                while time.time() < deadline:
                    time.sleep(PART_POLL_SECONDS)
                    info = kaggle_service.get_status(k_slug, username, token)
                    status = (info.get("status") or "").lower()
                    if status == "complete":
                        final_status = "complete"
                        break
                    if status == "error":
                        final_status = "error"
                        error_detail = str(info.get("error") or "")[:400]
                        break
                if final_status == "complete":
                    out_dir = part_dir(project_id, idx) / "kaggle_output"
                    video = kaggle_service.pull_output_video(k_slug, username, token, out_dir)
                    if not video:
                        raise RuntimeError("Render concluiu mas o MP4 nao foi encontrado no output.")
                    db.update_part(project_id, idx, status="done", video_path=str(video), error="")
                    ok += 1
                elif final_status == "error":
                    raise RuntimeError(error_detail or "kernel falhou")
                else:
                    raise RuntimeError(f"timeout apos {PART_RENDER_TIMEOUT // 60} min")
            except Exception as part_exc:  # noqa: BLE001 - uma parte falhar nao derruba as demais
                logger.warning("parte %s falhou: %s", idx, part_exc)
                db.update_part(project_id, idx, status="error", error=str(part_exc)[:400])
                failed += 1
        if ok and not failed:
            db.finish_job(job_id, f"{ok} parte(s) renderizadas", {"ok": ok, "failed": failed})
        elif ok:
            db.finish_job(
                job_id,
                f"{ok} parte(s) renderizadas, {failed} com erro — use 'Retomar render'",
                {"ok": ok, "failed": failed},
            )
        else:
            raise RuntimeError("Nenhuma parte renderizou com sucesso.")
    except Exception as exc:  # noqa: BLE001
        db.fail_job(job_id, "Falha no render por partes", str(exc))


@app.post("/projects/{project_id}/render-parts")
def render_parts(
    request: Request,
    project_id: int,
    background_tasks: BackgroundTasks,
    csrf_token: str = Form(""),
):
    user = require_user(request)
    verify_csrf(request, csrf_token)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    if not user.get("kaggle_username") or not user.get("kaggle_token"):
        raise HTTPException(400, "Configure Kaggle username e token em /settings.")
    if project.get("status") != "packaged":
        raise HTTPException(400, "Gere os pacotes em '03 Pacote' antes de renderizar as partes.")
    parts = db.list_parts(project_id)
    if not parts:
        raise HTTPException(400, "Projeto sem partes. Ative o modo video longo e gere o mapa novamente.")
    pending = [p for p in parts if p["status"] != "done"]
    if not pending:
        raise HTTPException(400, "Todas as partes ja foram renderizadas. Use 'Concatenar final'.")
    for kind in ("kaggle_parts", "concat_parts"):
        ensure_no_active_job(project_id, kind)
    job_id = db.create_job(
        user["id"], "kaggle_parts", project_id,
        f"Render de {len(pending)} parte(s) na fila",
    )
    background_tasks.add_task(
        run_kaggle_parts_job,
        job_id,
        project_id,
        user["id"],
        user["kaggle_username"],
        user["kaggle_token"],
    )
    return JSONResponse({"job_id": job_id, "parts": len(pending), "message": "Render por partes iniciado."})


def run_concat_job(job_id: int, project_id: int, user_id: int) -> None:
    try:
        db.update_job(job_id, status="running", message="Concatenando partes")
        project = db.get_project(project_id, user_id)
        if not project:
            raise RuntimeError("Projeto nao encontrado.")
        parts = db.list_parts(project_id)
        if not parts:
            raise RuntimeError("Projeto sem partes.")
        videos: list[Path] = []
        for part in sorted(parts, key=lambda p: p["part_idx"]):
            video = Path(part.get("video_path") or "")
            if part["status"] != "done" or not video.exists():
                raise RuntimeError(f"Parte {part['part_idx']} sem video renderizado.")
            videos.append(video)
        if not shutil.which("ffmpeg"):
            raise RuntimeError("FFmpeg nao encontrado no servidor; necessario para concatenar as partes.")

        out_dir = project_work_dir(project_id) / "kaggle_output"
        out_dir.mkdir(parents=True, exist_ok=True)
        base_out = out_dir / kaggle_service.BASE_VIDEO_NAME
        concat_list = out_dir / "concat_list.txt"
        concat_list.write_text(
            "\n".join(
                "file '{}'".format(v.resolve().as_posix().replace("'", r"'\''"))
                for v in videos
            ),
            encoding="utf-8",
        )

        def _ffmpeg(args: list[str], timeout: int = 1800) -> subprocess.CompletedProcess:
            return subprocess.run(["ffmpeg", "-y", *args], capture_output=True, timeout=timeout)

        # stream copy primeiro (partes usam o mesmo preset do montador); re-encode como fallback
        result = _ffmpeg(["-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", str(base_out)])
        if result.returncode != 0 or not base_out.exists() or base_out.stat().st_size == 0:
            db.update_job(job_id, status="running", message="Stream copy falhou; re-encodando")
            result = _ffmpeg(
                ["-f", "concat", "-safe", "0", "-i", str(concat_list),
                 "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-an", str(base_out)],
                timeout=3600,
            )
            if result.returncode != 0:
                raise RuntimeError(f"FFmpeg concat falhou: {result.stderr.decode(errors='replace')[:400]}")

        master_name = ""
        narration = find_input_media(project_id, "narration")
        if narration:
            db.update_job(job_id, status="running", message="Adicionando narracao ao master")
            master_out = out_dir / kaggle_service.MASTER_VIDEO_NAME
            result = _ffmpeg(
                ["-i", str(base_out), "-i", str(narration),
                 "-c:v", "copy", "-c:a", "aac", "-map", "0:v:0", "-map", "1:a:0",
                 "-shortest", str(master_out)],
            )
            if result.returncode == 0 and master_out.exists() and master_out.stat().st_size > 0:
                master_name = master_out.name
            else:
                logger.warning("mux de narracao falhou: %s", result.stderr.decode(errors="replace")[:300])

        concat_list.unlink(missing_ok=True)
        db.finish_job(
            job_id,
            "Video final concatenado" + (" com narracao" if master_name else ""),
            {"base": base_out.name, "master": master_name, "parts": len(videos)},
        )
    except Exception as exc:  # noqa: BLE001
        db.fail_job(job_id, "Falha na concatenacao", str(exc))


@app.post("/projects/{project_id}/concat-parts")
def concat_parts(
    request: Request,
    project_id: int,
    background_tasks: BackgroundTasks,
    csrf_token: str = Form(""),
):
    user = require_user(request)
    verify_csrf(request, csrf_token)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    parts = db.list_parts(project_id)
    if not parts:
        raise HTTPException(400, "Projeto sem partes.")
    not_done = [str(p["part_idx"]) for p in parts if p["status"] != "done"]
    if not_done:
        raise HTTPException(400, f"Renderize todas as partes antes de concatenar. Faltam: {', '.join(not_done)}")
    for kind in ("kaggle_parts", "concat_parts"):
        ensure_no_active_job(project_id, kind)
    job_id = db.create_job(user["id"], "concat_parts", project_id, "Concatenacao na fila")
    background_tasks.add_task(run_concat_job, job_id, project_id, user["id"])
    return JSONResponse({"job_id": job_id, "message": "Concatenacao iniciada."})


@app.get("/projects/{project_id}/parts-status")
def parts_status(request: Request, project_id: int):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    jobs = db.list_project_jobs(project_id, user["id"])
    active = next(
        (j for j in jobs if j["kind"] in {"kaggle_parts", "concat_parts"} and j["status"] in {"queued", "running"}),
        None,
    )
    outputs = local_output_videos(project_work_dir(project_id))
    return JSONResponse({
        "parts": db.list_parts(project_id),
        "active_job": {"id": active["id"], "kind": active["kind"], "message": active.get("message", "")} if active else None,
        "kaggle_username": user.get("kaggle_username", ""),
        "has_base_video": outputs["base"] is not None,
        "has_master_video": outputs["master"] is not None,
    })


@app.get("/jobs/{job_id}")
def job_status(request: Request, job_id: int):
    user = require_user(request)
    job = db.get_job(job_id, user["id"])
    if not job:
        raise HTTPException(404)
    return JSONResponse(job)


@app.get("/projects/{project_id}/jobs")
def project_jobs(request: Request, project_id: int):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    return JSONResponse({"project_status": project.get("status", ""), "jobs": db.list_project_jobs(project_id, user["id"])})


@app.get("/projects/{project_id}/diagnostics.json")
def project_diagnostics_json(request: Request, project_id: int, refresh: str = ""):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    scenes = db.list_scenes(project_id)
    selected = {row["scene_id"] for row in db.list_assets_by_state(project_id, CHOSEN_ASSET_STATES)}
    project_work = project_work_dir(project_id)
    validation = None
    if refresh:
        validation = diagnostics.validate_outputs(
            project_work,
            expected_duration=expected_duration_from_scenes(scenes),
        )
    snapshot = diagnostics.build_snapshot(
        project_work=project_work,
        zip_path=latest_zip(project_work),
        selected_count=len(selected),
        scene_count=len(scenes),
        expected_duration=expected_duration_from_scenes(scenes),
    )
    if validation:
        snapshot["outputs"]["validation"] = validation
    return JSONResponse(
        {
            "project": {"id": project_id, "name": project["name"], "status": project["status"]},
            "diagnostics": snapshot,
            "jobs": db.list_project_jobs(project_id, user["id"]),
        }
    )


@app.post("/projects/{project_id}/validate-output")
def validate_output(request: Request, project_id: int, csrf_token: str = Form("")):
    user = require_user(request)
    verify_csrf(request, csrf_token)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    scenes = db.list_scenes(project_id)
    payload = diagnostics.validate_outputs(
        project_work_dir(project_id),
        expected_duration=expected_duration_from_scenes(scenes),
    )
    return JSONResponse(payload)


def project_output_file(project_id: int, filename: str) -> Path:
    return project_work_dir(project_id) / "kaggle_output" / filename


@app.get("/projects/{project_id}/download-render-log")
def download_render_log(request: Request, project_id: int):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    path = project_output_file(project_id, "log_render.txt")
    if not path.exists():
        raise HTTPException(404, "Log de render ainda nao encontrado.")
    return FileResponse(path, filename=path.name, media_type="text/plain")


@app.get("/projects/{project_id}/download-validation")
def download_validation(request: Request, project_id: int):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    path = diagnostics.validation_path(project_work_dir(project_id))
    if not path.exists():
        raise HTTPException(404, "Validacao ainda nao gerada.")
    return FileResponse(path, filename=path.name, media_type="application/json")


@app.get("/projects/{project_id}/download-hyperframes-status")
def download_hyperframes_status(request: Request, project_id: int):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    path = project_output_file(project_id, "hyperframes_status.json")
    if not path.exists():
        raise HTTPException(404, "Status HyperFrames ainda nao encontrado.")
    return FileResponse(path, filename=path.name, media_type="application/json")


@app.get("/projects/{project_id}/kaggle-status")
def kaggle_status(request: Request, project_id: int):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    k_slug = project.get("kaggle_kernel_slug", "")
    if not k_slug:
        return JSONResponse({"status": "none"})
    if not user.get("kaggle_username") or not user.get("kaggle_token"):
        return JSONResponse({"status": "error", "error": "Credenciais Kaggle nao configuradas em /settings."})
    try:
        info = kaggle_service.get_status(k_slug, user["kaggle_username"], user["kaggle_token"])
        if info.get("status") == "complete":
            project_work = project_work_dir(project_id)
            outputs = local_output_videos(project_work)
            if not outputs["base"] and not outputs["master"]:
                kaggle_service.pull_output_video(
                    k_slug,
                    user["kaggle_username"],
                    user["kaggle_token"],
                    project_work / "kaggle_output",
                )
                outputs = local_output_videos(project_work)
            if outputs["master"]:
                info["master_video_url"] = f"/projects/{project_id}/download-master-video"
                info["video_url"] = info["master_video_url"]
            if outputs["base"]:
                info["base_video_url"] = f"/projects/{project_id}/download-base-video"
                if not info.get("video_url"):
                    info["video_url"] = info["base_video_url"]
            hf = local_hyperframes_status(project_work)
            if hf:
                info["hyperframes"] = hf
            info["validation"] = diagnostics.validate_outputs(
                project_work,
                expected_duration=expected_duration_from_scenes(db.list_scenes(project_id)),
            )
        db.update_kaggle_status(project_id, info["status"])
        return JSONResponse(info)
    except Exception as exc:
        return JSONResponse({"status": "error", "error": str(exc)})


@app.get("/projects/{project_id}/download-kaggle-video")
def download_kaggle_video(request: Request, project_id: int):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    video = latest_kaggle_video(project_work_dir(project_id))
    if not video:
        raise HTTPException(404, "Video do Kaggle ainda nao baixado.")
    return FileResponse(video, filename=video.name, media_type="video/mp4")


@app.get("/projects/{project_id}/download-base-video")
def download_base_video(request: Request, project_id: int):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    video = local_output_videos(project_work_dir(project_id))["base"]
    if not video:
        raise HTTPException(404, "Video base ainda nao baixado.")
    return FileResponse(video, filename=video.name, media_type="video/mp4")


@app.get("/projects/{project_id}/download-master-video")
def download_master_video(request: Request, project_id: int):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    video = local_output_videos(project_work_dir(project_id))["master"]
    if not video:
        raise HTTPException(404, "Video master ainda nao renderizado.")
    return FileResponse(video, filename=video.name, media_type="video/mp4")


@app.get("/projects/{project_id}/kaggle-debug")
def kaggle_debug(request: Request, project_id: int):
    """Retorna output bruto do CLI para diagnóstico."""
    if APP_ENV == "production" and os.getenv("ENABLE_KAGGLE_DEBUG") != "1":
        raise HTTPException(404)
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    u = user.get("kaggle_username", "")
    t = user.get("kaggle_token", "")
    k_slug = project.get("kaggle_kernel_slug", "")
    ds_slug = project.get("kaggle_dataset_slug", "")
    out = {}
    env = {**os.environ, "KAGGLE_USERNAME": u, "KAGGLE_KEY": t}
    for label, args in [
        ("kernels_files", ["kernels", "files", f"{u}/{k_slug}", "-v", "--page-size", "200"]),
        ("kernels_list", ["kernels", "list", "--mine"]),
        ("datasets_list", ["datasets", "list", "--mine"]),
    ]:
        try:
            r = subprocess.run([sys.executable, "-m", "kaggle"] + args,
                               env=env, capture_output=True, text=True, timeout=20)
            out[label] = {"stdout": r.stdout, "stderr": r.stderr, "code": r.returncode}
        except Exception as e:
            out[label] = {"error": str(e)}
    return JSONResponse({"k_slug": k_slug, "ds_slug": ds_slug, "results": out})


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "127.0.0.1")
    uvicorn.run("app:app", host=host, port=port, reload=APP_ENV == "dev")
