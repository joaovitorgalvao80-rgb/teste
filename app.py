"""NWRCH Studio - plataforma web de coleta e curadoria de B-rolls."""
from __future__ import annotations

import json
import hmac
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
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
from services import asset_search, diagnostics, edit_plan, groq_service, packager, kaggle_service
from services.script_parser import parse_script

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
BUSY_PROJECT_STATUSES = {"mapping", "searching", "packaging"}

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
}

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
    visual_style = str(cfg.get("visual_style") or "").strip()
    cfg["visual_style"] = visual_style or DEFAULT_CONFIG["visual_style"]
    return cfg

# ------------------------------------------------------------------
# App
# ------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_app: FastAPI):
    # garante pastas e banco antes de servir qualquer request
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    db.DATA_DIR = DATA_DIR
    db.DB_PATH = DATA_DIR / "plataforma.db"
    db.init_db()
    stale = db.fail_stale_jobs()
    if stale:
        print(f"[startup] {stale} job(s) pendentes de processo anterior marcados como erro")
    yield


app = FastAPI(title="NWRCH Studio", lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    secret_key=_require_secret(),
    max_age=60 * 60 * 24 * 7,
    https_only=APP_ENV == "production",
)

# static/ e criada antes do mount para evitar crash na inicializacao
_static_dir = ROOT / "static"
_static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

templates = Jinja2Templates(directory=str(ROOT / "templates"))


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


async def read_upload_limited(upload: UploadFile, max_bytes: int, label: str = "arquivo") -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            detail = label if "maximo" in label.lower() or "máximo" in label.lower() else f"{label} muito grande."
            raise HTTPException(400, detail)
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
    zip_path = Path(zip_path_str)
    try:
        db.update_job(job_id, status="running", message="Enviando dataset para o Kaggle")
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
    raw = await read_upload_limited(
        audio,
        MAX_TRANSCRIBE_UPLOAD_MB * 1024 * 1024,
        f"Arquivo muito grande (maximo {MAX_TRANSCRIBE_UPLOAD_MB} MB para upload)",
    )
    if len(raw) > MAX_TRANSCRIBE_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(400, f"Arquivo muito grande (máximo {MAX_TRANSCRIBE_UPLOAD_MB} MB para upload).")
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
    narration_media: Optional[UploadFile] = File(None),
    csrf_token: str = Form(""),
):
    user = require_user(request)
    verify_csrf(request, csrf_token)
    prepared_narration: Optional[tuple[bytes, str]] = None
    if narration_media and narration_media.filename:
        raw = await read_upload_limited(
            narration_media,
            MAX_TRANSCRIBE_UPLOAD_MB * 1024 * 1024,
            f"Arquivo muito grande (maximo {MAX_TRANSCRIBE_UPLOAD_MB} MB para upload)",
        )
        if raw:
            prepared_narration = prepare_narration_media(raw, narration_media.filename)
    config = normalize_project_config({
        "avatar_safe_area": avatar_safe_area,
        "visual_style": visual_style.strip() or DEFAULT_CONFIG["visual_style"],
        "resolution": resolution,
        "scene_duration": scene_duration,
        "image_fallback": image_fallback,
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
        s["selected"] = next((a for a in s["assets"] if a["state"] == "selected"), None)
    selected_count = sum(1 for s in scenes if s.get("selected"))
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
            "selected_count": selected_count,
            "has_keys": bool(user["pexels_key"] or user["pixabay_key"]),
            "narration_name": narration_file.name if narration_file else "",
            "avatar_name": avatar_file.name if avatar_file else "",
            "has_base_video": outputs["base"] is not None,
            "has_master_video": outputs["master"] is not None,
            "hyperframes_status": local_hyperframes_status(project_work) or {},
            "diagnostics": project_diagnostics_snapshot(project_id, scenes, selected_count),
            "jobs": jobs,
            "active_jobs": active_jobs,
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
        db.replace_scenes(project_id, merged)
        remove_project_artifacts(project_id, include_generated=True)
        db.set_project_status(project_id, "mapped")
        db.clear_kaggle_job(project_id)
        db.finish_job(job_id, "Mapa visual pronto", {"scenes": len(merged)})
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
# Buscar assets
# ------------------------------------------------------------------
def run_search_job(
    job_id: int,
    project_id: int,
    user_id: int,
    pexels_key: str,
    pixabay_key: str,
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
            {"added": total_added, "empty_scenes": empty_scenes, "scenes": len(scenes)},
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
    job_id = db.create_job(user["id"], "search_assets", project_id, "Busca de assets na fila")
    db.set_project_status(project_id, "searching")
    background_tasks.add_task(
        run_search_job,
        job_id,
        project_id,
        user["id"],
        user.get("pexels_key", ""),
        user.get("pixabay_key", ""),
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
):
    user = require_user(request)
    verify_csrf(request, csrf_token)
    if state not in {"pending", "selected", "rejected", "favorite"}:
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
    mark_project_dirty(owner["project_id"])
    return JSONResponse({"id": asset_id, "state": updated["state"]})


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
    data = await read_upload_limited(
        image,
        MAX_GENERATED_UPLOAD_MB * 1024 * 1024,
        f"Imagem muito grande (maximo {MAX_GENERATED_UPLOAD_MB} MB)",
    )
    if not data:
        raise HTTPException(400, "Imagem vazia.")
    if len(data) > MAX_GENERATED_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(400, f"Imagem muito grande (maximo {MAX_GENERATED_UPLOAD_MB} MB).")
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
    data = await read_upload_limited(
        media,
        MAX_MEDIA_UPLOAD_MB * 1024 * 1024,
        f"Arquivo muito grande (maximo {MAX_MEDIA_UPLOAD_MB} MB)",
    )
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
        selected_rows = db.list_assets_by_state(project_id, ["selected"])
        selected_by_scene = {row["scene_id"]: row for row in selected_rows}
        rejected = db.list_assets_by_state(project_id, ["rejected"])
        missing = missing_selected_scene_ids(scenes, selected_by_scene)
        if not selected_by_scene or missing:
            raise RuntimeError("Selecao incompleta; escolha um asset para cada cena.")
        project_work = project_work_dir(project_id)
        remove_project_artifacts(project_id)
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
        zip_path = packager.build_zip(
            project=project,
            config=config,
            scenes=scenes,
            selected_by_scene=selected_by_scene,
            rejected_assets=[
                {"scene_id": r["scene_code"], "source": r["source"], "url": r["download_url"], "keyword": r["keyword"]}
                for r in rejected
            ],
            work_dir=project_work,
            max_download_mb=config["max_download_mb"],
            edit_plan=plan,
            extra_files=[f for f in (narration_file, avatar_file) if f],
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
    selected_rows = db.list_assets_by_state(project_id, ["selected"])
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
    return JSONResponse({"jobs": db.list_project_jobs(project_id, user["id"])})


@app.get("/projects/{project_id}/diagnostics.json")
def project_diagnostics_json(request: Request, project_id: int, refresh: str = ""):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    scenes = db.list_scenes(project_id)
    selected = {row["scene_id"] for row in db.list_assets_by_state(project_id, ["selected"])}
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
