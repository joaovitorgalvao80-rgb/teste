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
from typing import Annotated, Optional
from urllib.parse import urlparse

from fastapi import BackgroundTasks, FastAPI, File, Form, Request, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse, PlainTextResponse
from pydantic import BaseModel
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

import database as db
from services import api_usage, asset_search, auto_select, diagnostics, edit_plan, groq_service, kaggle_service, ops_status, packager, scoring, vision
from services.project_config import (
    DEFAULT_CONFIG,
    LANGUAGES,
    _coerce_bool,
    _coerce_int,
    normalize_project_config,
    project_config,
    resolution_width,
)
from services.key_detect import KEY_FIELD_LABELS, MAX_KEYS_FILE_BYTES, detect_api_keys
from services.image_probe import image_kind_and_size as _image_kind_and_size
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
    or "20260615-partial-package"
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
ACTIVE_JOB_STATUSES = db.ACTIVE_JOB_STATUSES
# estados de take que contam como "escolhido" para pacote/diagnostico
CHOSEN_ASSET_STATES = ["selected", "accepted"]

EDIT_PLAN_FILENAME = "edit_plan.json"

# Constantes reutilizadas (evita literais duplicados espalhados pelo modulo)
PROJECTS_PATH = "/projects"
MEDIA_TYPE_JSON = "application/json"
MEDIA_TYPE_MP4 = "video/mp4"
MSG_PROJECT_NOT_FOUND = "Projeto nao encontrado."
MSG_NO_API_KEYS = "Cadastre ao menos uma chave de API em /settings."

# Respostas de erro comuns documentadas no OpenAPI (responses=) das rotas que
# levantam HTTPException. Centraliza a documentacao em vez de repetir por rota.
ERROR_RESPONSES = {
    400: {"description": "Requisicao invalida"},
    401: {"description": "Nao autenticado / sessao expirada"},
    403: {"description": "Acesso negado"},
    404: {"description": "Recurso nao encontrado"},
    409: {"description": "Conflito com o estado atual do projeto"},
    500: {"description": "Erro interno do servidor"},
}

# Defaults, coerção e normalização de config vivem em services/project_config.py
# (importados abaixo). _coerce_bool/_coerce_int seguem usados em rotas daqui.

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


def keyword_role_label(role: str) -> str:
    return scoring.ROLE_LABELS_PT.get(role or "", "reserva")


templates.env.globals["role_label"] = keyword_role_label


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


def has_visual_provider(user: dict) -> bool:
    return bool(user.get("pexels_key") or user.get("pixabay_key") or user.get("coverr_key"))


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
    return MEDIA_TYPE_JSON in request.headers.get("accept", "")


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


def scene_broll_flags(scenes: list[dict], config: dict) -> dict:
    """Mapeia scene_id -> bool (leva b-roll?) usando a MESMA decisao do render.

    No modo longo decide por parte (cada parte e um render avatar-base proprio,
    com seu primeiro/ultimo quadro no avatar). Permite a busca pular as cenas
    avatar-only (apresentacao, respiros) em vez de buscar imagem a toa.
    """
    out: dict = {}
    if config.get("long_mode"):
        by_part: dict = {}
        for s in scenes:
            by_part.setdefault(int(s.get("part") or 1), []).append(s)
        for idx in sorted(by_part):
            grp = by_part[idx]
            for s, flag in zip(grp, edit_plan.decide_broll(grp)):
                out[s["scene_id"]] = flag
    else:
        for s, flag in zip(scenes, edit_plan.decide_broll(scenes)):
            out[s["scene_id"]] = flag
    return out


def missing_selected_scene_ids(
    scenes: list[dict],
    selected_by_scene: dict[int, dict],
    required_scene_ids: Optional[set] = None,
) -> list[str]:
    """Cenas (que DEVEM ter b-roll) sem take escolhido. Cenas avatar-only nao
    entram em required_scene_ids, entao nao sao cobradas."""
    return [
        s["scene_id"] for s in scenes
        if s["id"] not in selected_by_scene
        and (required_scene_ids is None or s["scene_id"] in required_scene_ids)
    ]


def annotate_broll_requirements(scenes: list[dict], config: dict) -> dict:
    """Marca cenas que realmente precisam de take e retorna contadores de curadoria."""
    broll_map = scene_broll_flags(scenes, config) if scenes else {}
    stats = {
        "required": 0,
        "avatar_only": 0,
        "selected": 0,
        "accepted": 0,
        "pending": 0,
        "waiting": 0,
    }
    for scene in scenes:
        required = bool(broll_map.get(scene["scene_id"], True))
        scene["broll_required"] = required
        scene["avatar_only"] = not required
        if not required:
            stats["avatar_only"] += 1
            continue
        stats["required"] += 1
        chosen = scene.get("selected") or scene.get("chosen")
        if chosen and chosen.get("state") in CHOSEN_ASSET_STATES:
            stats["selected"] += 1
            if chosen.get("state") == "accepted":
                stats["accepted"] += 1
            else:
                stats["pending"] += 1
        else:
            stats["waiting"] += 1
    return stats


def problem_scenes(scenes: list[dict], config: dict, limit: int = 8) -> list[dict]:
    """Cenas de b-roll que merecem revisao antes de gastar render."""
    target_w = resolution_width(config)
    out: list[dict] = []
    for scene in scenes:
        if not scene.get("broll_required"):
            continue
        reasons: list[str] = []
        assets = scene.get("assets") or []
        chosen = scene.get("selected")
        if not assets:
            reasons.append("sem candidatos")
        elif not chosen:
            reasons.append("sem take escolhido")
        else:
            if chosen.get("low_relevance"):
                reasons.append("take escolhido com baixa relevancia")
            if chosen.get("vision_verdict") == "descartar":
                reasons.append("visao sugere descartar")
            if chosen.get("asset_type") == "video":
                if float(chosen.get("duration") or 0) < float(scene.get("duration") or 0) * 0.5:
                    reasons.append("video curto para a cena")
            if int(chosen.get("width") or 0) and int(chosen.get("width") or 0) < target_w * 0.66:
                reasons.append("resolucao baixa")
        if scene.get("low_relevance_count", 0) >= max(2, len(assets) // 2) and assets:
            reasons.append(f"{scene['low_relevance_count']} candidatos fracos")
        if reasons:
            out.append({
                "scene_id": scene.get("scene_id", ""),
                "scene_db_id": scene.get("id"),
                "part": int(scene.get("part") or 1),
                "narration": str(scene.get("narration") or "")[:120],
                "reasons": reasons[:4],
                "has_assets": bool(assets),
                "has_choice": bool(chosen),
            })
    return out[:limit]


_VISION_PROVIDER = vision.HeuristicVisionProvider()
# Quantos candidatos por cena a IA de visao analisa de fato (os melhores pela
# heuristica). Limita custo/tempo do LLM; o resto fica na heuristica offline.
VISION_LLM_TOP_N = int(os.getenv("VISION_LLM_TOP_N", "8"))
# Tamanho do contact-sheet: candidatas julgadas numa UNICA chamada (multi-imagem).
# O llama-4 do Groq lida bem com ~5 imagens/requisicao; acima disso degrada.
VISION_SHEET_N = int(os.getenv("VISION_SHEET_N", "5"))

# Ordenação da galeria: take escolhido primeiro, depois melhores por visão/relevância.
_TAKE_STATE_RANK = {"accepted": 3, "selected": 2, "favorite": 1, "pending": 0, "rejected": -1}


def _take_sort_key(asset: dict) -> tuple:
    return (
        _TAKE_STATE_RANK.get(asset.get("state", "pending"), 0),
        float(asset.get("vision_score") or 0),
        float(asset.get("relevance") or 0),
    )


def annotate_assets_with_vision(scene: dict, assets: list[dict], config: dict) -> list[dict]:
    """Anexa sinais de curadoria a cada asset para a UI (relevância, alerta, motivo).

    Usa o provedor de visão heurístico (offline). Não altera o banco: são campos
    derivados, calculados a cada render, para a galeria de seleção manual.
    """
    annotated: list[dict] = []
    for asset in assets:
        item = dict(asset)
        relevance = scoring.keyword_relevance(scene, asset)
        if asset.get("vision_analyzed"):
            # análise persistida (job de visão já rodou) — fonte de verdade
            try:
                flags = json.loads(asset.get("vision_flags_json") or "[]")
            except (TypeError, ValueError):
                flags = []
            verdict = asset.get("vision_verdict") or ""
            item["vision_score"] = asset.get("vision_score") or 0
            item["vision_flags"] = flags
            item["vision_verdict"] = verdict
            item["vision_reason"] = asset.get("vision_reason") or ""
            item["low_relevance"] = verdict == "descartar" or relevance < 0.33
        else:
            # ainda não analisado: estimativa heurística offline ao vivo
            analysis = _VISION_PROVIDER.analyze(asset, scene, config)
            item["vision_score"] = analysis.score
            item["vision_flags"] = analysis.flags
            item["vision_verdict"] = analysis.verdict
            item["vision_reason"] = "; ".join(analysis.reasons)
            item["low_relevance"] = analysis.relevance < 0.33
        item["relevance"] = round(relevance, 3)
        item["relevance_label"] = scoring.relevance_label(relevance)
        annotated.append(item)
    return annotated


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


class JobCanceled(RuntimeError):
    pass


def check_job_canceled(job_id: int) -> None:
    if db.is_job_canceling(job_id):
        raise JobCanceled("Tarefa cancelada pelo usuario.")


def cancel_project_status(project_id: int, kind: str) -> Optional[str]:
    if kind == "generate_map":
        return "mapped" if db.list_scenes(project_id) else "created"
    if kind == "search_assets":
        if any(db.list_assets_for_project(project_id).values()):
            return "searched"
        return "mapped" if db.list_scenes(project_id) else "created"
    if kind in ("auto_select", "auto_select_vision"):
        return "reviewing" if any(db.list_assets_for_project(project_id).values()) else "searched"
    if kind == "research_rejected":
        return "reviewing"
    if kind == "package":
        return "reviewed" if curation_report_path(project_id).exists() else "reviewing"
    return None


def finish_canceled_job(job_id: int, project_id: int, kind: str, message: str = "Tarefa cancelada") -> None:
    next_status = cancel_project_status(project_id, kind)
    if next_status:
        db.set_project_status(project_id, next_status)
    if kind == "kaggle_send":
        db.update_kaggle_status(project_id, "cancelacknowledged")
    db.cancel_job(job_id, message)


def expected_duration_from_scenes(scenes: list[dict]) -> float:
    return max((float(s.get("end_time") or 0) for s in scenes), default=0.0)


def project_diagnostics_snapshot(
    project_id: int,
    scenes: list[dict],
    selected_count: int,
    required_count: Optional[int] = None,
) -> dict:
    project_work = project_work_dir(project_id)
    return diagnostics.build_snapshot(
        project_work=project_work,
        zip_path=latest_zip(project_work),
        selected_count=selected_count,
        scene_count=required_count if required_count is not None else len(scenes),
        total_scene_count=len(scenes),
        expected_duration=expected_duration_from_scenes(scenes),
    )


def safe_next_url(raw_next: str) -> str:
    """Aceita apenas redirects internos, evitando open redirect no login."""
    if not raw_next:
        return PROJECTS_PATH
    # navegadores tratam '\' como '/': "/\evil.com" viraria "//evil.com"
    if "\\" in raw_next or any(ord(ch) < 0x20 for ch in raw_next):
        return PROJECTS_PATH
    parsed = urlparse(raw_next)
    if parsed.scheme or parsed.netloc or not raw_next.startswith("/") or raw_next.startswith("//"):
        return PROJECTS_PATH
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
    user_id: int,
    project_name: str,
    username: str,
    token: str,
    zip_path_str: str,
) -> None:
    try:
        check_job_canceled(job_id)
        db.update_job(job_id, status="running", message="Enviando dataset para o Kaggle")
        # copia o ZIP antes do upload: mark_project_dirty pode apagar o original
        # se o usuario alterar o projeto enquanto o envio roda em background
        with api_usage.context(user_id=user_id, project_id=project_id, job_id=job_id, operation="kaggle_send"):
            with tempfile.TemporaryDirectory(prefix="nwrch_kaggle_") as tmp:
                zip_path = Path(tmp) / Path(zip_path_str).name
                shutil.copy2(zip_path_str, zip_path)
                check_job_canceled(job_id)
                ds_slug = kaggle_service.upload_dataset(zip_path, project_name, username, token, project_id=project_id)
        check_job_canceled(job_id)
        db.update_job(
            job_id,
            status="running",
            message="Criando kernel de render no Kaggle",
            result={"dataset_slug": ds_slug},
        )
        check_job_canceled(job_id)
        with api_usage.context(user_id=user_id, project_id=project_id, job_id=job_id, operation="kaggle_send"):
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
    except JobCanceled:
        finish_canceled_job(job_id, project_id, "kaggle_send")
    except Exception as exc:  # noqa: BLE001 - registra falha operacional para a UI
        db.update_kaggle_status(project_id, "error")
        db.fail_job(job_id, "Falha ao enviar para o Kaggle", str(exc))


# ------------------------------------------------------------------
# Health check (Railway / load balancer)
# ------------------------------------------------------------------
@app.get("/health", responses=ERROR_RESPONSES)
def health():
    return {"status": "ok"}


# ------------------------------------------------------------------
# Auth
# ------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse, responses=ERROR_RESPONSES)
def home(request: Request):
    if current_user(request):
        return RedirectResponse(PROJECTS_PATH, status_code=303)
    return RedirectResponse("/login", status_code=303)


@app.get("/login", response_class=HTMLResponse, responses=ERROR_RESPONSES)
def login_page(request: Request, error: str = "", next: str = ""):
    return render_template(request, "login.html", {"error": error, "next": next})


@app.post("/login", responses=ERROR_RESPONSES)
def login(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "",
):
    user = db.get_user_by_name(username.strip())
    if not user or not db.verify_password(password, user["password_hash"]):
        return RedirectResponse("/login?error=Credenciais+invalidas", status_code=303)
    request.session.clear()  # evita session fixation
    request.session["user_id"] = user["id"]
    return RedirectResponse(safe_next_url(next), status_code=303)


@app.post("/register", responses=ERROR_RESPONSES)
def register(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    invite_code: Annotated[str, Form()] = "",
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


@app.get("/logout", responses=ERROR_RESPONSES)
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ------------------------------------------------------------------
# Settings (APIs)
# ------------------------------------------------------------------
@app.get("/settings", response_class=HTMLResponse, responses=ERROR_RESPONSES)
def settings_page(request: Request, saved: str = ""):
    user = require_user(request)
    secret_masks = {
        "pexels": mask_secret(user.get("pexels_key", "")),
        "pixabay": mask_secret(user.get("pixabay_key", "")),
        "groq": mask_secret(user.get("groq_key", "")),
        "coverr": mask_secret(user.get("coverr_key", "")),
        "nvidia": mask_secret(user.get("nvidia_key", "")),
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
            "integration_status": ops_status.integration_snapshot(user, APP_ENV),
            "api_usage": db.api_usage_summary(user["id"]),
        },
    )


@app.get("/settings/integrations-status", responses=ERROR_RESPONSES)
def integrations_status(request: Request):
    user = require_user(request)
    return JSONResponse(ops_status.integration_snapshot(user, APP_ENV))


@app.get("/settings/test-kaggle", responses=ERROR_RESPONSES)
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


class SettingsForm(BaseModel):
    """Campos do formulário de /settings (agrupa as chaves de API e flags de limpeza)."""
    pexels: str = ""
    pixabay: str = ""
    groq: str = ""
    groq_model: str = ""
    coverr: str = ""
    nvidia: str = ""
    kaggle_username: str = ""
    kaggle_token: str = ""
    clear_pexels: str = ""
    clear_pixabay: str = ""
    clear_groq: str = ""
    clear_coverr: str = ""
    clear_nvidia: str = ""
    clear_kaggle_token: str = ""
    csrf_token: str = ""


@app.post("/settings", responses=ERROR_RESPONSES)
def settings_save(request: Request, form: Annotated[SettingsForm, Form()]):
    user = require_user(request)
    verify_csrf(request, form.csrf_token)
    db.update_api_keys(
        user["id"],
        secret_from_form(user.get("pexels_key", ""), form.pexels, form.clear_pexels),
        secret_from_form(user.get("pixabay_key", ""), form.pixabay, form.clear_pixabay),
        secret_from_form(user.get("groq_key", ""), form.groq, form.clear_groq),
        form.groq_model.strip(),
        coverr=secret_from_form(user.get("coverr_key", ""), form.coverr, form.clear_coverr),
        nvidia=secret_from_form(user.get("nvidia_key", ""), form.nvidia, form.clear_nvidia),
    )
    db.update_kaggle_keys(
        user["id"],
        form.kaggle_username.strip(),
        secret_from_form(user.get("kaggle_token", ""), form.kaggle_token, form.clear_kaggle_token),
    )
    return RedirectResponse("/settings?saved=1", status_code=303)


# ------------------------------------------------------------------
# Importação de chaves por arquivo .txt (detecção automática)
# ------------------------------------------------------------------
# Detecção de chaves (formatos, rótulos, parsing) vive em services/key_detect.py.


@app.post("/settings/import-keys", responses=ERROR_RESPONSES)
async def import_keys(
    request: Request,
    keys_file: Annotated[UploadFile, File()],
    csrf_token: Annotated[str, Form()] = "",
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
        coverr=detected.get("coverr", user.get("coverr_key", "")),
        nvidia=detected.get("nvidia", user.get("nvidia_key", "")),
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
_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac", ".wma"}
_ALLOWED_UPLOAD_EXTS = _VIDEO_EXTS | _AUDIO_EXTS
_GROQ_MAX_BYTES = 24 * 1024 * 1024  # 25 MB hard limit da API; 24 MB de margem


def _write_temp_upload(raw: bytes, tmp_dir: str) -> Path:
    with tempfile.NamedTemporaryFile(prefix="upload_", suffix=".bin", dir=tmp_dir, delete=False) as src_file:
        src_file.write(raw)
        return Path(src_file.name)


def _extract_audio_bytes(raw: bytes, filename: str) -> tuple[bytes, str]:
    """Se for vídeo ou arquivo grande, extrai/comprime para MP3 mono 64k via FFmpeg."""
    raw_ext = Path(filename).suffix.lower()
    is_video = raw_ext in _VIDEO_EXTS
    if not is_video and len(raw) <= _GROQ_MAX_BYTES:
        return raw, filename
    if not shutil.which("ffmpeg"):
        raise HTTPException(
            500,
            "FFmpeg não encontrado no servidor; necessário para extrair áudio de vídeo. "
            "Instale o FFmpeg ou envie um arquivo de áudio (mp3/wav) direto.",
        )
    with tempfile.TemporaryDirectory() as tmp:
        src = _write_temp_upload(raw, tmp)
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


@app.get("/static/empty.vtt", include_in_schema=False)
def empty_vtt() -> PlainTextResponse:
    return PlainTextResponse("WEBVTT\n\n", media_type="text/vtt")


@app.post("/transcribe-audio", responses=ERROR_RESPONSES)
async def transcribe_audio(
    request: Request,
    audio: Annotated[UploadFile, File()],
    csrf_token: Annotated[str, Form()] = "",
    language: Annotated[str, Form()] = DEFAULT_CONFIG["script_language"],
):
    user = require_user(request)
    verify_csrf(request, csrf_token)
    if not user.get("groq_key"):
        raise HTTPException(400, "Configure a chave Groq em /settings para usar transcrição.")
    raw = await read_upload_limited(audio, MAX_TRANSCRIBE_UPLOAD_MB * 1024 * 1024)
    try:
        data, fname = _extract_audio_bytes(raw, audio.filename or "audio.mp4")
        with api_usage.context(user_id=user["id"], operation="transcribe_audio"):
            transcript = groq_service.transcribe_audio(data, fname, user["groq_key"], language=language)
        return JSONResponse({"transcript": transcript})
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, f"Transcrição falhou: {exc}") from exc


# ------------------------------------------------------------------
# Projects
# ------------------------------------------------------------------
@app.get("/projects", response_class=HTMLResponse, responses=ERROR_RESPONSES)
def projects_page(request: Request):
    user = require_user(request)
    projects = db.list_projects(user["id"])
    return render_template(request, "projects.html", {"user": user, "projects": projects})


@app.get("/projects/new", response_class=HTMLResponse, responses=ERROR_RESPONSES)
def new_project_page(request: Request):
    user = require_user(request)
    return render_template(request, "new_project.html", {"user": user, "config": DEFAULT_CONFIG, "languages": LANGUAGES})


@app.post("/projects/new", responses=ERROR_RESPONSES)
async def new_project(
    request: Request,
    name: Annotated[str, Form()],
    script: Annotated[str, Form()],
    avatar_safe_area: Annotated[str, Form()] = "right",
    visual_style: Annotated[str, Form()] = DEFAULT_CONFIG["visual_style"],
    resolution: Annotated[str, Form()] = "1920x1080",
    scene_duration: Annotated[float, Form()] = 4.0,
    image_fallback: Annotated[str, Form()] = "",
    long_mode: Annotated[str, Form()] = "",
    script_language: Annotated[str, Form()] = DEFAULT_CONFIG["script_language"],
    narration_media: Annotated[Optional[UploadFile], File()] = None,
    csrf_token: Annotated[str, Form()] = "",
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
        "script_language": script_language,
    })
    pid = db.create_project(user["id"], name.strip() or "projeto", script, config)
    if prepared_narration:
        save_input_media_bytes(pid, "narration", prepared_narration[0], prepared_narration[1])
    return RedirectResponse(f"/projects/{pid}", status_code=303)


@app.post("/projects/{project_id}/delete", responses=ERROR_RESPONSES)
def delete_project(request: Request, project_id: int, csrf_token: Annotated[str, Form()] = ""):
    user = require_user(request)
    verify_csrf(request, csrf_token)
    project = db.get_project(project_id, user["id"])
    if project:
        ensure_project_not_busy(project)
        db.delete_project(project_id, user["id"])
        remove_project_workspace(project_id)
    return RedirectResponse(PROJECTS_PATH, status_code=303)


@app.get("/projects/{project_id}", response_class=HTMLResponse, responses=ERROR_RESPONSES)
def project_page(request: Request, project_id: int):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    config = project_config(project)
    scenes = db.list_scenes(project_id)
    assets_by_scene = db.list_assets_for_project(project_id)
    for s in scenes:
        annotated = annotate_assets_with_vision(s, assets_by_scene.get(s["id"], []), config)
        annotated.sort(key=_take_sort_key, reverse=True)
        s["assets"] = annotated
        s["selected"] = next((a for a in s["assets"] if a["state"] in CHOSEN_ASSET_STATES), None)
        s["low_relevance_count"] = sum(1 for a in annotated if a.get("low_relevance"))
    curation_stats = annotate_broll_requirements(scenes, config)
    asset_count = sum(len(s["assets"]) for s in scenes)
    selected_count = curation_stats["selected"]
    accepted_count = curation_stats["accepted"]
    project_work = project_work_dir(project_id)
    narration_file = find_input_media(project_id, "narration")
    avatar_file = find_input_media(project_id, "avatar")
    outputs = local_output_videos(project_work)
    jobs = db.list_project_jobs(project_id, user["id"])
    active_jobs = [job for job in jobs if job.get("status") in ACTIVE_JOB_STATUSES]
    parts = db.list_parts(project_id) if config.get("long_mode") else []
    # No modo longo a galeria de curadoria mostra so as cenas da parte atual
    # (a primeira ainda nao curada); evita misturar partes numa lista enorme.
    current_part = None
    if parts:
        current_part = next(
            (p["part_idx"] for p in parts if p.get("curation_status") != "curated"),
            parts[-1]["part_idx"],
        )
        gallery_scenes = [s for s in scenes if int(s.get("part") or 1) == current_part]
    else:
        gallery_scenes = scenes
    diagnostics_snapshot = project_diagnostics_snapshot(
        project_id,
        scenes,
        selected_count,
        curation_stats["required"],
    )
    api_summary = db.api_usage_summary(user["id"], project_id=project_id)
    scene_alerts = problem_scenes(scenes, config)
    operational_state = ops_status.project_state(
        project,
        scenes=scenes,
        asset_count=asset_count,
        curation_stats=curation_stats,
        jobs=jobs,
        parts=parts,
        outputs=outputs,
        diagnostics=diagnostics_snapshot,
        has_asset_keys=has_visual_provider(user),
    )
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
            "broll_required_count": curation_stats["required"],
            "avatar_only_count": curation_stats["avatar_only"],
            "has_keys": has_visual_provider(user),
            "narration_name": narration_file.name if narration_file else "",
            "avatar_name": avatar_file.name if avatar_file else "",
            "has_base_video": outputs["base"] is not None,
            "has_master_video": outputs["master"] is not None,
            "edit_plan": local_edit_plan(project_id),
            "hyperframes_status": local_hyperframes_status(project_work) or {},
            "diagnostics": diagnostics_snapshot,
            "api_usage": api_summary,
            "problem_scenes": scene_alerts,
            "operational_state": operational_state,
            "jobs": jobs,
            "active_jobs": active_jobs,
            "active_job_statuses": ACTIVE_JOB_STATUSES,
            "parts": parts,
            "parts_job_active": any(
                j["kind"] in {"kaggle_parts", "concat_parts"} and j["status"] in ACTIVE_JOB_STATUSES
                for j in jobs
            ),
            "search_part_active": any(
                j["kind"] == "search_part" and j["status"] in ACTIVE_JOB_STATUSES for j in jobs
            ),
            "auto_select_active": any(
                j["kind"] in {"auto_select", "auto_select_vision"} and j["status"] in ACTIVE_JOB_STATUSES
                for j in jobs
            ),
            "all_parts_curated": bool(parts)
            and all(p.get("curation_status") == "curated" for p in parts),
            "current_part": current_part,
            "gallery_scenes": gallery_scenes,
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
        check_job_canceled(job_id)
        db.update_job(job_id, status="running", message="Gerando mapa visual")
        project = db.get_project(project_id, user_id)
        if not project:
            raise RuntimeError(MSG_PROJECT_NOT_FOUND)
        config = project_config(project)
        base_scenes = parse_script(project["script"], config["scene_duration"])
        if not base_scenes:
            raise RuntimeError("Roteiro vazio ou invalido.")
        check_job_canceled(job_id)
        # Tema global: ancora keywords e visao no assunto do video inteiro.
        with api_usage.context(user_id=user_id, project_id=project_id, job_id=job_id, operation="generate_map"):
            video_theme = groq_service.infer_video_theme(
                base_scenes, groq_key, groq_model or groq_service.DEFAULT_MODEL,
                language=config["script_language"],
            )
        check_job_canceled(job_id)
        if video_theme:
            config["video_theme"] = video_theme
            db.set_project_config(project_id, config)
        with api_usage.context(user_id=user_id, project_id=project_id, job_id=job_id, operation="generate_map"):
            briefs = groq_service.generate_briefs(
                base_scenes,
                groq_key=groq_key,
                style=config["visual_style"],
                avatar_safe_area=config["avatar_safe_area"],
                safe_ratio=config["avatar_safe_width_ratio"],
                model=groq_model or groq_service.DEFAULT_MODEL,
                video_theme=video_theme,
                language=config["script_language"],
            )
        check_job_canceled(job_id)
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
    except JobCanceled:
        finish_canceled_job(job_id, project_id, "generate_map")
    except Exception as exc:  # noqa: BLE001
        db.set_project_status(project_id, "map_failed")
        db.fail_job(job_id, "Falha ao gerar mapa visual", str(exc))


@app.post("/projects/{project_id}/generate-map", responses=ERROR_RESPONSES)
def generate_map(
    request: Request,
    project_id: int,
    background_tasks: BackgroundTasks,
    csrf_token: Annotated[str, Form()] = "",
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
    part_idx: Optional[int] = None,
) -> int:
    """Escolhe o melhor take pendente para cada cena sem take aceito.

    Cenas com asset 'accepted' (aprovado na revisao) nunca sao tocadas.
    Quando ``part_idx`` e informado, so considera as cenas daquela parte.
    Retorna o numero de cenas com take selecionado automaticamente.
    """
    scenes = db.list_scenes(project_id)
    if part_idx is not None:
        scenes = [s for s in scenes if int(s.get("part") or 1) == part_idx]
    assets_by_scene = db.list_assets_for_project(project_id)
    broll_map = scene_broll_flags(scenes, config)
    target_scenes = []
    candidates_by_scene: dict[int, list[dict]] = {}
    for scene in scenes:
        if not broll_map.get(scene["scene_id"], True):
            continue
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
            check_job_canceled(job_id)
            db.update_job(job_id, status="running", message=f"Selecionando takes ({done}/{total} cenas)")

    choices = auto_select.choose_best_takes(
        target_scenes,
        candidates_by_scene,
        config,
        groq_key=groq_key,
        model=groq_model or groq_service.DEFAULT_MODEL,
        progress=progress,
    )
    if job_id:
        check_job_canceled(job_id)
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
    coverr_key: str = "",
    nvidia_key: str = "",
) -> None:
    try:
        check_job_canceled(job_id)
        db.update_job(job_id, status="running", message="Buscando assets")
        project = db.get_project(project_id, user_id)
        if not project:
            raise RuntimeError(MSG_PROJECT_NOT_FOUND)
        config = project_config(project)
        max_w = resolution_width(config)
        scenes = db.list_scenes(project_id)
        if not scenes:
            raise RuntimeError("Gere o mapa visual antes da busca.")
        seen: set = set()
        total_added = 0
        empty_scenes: list[str] = []
        # decide de antemao quais cenas levam b-roll; nao busca imagem para as
        # cenas avatar-only (apresentacao/respiros) -> menos API, pool mais limpo.
        broll_map = scene_broll_flags(scenes, config)
        broll_count = sum(1 for s in scenes if broll_map.get(s["scene_id"], True))
        for scene in scenes:
            check_job_canceled(job_id)
            if not broll_map.get(scene["scene_id"], True):
                continue
            db.update_job(job_id, status="running", message=f"Buscando {scene['scene_id']}")
            with api_usage.context(user_id=user_id, project_id=project_id, job_id=job_id, operation="search_assets"):
                results = asset_search.search_scene(
                    scene["keywords"],
                    pexels_key,
                    pixabay_key,
                    max_w=max_w,
                    per_keyword=config["per_keyword"],
                    allow_images=bool(config["image_fallback"]),
                    seen_urls=seen,
                    coverr_key=coverr_key,
                    extra_image_banks=True,
                )
            check_job_canceled(job_id)
            added = db.add_assets(scene["id"], results)
            total_added += added
            if added == 0:
                empty_scenes.append(scene["scene_id"])
        # so e erro se HAVIA cenas de b-roll para buscar e nada veio (chaves/APIs);
        # um projeto so de avatar (sem b-roll) legitimamente nao busca nada.
        if broll_count > 0 and total_added <= 0:
            raise RuntimeError("Busca retornou zero assets. Verifique chaves, keywords ou disponibilidade das APIs.")

        # A analise de visao NAO roda automaticamente na busca: e uma acao sob
        # demanda (botao "Analisar visao" -> /analyze-vision). Em projetos longos,
        # analisar centenas de assets dentro da busca era inviavel (lento + rate
        # limit das APIs de visao). A galeria ja mostra a relevancia por keyword
        # (heuristica) para a curadoria manual; a visao fica disponivel para quando
        # o usuario quiser refinar a selecao ou automatizar videos menores.
        analyzed = 0
        vision_provider = ""

        check_job_canceled(job_id)
        db.set_project_status(project_id, "searched")
        db.finish_job(
            job_id,
            "Busca concluida",
            {
                "added": total_added,
                "empty_scenes": empty_scenes,
                "scenes": len(scenes),
                "auto_selected": 0,
                "vision_analyzed": analyzed,
                "vision_provider": vision_provider,
            },
        )
    except JobCanceled:
        finish_canceled_job(job_id, project_id, "search_assets")
    except Exception as exc:  # noqa: BLE001
        db.set_project_status(project_id, "search_failed")
        db.fail_job(job_id, "Falha na busca de assets", str(exc))


def run_part_search_job(
    job_id: int,
    project_id: int,
    user_id: int,
    part_idx: int,
    pexels_key: str,
    pixabay_key: str,
    groq_key: str = "",
    groq_model: str = "",
    coverr_key: str = "",
    nvidia_key: str = "",
) -> None:
    """Busca assets de UMA parte e ja faz a selecao automatica em seguida.

    Cada parte e um job proprio e independente: se a parte 3 falhar por
    rate-limit, as partes 1-2 ja confirmadas nao se perdem (era o risco do
    job unico que varria todas as cenas de uma vez).
    """
    try:
        check_job_canceled(job_id)
        db.update_part(project_id, part_idx, curation_status="searching")
        db.update_job(job_id, status="running", message=f"Buscando assets da parte {part_idx}")
        project = db.get_project(project_id, user_id)
        if not project:
            raise RuntimeError(MSG_PROJECT_NOT_FOUND)
        config = project_config(project)
        max_w = resolution_width(config)
        all_scenes = db.list_scenes(project_id)
        scenes = [s for s in all_scenes if int(s.get("part") or 1) == part_idx]
        if not scenes:
            raise RuntimeError(f"Parte {part_idx} sem cenas.")
        seen: set = set()
        total_added = 0
        empty_scenes: list[str] = []
        broll_map = scene_broll_flags(scenes, config)
        broll_count = sum(1 for s in scenes if broll_map.get(s["scene_id"], True))
        for scene in scenes:
            check_job_canceled(job_id)
            if not broll_map.get(scene["scene_id"], True):
                continue
            db.update_job(job_id, status="running", message=f"Buscando {scene['scene_id']}")
            with api_usage.context(user_id=user_id, project_id=project_id, job_id=job_id, operation="search_part"):
                results = asset_search.search_scene(
                    scene["keywords"],
                    pexels_key,
                    pixabay_key,
                    max_w=max_w,
                    per_keyword=config["per_keyword"],
                    allow_images=bool(config["image_fallback"]),
                    seen_urls=seen,
                    coverr_key=coverr_key,
                    extra_image_banks=True,
                )
            check_job_canceled(job_id)
            added = db.add_assets(scene["id"], results)
            total_added += added
            if added == 0:
                empty_scenes.append(scene["scene_id"])
        if broll_count > 0 and total_added <= 0:
            raise RuntimeError("Busca retornou zero assets. Verifique chaves, keywords ou disponibilidade das APIs.")

        # Selecao automatica embutida: ao terminar a busca da parte, a IA ja
        # escolhe o melhor take de cada cena e a parte cai direto na revisao.
        check_job_canceled(job_id)
        db.update_job(job_id, status="running", message="Selecionando os melhores takes")
        with api_usage.context(user_id=user_id, project_id=project_id, job_id=job_id, operation="search_part_auto_select"):
            chosen = auto_select_for_project(
                project_id, config, groq_key, groq_model, job_id=job_id,
                review_round=int(project.get("review_round") or 0), part_idx=part_idx,
            )

        check_job_canceled(job_id)
        db.update_part(project_id, part_idx, curation_status="reviewing")
        db.set_project_status(project_id, "reviewing")
        db.finish_job(
            job_id,
            f"Parte {part_idx}: {total_added} takes buscados, {chosen} selecionados",
            {
                "part": part_idx,
                "added": total_added,
                "empty_scenes": empty_scenes,
                "scenes": len(scenes),
                "auto_selected": chosen,
            },
        )
    except JobCanceled:
        db.update_part(project_id, part_idx, curation_status="pending")
        finish_canceled_job(job_id, project_id, "search_part")
    except Exception as exc:  # noqa: BLE001
        db.update_part(project_id, part_idx, curation_status="pending")
        db.fail_job(job_id, f"Falha na busca da parte {part_idx}", str(exc))


@app.post("/projects/{project_id}/parts/{part_idx}/search", responses=ERROR_RESPONSES)
def search_part(
    request: Request,
    project_id: int,
    part_idx: int,
    background_tasks: BackgroundTasks,
    csrf_token: Annotated[str, Form()] = "",
):
    user = require_user(request)
    verify_csrf(request, csrf_token)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    ensure_project_not_busy(project)
    if not has_visual_provider(user):
        raise HTTPException(400, MSG_NO_API_KEYS)
    parts = db.list_parts(project_id)
    if not parts:
        raise HTTPException(400, "Gere o mapa visual antes de buscar assets.")
    part = db.get_part(project_id, part_idx)
    if not part:
        raise HTTPException(404, "Parte nao encontrada.")
    # Fluxo estritamente sequencial: so libera esta parte se a anterior ja foi curada.
    if part_idx > 1:
        prev = db.get_part(project_id, part_idx - 1)
        if not prev or prev.get("curation_status") != "curated":
            raise HTTPException(400, f"Conclua a parte {part_idx - 1} antes de buscar a parte {part_idx}.")
    ensure_no_active_job(project_id, "search_part")
    job_id = db.create_job(
        user["id"], "search_part", project_id, f"Busca da parte {part_idx} na fila"
    )
    db.set_project_status(project_id, "searching")
    background_tasks.add_task(
        run_part_search_job,
        job_id,
        project_id,
        user["id"],
        part_idx,
        user.get("pexels_key", ""),
        user.get("pixabay_key", ""),
        user.get("groq_key", ""),
        user.get("groq_model") or groq_service.DEFAULT_MODEL,
        user.get("coverr_key", ""),
        user.get("nvidia_key", ""),
    )
    return RedirectResponse(f"/projects/{project_id}/review?part={part_idx}", status_code=303)


def run_part_auto_select_vision_job(
    job_id: int,
    project_id: int,
    user_id: int,
    part_idx: int,
    groq_key: str = "",
    groq_model: str = "",
    nvidia_key: str = "",
) -> None:
    """Analisa a visao (IA) das cenas da parte e ja re-seleciona o melhor take.

    Diferente da auto-selecao embutida na busca (que usa so a relevancia por
    keyword), aqui a IA de visao pontua os candidatos antes de escolher — mesma
    qualidade do fluxo dos videos curtos, mas restrito a uma parte.
    """
    try:
        check_job_canceled(job_id)
        db.update_job(job_id, status="running", message=f"Analisando visao da parte {part_idx}")
        project = db.get_project(project_id, user_id)
        if not project:
            raise RuntimeError(MSG_PROJECT_NOT_FOUND)
        config = project_config(project)

        def progress(done: int, total: int) -> None:
            check_job_canceled(job_id)
            db.update_job(job_id, status="running", message=f"Analisando visao ({done}/{total})")

        with api_usage.context(user_id=user_id, project_id=project_id, job_id=job_id, operation="part_vision"):
            analyzed, provider_name = analyze_pending_vision(
                project_id, user_id, groq_key, progress=progress,
                nvidia_key=nvidia_key, part_idx=part_idx,
            )
        check_job_canceled(job_id)
        db.update_job(job_id, status="running", message="Selecionando os melhores takes")
        with api_usage.context(user_id=user_id, project_id=project_id, job_id=job_id, operation="part_vision_auto_select"):
            chosen = auto_select_for_project(
                project_id, config, groq_key, groq_model, job_id=job_id,
                review_round=int(project.get("review_round") or 0), part_idx=part_idx,
            )
        db.update_part(project_id, part_idx, curation_status="reviewing")
        db.set_project_status(project_id, "reviewing")
        db.finish_job(
            job_id,
            f"Parte {part_idx}: {analyzed} assets analisados ({provider_name}), {chosen} selecionados",
            {"part": part_idx, "vision_analyzed": analyzed, "provider": provider_name, "auto_selected": chosen},
        )
    except JobCanceled:
        finish_canceled_job(job_id, project_id, "auto_select_vision")
    except Exception as exc:  # noqa: BLE001
        db.fail_job(job_id, f"Falha na selecao com visao da parte {part_idx}", str(exc))


@app.post("/projects/{project_id}/parts/{part_idx}/auto-select-vision", responses=ERROR_RESPONSES)
def part_auto_select_vision(
    request: Request,
    project_id: int,
    part_idx: int,
    background_tasks: BackgroundTasks,
    csrf_token: Annotated[str, Form()] = "",
):
    user = require_user(request)
    verify_csrf(request, csrf_token)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    ensure_project_not_busy(project)
    part = db.get_part(project_id, part_idx)
    if not part:
        raise HTTPException(404, "Parte nao encontrada.")
    assets_by_scene = db.list_assets_for_project(project_id)
    scenes = [s for s in db.list_scenes(project_id) if int(s.get("part") or 1) == part_idx]
    if not any(assets_by_scene.get(s["id"]) for s in scenes):
        raise HTTPException(400, "Busque os assets desta parte antes da selecao com visao.")
    ensure_no_active_job(project_id, "auto_select_vision")
    job_id = db.create_job(
        user["id"], "auto_select_vision", project_id, f"Selecao com visao da parte {part_idx} na fila"
    )
    db.set_project_status(project_id, "auto_selecting")
    background_tasks.add_task(
        run_part_auto_select_vision_job,
        job_id,
        project_id,
        user["id"],
        part_idx,
        user.get("groq_key", ""),
        user.get("groq_model") or groq_service.DEFAULT_MODEL,
        user.get("nvidia_key", ""),
    )
    return RedirectResponse(f"/projects/{project_id}/review?part={part_idx}", status_code=303)


def _write_full_curation_report(project: dict, project_id: int, assets_by_scene: dict) -> None:
    """Gera o relatorio completo de curadoria e marca o projeto como revisado."""
    all_scenes = db.list_scenes(project_id)
    chosen_by_scene: dict[int, dict] = {}
    for scene in all_scenes:
        accepted = next(
            (a for a in assets_by_scene.get(scene["id"], []) if a["state"] == "accepted"), None
        )
        if accepted:
            chosen_by_scene[scene["id"]] = accepted
    rejected_by_scene = {
        scene["id"]: [a for a in assets_by_scene.get(scene["id"], []) if a["state"] == "rejected"]
        for scene in all_scenes
    }
    report = packager.build_curation_report(
        project,
        all_scenes,
        chosen_by_scene,
        rejected_by_scene,
        review_round=int(project.get("review_round") or 0),
    )
    path = curation_report_path(project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report, encoding="utf-8")
    db.set_project_status(project_id, "reviewed")


@app.post("/projects/{project_id}/parts/{part_idx}/confirm", responses=ERROR_RESPONSES)
def confirm_part(
    request: Request,
    project_id: int,
    part_idx: int,
    csrf_token: Annotated[str, Form()] = "",
):
    user = require_user(request)
    verify_csrf(request, csrf_token)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    ensure_project_not_busy(project)
    part = db.get_part(project_id, part_idx)
    if not part:
        raise HTTPException(404, "Parte nao encontrada.")
    scenes = [s for s in db.list_scenes(project_id) if int(s.get("part") or 1) == part_idx]
    if not scenes:
        raise HTTPException(400, "Parte sem cenas.")
    # Sem trava de contagem: o usuario confirma com os takes que escolheu, mesmo
    # que faltem cenas. Cenas sem take aceito ficam sem b-roll (o avatar cobre),
    # igual ao comportamento das cenas avatar-only.
    assets_by_scene = db.list_assets_for_project(project_id)
    db.update_part(project_id, part_idx, curation_status="curated")

    # Se todas as partes ficaram curadas, gera o relatorio de curadoria completo
    # e marca o projeto como revisado (libera o Pacote).
    parts = db.list_parts(project_id)
    all_curated = all(p.get("curation_status") == "curated" for p in parts)
    if all_curated:
        _write_full_curation_report(project, project_id, assets_by_scene)
        return RedirectResponse(f"/projects/{project_id}", status_code=303)

    # Senao, avanca para a proxima parte ainda nao curada.
    next_part = next(
        (p["part_idx"] for p in parts if p.get("curation_status") != "curated"), part_idx
    )
    return RedirectResponse(f"/projects/{project_id}?part={next_part}#parts-panel", status_code=303)


@app.post("/projects/{project_id}/search", responses=ERROR_RESPONSES)
def search_all(
    request: Request,
    project_id: int,
    background_tasks: BackgroundTasks,
    csrf_token: Annotated[str, Form()] = "",
):
    user = require_user(request)
    verify_csrf(request, csrf_token)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    ensure_project_not_busy(project)
    if not has_visual_provider(user):
        raise HTTPException(400, MSG_NO_API_KEYS)
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
        user.get("coverr_key", ""),
        user.get("nvidia_key", ""),
    )
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/scenes/{scene_db_id}/search-more", responses=ERROR_RESPONSES)
def search_more(
    request: Request,
    scene_db_id: int,
    media: Annotated[str, Form()] = "all",
    keyword: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
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
    if not has_visual_provider(user):
        raise HTTPException(400, MSG_NO_API_KEYS)
    config = project_config(project)
    if media not in {"all", "video", "image"}:
        media = "all"
    max_w = resolution_width(config)
    existing = {a["download_url"] for a in db.list_assets(scene_db_id)}
    # busca manual: o usuario digitou uma keyword propria; senao usa as da cena
    custom = [k.strip() for k in str(keyword or "").split(",") if k.strip()][:5]
    search_keywords = custom or scene["keywords"]
    with api_usage.context(user_id=user["id"], project_id=project["id"], operation="search_more"):
        results = asset_search.search_scene(
            search_keywords,
            user["pexels_key"],
            user["pixabay_key"],
            max_w=max_w,
            per_keyword=config["per_keyword"] + 4,
            allow_images=True,
            seen_urls=existing,
            media=media,
            coverr_key=user.get("coverr_key", ""),
            extra_image_banks=True,
        )
    added = db.add_assets(scene_db_id, results)
    if added:
        mark_project_dirty(project["id"])
    return JSONResponse({"added": added, "media": media})


@app.post("/scenes/{scene_db_id}/regen-keywords", responses=ERROR_RESPONSES)
def regen_keywords(request: Request, scene_db_id: int, csrf_token: Annotated[str, Form()] = ""):
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
    with api_usage.context(user_id=user["id"], project_id=project["id"], operation="regenerate_keywords"):
        kws = groq_service.regenerate_keywords(
            scene.get("narration", ""),
            scene.get("visual_goal", ""),
            user["groq_key"],
            config["visual_style"],
            model=user.get("groq_model") or groq_service.DEFAULT_MODEL,
            language=config["script_language"],
        )
    db.update_scene_keywords(scene_db_id, kws)
    mark_project_dirty(project["id"])
    return JSONResponse({"keywords": kws})


@app.post("/scenes/{scene_db_id}/avatar-override", responses=ERROR_RESPONSES)
def set_avatar_override(
    request: Request,
    scene_db_id: int,
    mode: Annotated[str, Form()] = "auto",
    csrf_token: Annotated[str, Form()] = "",
):
    """Override manual por cena: 'no_avatar' (forca b-roll em tela cheia),
    'no_broll' (forca o avatar) ou 'auto' (decisao automatica)."""
    user = require_user(request)
    verify_csrf(request, csrf_token)
    scene = db.get_scene(scene_db_id)
    if not scene:
        raise HTTPException(404)
    project = db.get_project(scene["project_id"], user["id"])
    if not project:
        raise HTTPException(404)
    ensure_project_not_busy(project)
    value = {"no_avatar": 1, "no_broll": -1, "auto": 0}.get(mode)
    if value is None:
        raise HTTPException(400, "modo invalido (use auto, no_avatar ou no_broll)")
    db.update_scene_broll_override(scene_db_id, value)
    mark_project_dirty(project["id"])
    return JSONResponse({"mode": mode, "broll_override": value})


# ------------------------------------------------------------------
# Curadoria
# ------------------------------------------------------------------
def _revert_part_on_take_change(project: dict, owner: dict, asset_id: int) -> None:
    """Modo longo: mexer num take de uma parte ja curada exige re-confirmar a parte."""
    if not project_config(project).get("long_mode"):
        return
    asset = db.get_asset(asset_id)
    scene = db.get_scene(asset["scene_id"]) if asset else None
    if not scene:
        return
    p_idx = int(scene.get("part") or 1)
    part = db.get_part(owner["project_id"], p_idx)
    if part and part.get("curation_status") == "curated":
        db.update_part(owner["project_id"], p_idx, curation_status="reviewing")
        curation_report_path(owner["project_id"]).unlink(missing_ok=True)
        if project.get("status") == "reviewed":
            db.set_project_status(owner["project_id"], "reviewing")


def _project_status_after_take_change(project: dict, owner: dict, user: dict) -> Optional[str]:
    status = project.get("status")
    if status == "reviewed":
        # Changing a take after finishing makes the review report stale.
        db.set_project_status(owner["project_id"], "reviewing")
        curation_report_path(owner["project_id"]).unlink(missing_ok=True)
        return "reviewing"
    if status != "reviewing":
        # Outside review, invalidate any existing package as before.
        mark_project_dirty(owner["project_id"])
        curation_report_path(owner["project_id"]).unlink(missing_ok=True)
        fresh_project = db.get_project(owner["project_id"], user["id"])
        return (fresh_project or project).get("status", status)
    return status


@app.post("/assets/{asset_id}/state", responses=ERROR_RESPONSES)
def asset_state(
    request: Request,
    asset_id: int,
    state: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
    redirect: Annotated[str, Form()] = "",
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

    _revert_part_on_take_change(project, owner, asset_id)
    project_status = _project_status_after_take_change(project, owner, user)
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
        check_job_canceled(job_id)
        db.update_job(job_id, status="running", message="Selecionando os melhores takes")
        project = db.get_project(project_id, user_id)
        if not project:
            raise RuntimeError(MSG_PROJECT_NOT_FOUND)
        config = project_config(project)
        with api_usage.context(user_id=user_id, project_id=project_id, job_id=job_id, operation="auto_select"):
            chosen = auto_select_for_project(
                project_id, config, groq_key, groq_model, job_id=job_id,
                review_round=int(project.get("review_round") or 0),
            )
        if chosen <= 0:
            raise RuntimeError("Nenhuma cena com candidatos pendentes para selecionar.")
        db.set_project_status(project_id, "reviewing")
        db.finish_job(job_id, f"{chosen} takes selecionados", {"auto_selected": chosen})
    except JobCanceled:
        finish_canceled_job(job_id, project_id, "auto_select")
    except Exception as exc:  # noqa: BLE001
        db.set_project_status(project_id, "searched")
        db.fail_job(job_id, "Falha na selecao automatica", str(exc))


@app.post("/projects/{project_id}/auto-select", responses=ERROR_RESPONSES)
def auto_select_route(
    request: Request,
    project_id: int,
    background_tasks: BackgroundTasks,
    csrf_token: Annotated[str, Form()] = "",
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


# ------------------------------------------------------------------
# Analise de visao: pontua cada candidato comparando imagem x cena
# ------------------------------------------------------------------
def _build_vision_providers(groq_key: str, nvidia_key: str) -> list:
    """Provedores de visao disponiveis, em ordem de round-robin."""
    providers: list = []
    if groq_key:
        providers.append(vision.get_provider("groq", api_key=groq_key))
    if nvidia_key:
        providers.append(vision.get_provider("nvidia", api_key=nvidia_key))
    return providers


def _score_scene_assets(scene: dict, pend: list, provider, heuristic, config: dict, sheet_n: int):
    """Pontua as candidatas de uma cena (top-N no contact-sheet, resto na heuristica)."""
    # ranqueia pela RELEVANCIA textual: a IA olha primeiro os mais on-topic.
    ranked = sorted(pend, key=lambda a, scene=scene: scoring.keyword_relevance(scene, a), reverse=True)
    top, rest = ranked[:sheet_n], ranked[sheet_n:]
    results = provider.analyze_batch(top, scene, config)
    for asset in rest:  # candidatas fora do sheet: heuristica offline
        results[asset["id"]] = heuristic.analyze(asset, scene, config)
    return ranked, results


def _analyze_scene_pending_assets(
    scene: dict,
    pending_assets: list,
    provider,
    heuristic,
    config: dict,
    sheet_n: int,
) -> int:
    ranked, results = _score_scene_assets(scene, pending_assets, provider, heuristic, config, sheet_n)
    for asset in ranked:
        res = results.get(asset["id"]) or heuristic.analyze(asset, scene, config)
        db.set_asset_vision(
            asset["id"], res.score, res.verdict,
            "; ".join(res.reasons)[:300], res.flags, res.provider,
        )
    return len(ranked)


def analyze_pending_vision(
    project_id: int,
    user_id: int,
    groq_key: str = "",
    progress: Optional[callable] = None,
    nvidia_key: str = "",
    part_idx: Optional[int] = None,
) -> tuple[int, str]:
    """Analisa (e persiste) os assets ainda nao analisados do projeto.

    A IA olha a thumbnail (inclusive o poster de videos) dos candidatos mais
    relevantes de cada cena e julga se a imagem REALMENTE representa a cena.
    Usa TODOS os provedores de visao disponiveis em rodizio (round-robin) —
    Groq + NVIDIA — para distribuir a carga e driblar o rate limit de cada um,
    cobrindo mais candidatos por cena. Cai na heuristica offline para o restante
    ou quando nao ha chave. Idempotente: so toca assets com vision_analyzed=0.
    Retorna (quantos_analisados, nome_do_provedor_principal).
    """
    project = db.get_project(project_id, user_id)
    if not project:
        raise RuntimeError(MSG_PROJECT_NOT_FOUND)
    config = project_config(project)
    scenes = db.list_scenes(project_id)
    if part_idx is not None:
        scenes = [s for s in scenes if int(s.get("part") or 1) == part_idx]
    # Tema global do video ancora o julgamento da visao (rejeita fora-do-tema).
    video_theme = str(config.get("video_theme") or "").strip()
    if video_theme:
        for s in scenes:
            s["video_theme"] = video_theme
    assets_by_scene = db.list_assets_for_project(project_id)

    providers = _build_vision_providers(groq_key, nvidia_key)
    heuristic = vision.HeuristicVisionProvider()
    primary_name = providers[0].name if providers else heuristic.name
    # Contact-sheet: a IA julga as TOP-N candidatas de cada cena numa unica
    # chamada (multi-imagem), comparando-as entre si dentro do tema do video.
    # Isso da ~1 chamada/cena (em vez de N) -> cabe no rate limit -> mais cenas
    # vetadas de verdade. O resto da cena fica na heuristica offline.
    sheet_n = max(2, VISION_SHEET_N)

    total_pending = sum(
        1 for scene in scenes
        for a in assets_by_scene.get(scene["id"], []) if not a.get("vision_analyzed")
    )
    if total_pending == 0:
        return 0, primary_name

    analyzed = 0
    rr = 0  # round-robin entre provedores, por cena
    for scene in scenes:
        pend = [a for a in assets_by_scene.get(scene["id"], []) if not a.get("vision_analyzed")]
        if not pend:
            continue
        provider = providers[rr % len(providers)] if providers else heuristic
        rr += 1
        before = analyzed
        analyzed += _analyze_scene_pending_assets(scene, pend, provider, heuristic, config, sheet_n)
        if progress and analyzed // 10 > before // 10:
            progress(analyzed, total_pending)
    return analyzed, primary_name


def run_vision_job(
    job_id: int,
    project_id: int,
    user_id: int,
    groq_key: str,
    nvidia_key: str = "",
) -> None:
    """Job dedicado de analise de visao (botao 'Analisar visao')."""
    try:
        check_job_canceled(job_id)
        db.update_job(job_id, status="running", message="Analisando assets")

        def progress(done: int, total: int) -> None:
            check_job_canceled(job_id)
            db.update_job(job_id, status="running", message=f"Analisando assets ({done}/{total})")

        with api_usage.context(user_id=user_id, project_id=project_id, job_id=job_id, operation="vision"):
            analyzed, provider_name = analyze_pending_vision(
                project_id, user_id, groq_key, progress=progress, nvidia_key=nvidia_key
            )
        if analyzed == 0:
            db.finish_job(job_id, "Nada novo para analisar", {"analyzed": 0, "provider": provider_name})
            return
        db.finish_job(
            job_id,
            f"{analyzed} assets analisados ({provider_name})",
            {"analyzed": analyzed, "provider": provider_name},
        )
    except JobCanceled:
        finish_canceled_job(job_id, project_id, "vision")
    except Exception as exc:  # noqa: BLE001
        db.fail_job(job_id, "Falha na analise de visao", str(exc))


@app.post("/projects/{project_id}/analyze-vision", responses=ERROR_RESPONSES)
def analyze_vision_route(
    request: Request,
    project_id: int,
    background_tasks: BackgroundTasks,
    csrf_token: Annotated[str, Form()] = "",
):
    user = require_user(request)
    verify_csrf(request, csrf_token)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    ensure_project_not_busy(project)
    scenes = db.list_scenes(project_id)
    assets_by_scene = db.list_assets_for_project(project_id)
    if not any(assets_by_scene.get(scene["id"]) for scene in scenes):
        raise HTTPException(400, "Busque assets antes de analisar a visao.")
    ensure_no_active_job(project_id, "vision")
    job_id = db.create_job(user["id"], "vision", project_id, "Analise de visao na fila")
    background_tasks.add_task(
        run_vision_job,
        job_id,
        project_id,
        user["id"],
        user.get("groq_key", ""),
        user.get("nvidia_key", ""),
    )
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.get("/projects/{project_id}/review", response_class=HTMLResponse, responses=ERROR_RESPONSES)
def review_page(request: Request, project_id: int, part: Optional[int] = None):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    config = project_config(project)
    scenes = db.list_scenes(project_id)
    parts = db.list_parts(project_id) if config.get("long_mode") else []
    part_idx = None
    if parts:
        valid = {p["part_idx"] for p in parts}
        # parte explicita ou a primeira ainda nao curada (driver sequencial)
        if part in valid:
            part_idx = part
        else:
            part_idx = next(
                (p["part_idx"] for p in parts if p.get("curation_status") != "curated"),
                parts[0]["part_idx"],
            )
        scenes = [s for s in scenes if int(s.get("part") or 1) == part_idx]
    assets_by_scene = db.list_assets_for_project(project_id)
    review_scenes = []
    for scene in scenes:
        assets = annotate_assets_with_vision(scene, assets_by_scene.get(scene["id"], []), config)
        chosen = next((a for a in assets if a["state"] in CHOSEN_ASSET_STATES), None)
        scene["chosen"] = chosen
        scene["rejected_count"] = sum(1 for a in assets if a["state"] == "rejected")
        review_scenes.append(scene)
    curation_stats = annotate_broll_requirements(review_scenes, config)
    return render_template(
        request,
        "review.html",
        {
            "user": user,
            "project": project,
            "config": config,
            "scenes": review_scenes,
            "accepted": curation_stats["accepted"],
            "pending_review": curation_stats["pending"],
            "rejected_waiting": curation_stats["waiting"],
            "total": curation_stats["required"],
            "scene_count": len(review_scenes),
            "avatar_only_count": curation_stats["avatar_only"],
            "review_round": int(project.get("review_round") or 0),
            "has_report": project.get("status") in {"reviewed", "packaging", "packaged", "package_failed"}
            and curation_report_path(project_id).exists(),
            "part_idx": part_idx,
            "total_parts": len(parts),
            "part_curated": part_idx is not None
            and any(p["part_idx"] == part_idx and p.get("curation_status") == "curated" for p in parts),
        },
    )


def _research_one_scene(
    scene: dict,
    label: str,
    job_id: int,
    user_id: int,
    keys: dict,
    config: dict,
    max_w: int,
    assets_by_scene: dict,
) -> int:
    """Re-busca de uma cena rejeitada: novas keywords + nova busca. Retorna adicionados."""
    db.update_job(job_id, status="running", message=label)
    # keywords novas para fugir dos resultados que o usuario rejeitou
    with api_usage.context(user_id=user_id, project_id=scene.get("project_id"), job_id=job_id, operation="research_keywords"):
        kws = groq_service.regenerate_keywords(
            scene.get("narration", ""),
            scene.get("visual_goal", ""),
            keys["groq"],
            config["visual_style"],
            model=keys["groq_model"] or groq_service.DEFAULT_MODEL,
            language=config["script_language"],
        )
    check_job_canceled(job_id)
    if kws:
        roles = scoring.assign_roles(kws)
        db.update_scene_keywords(scene["id"], kws, roles)
        scene["keywords"] = kws
        scene["keyword_roles"] = roles
    existing = {a["download_url"] for a in assets_by_scene.get(scene["id"], [])}
    with api_usage.context(user_id=user_id, project_id=scene.get("project_id"), job_id=job_id, operation="research_assets"):
        results = asset_search.search_scene(
            scene["keywords"],
            keys["pexels"],
            keys["pixabay"],
            max_w=max_w,
            per_keyword=config["per_keyword"] + 4,
            allow_images=True,
            seen_urls=existing,
            coverr_key=keys["coverr"],
            extra_image_banks=True,
        )
    check_job_canceled(job_id)
    return db.add_assets(scene["id"], results)


def run_research_job(
    job_id: int,
    project_id: int,
    user_id: int,
    pexels_key: str,
    pixabay_key: str,
    groq_key: str,
    groq_model: str,
    coverr_key: str = "",
    nvidia_key: str = "",
    part_idx: Optional[int] = None,
) -> None:
    try:
        check_job_canceled(job_id)
        db.update_job(job_id, status="running", message="Buscando takes melhores para as cenas rejeitadas")
        project = db.get_project(project_id, user_id)
        if not project:
            raise RuntimeError(MSG_PROJECT_NOT_FOUND)
        config = project_config(project)
        max_w = resolution_width(config)
        new_round = int(project.get("review_round") or 0) + 1
        db.set_project_review_round(project_id, new_round)

        scenes = db.list_scenes(project_id)
        if part_idx is not None:
            scenes = [s for s in scenes if int(s.get("part") or 1) == part_idx]
        assets_by_scene = db.list_assets_for_project(project_id)
        targets = [
            s for s in scenes
            if not any(a["state"] in CHOSEN_ASSET_STATES for a in assets_by_scene.get(s["id"], []))
        ]
        if not targets:
            raise RuntimeError("Nenhuma cena rejeitada aguardando nova busca.")

        keys = {"pexels": pexels_key, "pixabay": pixabay_key, "groq": groq_key,
                "groq_model": groq_model, "coverr": coverr_key}
        added_total = 0
        for i, scene in enumerate(targets, 1):
            check_job_canceled(job_id)
            label = f"Nova busca {i}/{len(targets)}: {scene['scene_id']}"
            added_total += _research_one_scene(scene, label, job_id, user_id, keys, config, max_w, assets_by_scene)

        # pontua os novos takes antes de escolher, para a selecao usar a visao.
        # No fluxo por-parte pulamos a visao em massa (e sob demanda e ficaria
        # cara reanalisando todo o projeto) -> a re-busca da parte fica rapida.
        if part_idx is None:
            try:
                check_job_canceled(job_id)
                db.update_job(job_id, status="running", message="Analisando visao dos novos takes")
                with api_usage.context(user_id=user_id, project_id=project_id, job_id=job_id, operation="research_vision"):
                    analyze_pending_vision(
                        project_id,
                        user_id,
                        groq_key,
                        progress=lambda done, total: check_job_canceled(job_id),
                        nvidia_key=nvidia_key,
                    )
            except Exception as vexc:  # noqa: BLE001
                if isinstance(vexc, JobCanceled):
                    raise
                logger.warning("Analise de visao na re-busca falhou: %s", vexc)

        check_job_canceled(job_id)
        db.update_job(job_id, status="running", message="Selecionando os melhores takes novos")
        with api_usage.context(user_id=user_id, project_id=project_id, job_id=job_id, operation="research_auto_select"):
            chosen = auto_select_for_project(
                project_id, config, groq_key, groq_model, job_id=job_id,
                review_round=new_round, part_idx=part_idx,
            )
        db.set_project_status(project_id, "reviewing")
        db.finish_job(
            job_id,
            f"Rodada {new_round}: {added_total} takes novos, {chosen} selecionados",
            {"round": new_round, "added": added_total, "selected": chosen, "scenes": len(targets)},
        )
    except JobCanceled:
        finish_canceled_job(job_id, project_id, "research_rejected")
    except Exception as exc:  # noqa: BLE001
        db.set_project_status(project_id, "reviewing")
        db.fail_job(job_id, "Falha na nova busca", str(exc))


@app.post("/projects/{project_id}/research-rejected", responses=ERROR_RESPONSES)
def research_rejected(
    request: Request,
    project_id: int,
    background_tasks: BackgroundTasks,
    csrf_token: Annotated[str, Form()] = "",
    part: Annotated[str, Form()] = "",
):
    user = require_user(request)
    verify_csrf(request, csrf_token)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    ensure_project_not_busy(project)
    if not has_visual_provider(user):
        raise HTTPException(400, MSG_NO_API_KEYS)
    part_idx: Optional[int] = None
    if part.strip():
        try:
            part_idx = int(part)
        except ValueError:
            part_idx = None
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
        user.get("coverr_key", ""),
        user.get("nvidia_key", ""),
        part_idx,
    )
    suffix = f"?part={part_idx}" if part_idx is not None else ""
    return RedirectResponse(f"/projects/{project_id}/review{suffix}", status_code=303)


@app.post("/projects/{project_id}/finish-review", responses=ERROR_RESPONSES)
def finish_review(request: Request, project_id: int, csrf_token: Annotated[str, Form()] = ""):
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
    # cenas avatar-only nao precisam de take aceito (nao levam b-roll)
    broll_required = {sid for sid, on in scene_broll_flags(scenes, project_config(project)).items() if on}
    assets_by_scene = db.list_assets_for_project(project_id)
    chosen_by_scene: dict[int, dict] = {}
    for scene in scenes:
        assets = assets_by_scene.get(scene["id"], [])
        accepted = next((a for a in assets if a["state"] == "accepted"), None)
        if accepted:
            chosen_by_scene[scene["id"]] = accepted
    required_scene_db_ids = {s["id"] for s in scenes if s["scene_id"] in broll_required}
    if not broll_required and not chosen_by_scene:
        raise HTTPException(400, "O plano atual nao tem cenas de b-roll para revisar.")
    if broll_required and not any(scene_id in chosen_by_scene for scene_id in required_scene_db_ids):
        raise HTTPException(400, "Aceite ao menos um take antes de concluir a revisao.")

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


@app.get("/projects/{project_id}/curation-report", responses=ERROR_RESPONSES)
def download_curation_report(request: Request, project_id: int):
    user = require_user(request)
    if not db.get_project(project_id, user["id"]):
        raise HTTPException(404)
    path = curation_report_path(project_id)
    if not path.exists():
        raise HTTPException(404, "Relatorio de curadoria ainda nao gerado. Conclua a revisao primeiro.")
    return FileResponse(path, filename=path.name, media_type="text/markdown")


@app.get("/projects/{project_id}/preview", response_class=HTMLResponse, responses=ERROR_RESPONSES)
def preview_page(request: Request, project_id: int):
    """Folha de contato: o take escolhido de cada cena lado a lado com a narracao.

    Permite revisar visualmente as escolhas e pegar erros ANTES de gastar render.
    """
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    config = project_config(project)
    scenes = db.list_scenes(project_id)
    chosen_rows = db.list_assets_by_state(project_id, CHOSEN_ASSET_STATES)
    chosen_by_scene = {row["scene_id"]: row for row in chosen_rows}

    # cenas avatar-only nao tem (nem precisam de) take: nao contam como "faltando"
    broll_map = scene_broll_flags(scenes, config)
    cards = []
    missing = low_rel = discard = broll_total = 0
    for scene in scenes:
        is_broll = broll_map.get(scene["scene_id"], True)
        asset = chosen_by_scene.get(scene["id"])
        annotated = annotate_assets_with_vision(scene, [asset], config)[0] if asset else None
        if not is_broll:
            cards.append({"scene": scene, "asset": annotated, "avatar_only": True})
            continue
        broll_total += 1
        if not annotated:
            missing += 1
        else:
            if annotated.get("low_relevance"):
                low_rel += 1
            if annotated.get("vision_verdict") == "descartar":
                discard += 1
        cards.append({"scene": scene, "asset": annotated, "avatar_only": False})

    return render_template(
        request,
        "preview.html",
        {
            "user": user,
            "project": project,
            "config": config,
            "cards": cards,
            "total": broll_total,
            "with_take": broll_total - missing,
            "missing": missing,
            "low_relevance": low_rel,
            "discard": discard,
        },
    )


# ------------------------------------------------------------------
# Imagens geradas por IA (Puter.js no browser -> salvas como asset)
# ------------------------------------------------------------------
GENERATED_DIR_NAME = "generated"
MAX_GENERATED_UPLOAD_MB = 15
_GENERATED_NAME_RE = re.compile(r"^gen_[0-9a-f]{32}\.(png|jpg|webp)$")
_GENERATED_MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}


def project_generated_dir(project_id: int) -> Path:
    return project_work_dir(project_id) / GENERATED_DIR_NAME


# Detecção de formato/dimensões de imagem vive em services/image_probe.py
# (importado como _image_kind_and_size no topo).


@app.post("/scenes/{scene_db_id}/generated-image", responses=ERROR_RESPONSES)
async def save_generated_image(
    request: Request,
    scene_db_id: int,
    image: Annotated[UploadFile, File()],
    prompt: Annotated[str, Form()] = "",
    width: Annotated[str, Form()] = "0",
    height: Annotated[str, Form()] = "0",
    csrf_token: Annotated[str, Form()] = "",
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


@app.get("/projects/{project_id}/generated/{filename}", responses=ERROR_RESPONSES)
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
@app.post("/projects/{project_id}/upload-media", responses=ERROR_RESPONSES)
async def upload_media(
    request: Request,
    project_id: int,
    kind: Annotated[str, Form()],
    media: Annotated[UploadFile, File()],
    csrf_token: Annotated[str, Form()] = "",
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


@app.post("/projects/{project_id}/remove-media", responses=ERROR_RESPONSES)
def remove_media(
    request: Request,
    project_id: int,
    kind: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
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


def _slice_avatar(avatar_path: Path, start: float, duration: float, out_path: Path) -> Path:
    """Corta o avatar no intervalo da parte (sem audio) para servir de base no render.

    No modo longo cada parte e um render avatar-base proprio; o avatar e um video
    unico do apresentador, entao fatiamos o trecho [start, start+duration] e
    deixamos mudo (a narracao completa entra so na concatenacao final).
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-ss", "%.3f" % max(start, 0.0),
        "-i", str(avatar_path),
        "-t", "%.3f" % max(duration, 0.1),
        "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        str(out_path),
    ]
    res = subprocess.run(cmd, capture_output=True, timeout=1800)
    if res.returncode != 0 or not out_path.exists() or out_path.stat().st_size == 0:
        raise RuntimeError(
            "falha ao cortar o avatar da parte: " + res.stderr.decode(errors="replace")[:300]
        )
    return out_path


class _PackageCtx:
    """Contexto compartilhado do job de empacotamento (evita explosao de parametros)."""
    def __init__(self, job_id, project_id, project, config, scenes, selected_by_scene, rejected_payload):
        self.job_id = job_id
        self.project_id = project_id
        self.project = project
        self.config = config
        self.scenes = scenes
        self.selected_by_scene = selected_by_scene
        self.rejected_payload = rejected_payload


def _validate_package_selection(scenes, selected_by_scene, broll_required, required_scene_db_ids) -> None:
    if not broll_required:
        if selected_by_scene:
            return
        raise RuntimeError("Selecao vazia; escolha ao menos um asset antes de gerar pacote.")
    if not any(scene_id in selected_by_scene for scene_id in required_scene_db_ids):
        raise RuntimeError("Selecao vazia; escolha ao menos um asset antes de gerar pacote.")


def _fallback_unselected_brolls_to_avatar(scenes: list[dict], selected_by_scene: dict[int, dict]) -> None:
    """Cenas b-roll sem take escolhido viram avatar no pacote/render."""
    for scene in scenes:
        if scene.get("broll") and scene["id"] not in selected_by_scene:
            scene["broll"] = False


def _build_part_zip(ctx: "_PackageCtx", part: dict, parts_count: int, avatar_input) -> Optional[str]:
    """Monta o ZIP de uma parte (modo longo): fatia o avatar, gera edit_plan e zipa."""
    idx = part["part_idx"]
    check_job_canceled(ctx.job_id)
    db.update_job(ctx.job_id, status="running", message=f"Baixando assets da parte {idx}/{parts_count}")
    part_scenes = [s for s in ctx.scenes if int(s.get("part") or 1) == idx]
    if not part_scenes:
        db.update_part(ctx.project_id, idx, status="error", error="parte sem cenas")
        return None
    # avatar fatiado para o intervalo desta parte (vira a base do render)
    extras: list[Path] = []
    avatar_name = ""
    if avatar_input and avatar_input.exists():
        p_start = min(float(s.get("start_time") or 0) for s in part_scenes)
        p_end = max(float(s.get("end_time") or 0) for s in part_scenes)
        slice_out = part_dir(ctx.project_id, idx) / "avatar.mp4"
        _slice_avatar(avatar_input, p_start, p_end - p_start, slice_out)
        extras.append(slice_out)
        avatar_name = slice_out.name
    check_job_canceled(ctx.job_id)
    rebased = _rebase_scenes(part_scenes)
    part_plan = edit_plan.build_edit_plan(
        ctx.project, ctx.config, rebased,
        narration_file="",      # narracao entra so no concat final
        avatar_file=avatar_name,  # avatar-base por parte
    )
    zip_path = packager.build_zip(
        project=ctx.project,
        config=ctx.config,
        scenes=rebased,
        selected_by_scene=ctx.selected_by_scene,
        rejected_assets=ctx.rejected_payload,
        work_dir=part_dir(ctx.project_id, idx),
        max_download_mb=ctx.config["max_download_mb"],
        edit_plan=part_plan,
        extra_files=extras,
        zip_basename=f"{ctx.project['name']}_pt{idx:02d}",
    )
    check_job_canceled(ctx.job_id)
    db.update_part(
        ctx.project_id, idx,
        zip_name=zip_path.name, status="zipped",
        error="", video_path="", dataset_slug="", kernel_slug="",
    )
    return zip_path.name


def _package_long_mode(ctx: "_PackageCtx") -> None:
    """Um ZIP por parte: cada parte e um render avatar-base proprio (partes mudas)."""
    parts = db.list_parts(ctx.project_id)
    if not parts:
        raise RuntimeError("Projeto longo sem partes; gere o mapa visual novamente.")
    if parts_dir(ctx.project_id).exists():
        shutil.rmtree(parts_dir(ctx.project_id), ignore_errors=True)
    avatar_input = find_input_media(ctx.project_id, "avatar")
    if avatar_input and not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg necessario no servidor para fatiar o avatar por parte (modo longo).")
    zip_names = [
        name for part in parts
        if (name := _build_part_zip(ctx, part, len(parts), avatar_input))
    ]
    if not zip_names:
        raise RuntimeError("Nenhuma parte gerou pacote.")
    db.set_project_status(ctx.project_id, "packaged")
    db.clear_kaggle_job(ctx.project_id)
    db.finish_job(
        ctx.job_id,
        f"{len(zip_names)} pacotes prontos (1 por parte, avatar-base)",
        {"parts": len(zip_names), "zips": zip_names, "scenes": len(ctx.scenes)},
    )


def _package_single_mode(ctx: "_PackageCtx") -> None:
    project_work = project_work_dir(ctx.project_id)
    narration_file = find_input_media(ctx.project_id, "narration")
    avatar_file = find_input_media(ctx.project_id, "avatar")
    check_job_canceled(ctx.job_id)
    plan = edit_plan.build_edit_plan(
        ctx.project,
        ctx.config,
        ctx.scenes,
        narration_file=narration_file.name if narration_file else "",
        avatar_file=avatar_file.name if avatar_file else "",
    )
    # copia local do plano: permite revisar a edicao antes de enviar ao Kaggle
    project_work.mkdir(parents=True, exist_ok=True)
    (project_work / EDIT_PLAN_FILENAME).write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    check_job_canceled(ctx.job_id)
    zip_path = packager.build_zip(
        project=ctx.project,
        config=ctx.config,
        scenes=ctx.scenes,
        selected_by_scene=ctx.selected_by_scene,
        rejected_assets=ctx.rejected_payload,
        work_dir=project_work,
        max_download_mb=ctx.config["max_download_mb"],
        edit_plan=plan,
        extra_files=[
            f for f in (narration_file, avatar_file, curation_report_path(ctx.project_id)) if f and f.exists()
        ],
    )
    check_job_canceled(ctx.job_id)
    db.set_project_status(ctx.project_id, "packaged")
    db.clear_kaggle_job(ctx.project_id)
    db.finish_job(
        ctx.job_id,
        "Pacote ZIP pronto",
        {"zip": zip_path.name, "scenes": len(ctx.scenes), "selected": len(ctx.selected_by_scene)},
    )


def run_package_job(
    job_id: int,
    project_id: int,
    user_id: int,
) -> None:
    try:
        check_job_canceled(job_id)
        db.update_job(job_id, status="running", message="Gerando edit_plan e baixando assets")
        project = db.get_project(project_id, user_id)
        if not project:
            raise RuntimeError(MSG_PROJECT_NOT_FOUND)
        config = project_config(project)
        scenes = db.list_scenes(project_id)
        # decisao de b-roll (mesma da busca/render): anexa em cada cena para o
        # edit_plan, o guia e o montador saberem quais cenas sao avatar-only.
        broll_map = scene_broll_flags(scenes, config)
        for s in scenes:
            s["broll"] = bool(broll_map.get(s["scene_id"], True))
        broll_required = {s["scene_id"] for s in scenes if s["broll"]}
        required_scene_db_ids = {s["id"] for s in scenes if s["broll"]}
        selected_rows = db.list_assets_by_state(project_id, CHOSEN_ASSET_STATES)
        selected_by_scene = {row["scene_id"]: row for row in selected_rows}
        rejected = db.list_assets_by_state(project_id, ["rejected"])
        _validate_package_selection(scenes, selected_by_scene, broll_required, required_scene_db_ids)
        _fallback_unselected_brolls_to_avatar(scenes, selected_by_scene)
        remove_project_artifacts(project_id)
        rejected_payload = [
            {"scene_id": r["scene_code"], "source": r["source"], "url": r["download_url"], "keyword": r["keyword"]}
            for r in rejected
        ]

        ctx = _PackageCtx(job_id, project_id, project, config, scenes, selected_by_scene, rejected_payload)
        if config.get("long_mode"):
            _package_long_mode(ctx)
        else:
            _package_single_mode(ctx)
    except JobCanceled:
        finish_canceled_job(job_id, project_id, "package")
    except Exception as exc:  # noqa: BLE001
        db.set_project_status(project_id, "package_failed")
        db.fail_job(job_id, "Falha ao gerar pacote", str(exc))


@app.post("/projects/{project_id}/package", responses=ERROR_RESPONSES)
def package(
    request: Request,
    project_id: int,
    background_tasks: BackgroundTasks,
    csrf_token: Annotated[str, Form()] = "",
):
    user = require_user(request)
    verify_csrf(request, csrf_token)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    ensure_project_not_busy(project)
    if project_config(project).get("long_mode"):
        parts = db.list_parts(project_id)
        not_curated = [p["part_idx"] for p in parts if p.get("curation_status") != "curated"]
        if not parts or not_curated:
            preview = ", ".join(str(i) for i in not_curated[:8])
            raise HTTPException(
                400,
                f"Conclua a curadoria de todas as partes antes de gerar o pacote. Faltando: {preview}",
            )
    scenes = db.list_scenes(project_id)
    broll_required = {sid for sid, on in scene_broll_flags(scenes, project_config(project)).items() if on}
    selected_rows = db.list_assets_by_state(project_id, CHOSEN_ASSET_STATES)
    selected_by_scene = {row["scene_id"]: row for row in selected_rows}
    required_scene_db_ids = {s["id"] for s in scenes if s["scene_id"] in broll_required}

    if not broll_required and not selected_by_scene:
        raise HTTPException(400, "O plano atual nao tem cenas de b-roll para empacotar.")
    if broll_required and not any(scene_id in selected_by_scene for scene_id in required_scene_db_ids):
        raise HTTPException(400, "Selecione ao menos um asset antes de gerar o pacote.")
    ensure_no_active_job(project_id, "package")
    job_id = db.create_job(user["id"], "package", project_id, "Preparando pacote ZIP")
    db.set_project_status(project_id, "packaging")
    background_tasks.add_task(
        run_package_job,
        job_id,
        project_id,
        user["id"],
    )
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.get("/projects/{project_id}/download-zip", responses=ERROR_RESPONSES)
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


@app.get("/projects/{project_id}/edit-plan", responses=ERROR_RESPONSES)
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
@app.post("/projects/{project_id}/send-to-kaggle", responses=ERROR_RESPONSES)
def send_to_kaggle(
    request: Request,
    project_id: int,
    background_tasks: BackgroundTasks,
    csrf_token: Annotated[str, Form()] = "",
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
        user["id"],
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


def _poll_part_render(job_id: int, k_slug: str, username: str, token: str) -> tuple[str, str]:
    """Aguarda o kernel da parte terminar. Retorna (status_final, detalhe_erro)."""
    deadline = time.time() + PART_RENDER_TIMEOUT
    while time.time() < deadline:
        time.sleep(PART_POLL_SECONDS)
        check_job_canceled(job_id)
        info = kaggle_service.get_status(k_slug, username, token)
        status = (info.get("status") or "").lower()
        if status == "complete":
            return "complete", ""
        if status == "error":
            return "error", str(info.get("error") or "")[:400]
    return "timeout", ""


def _upload_and_render_part(
    job_id: int,
    project_id: int,
    user_id: int,
    project: dict,
    part: dict,
    total: int,
    username: str,
    token: str,
) -> None:
    """Sobe o ZIP da parte, dispara o kernel, aguarda e baixa o MP4. Raise em falha."""
    idx = part["part_idx"]
    label = f"parte {idx}/{total}"
    zip_path = part_dir(project_id, idx) / (part.get("zip_name") or "")
    if not part.get("zip_name") or not zip_path.exists():
        raise RuntimeError("ZIP da parte nao encontrado; gere os pacotes novamente.")
    db.update_job(job_id, status="running", message=f"Enviando {label} ao Kaggle")
    db.update_part(project_id, idx, status="uploading", error="")
    part_name = f"{project['name']} pt{idx:02d}"
    check_job_canceled(job_id)
    with api_usage.context(user_id=user_id, project_id=project_id, job_id=job_id, operation="kaggle_part"):
        ds_slug = kaggle_service.upload_dataset(zip_path, part_name, username, token, project_id=project_id)
    check_job_canceled(job_id)
    with api_usage.context(user_id=user_id, project_id=project_id, job_id=job_id, operation="kaggle_part"):
        k_slug, _push = kaggle_service.push_kernel(ds_slug, part_name, username, token, project_id=project_id)
    db.update_part(project_id, idx, dataset_slug=ds_slug, kernel_slug=k_slug, status="running")
    check_job_canceled(job_id)

    db.update_job(job_id, status="running", message=f"Renderizando {label} no Kaggle")
    with api_usage.context(user_id=user_id, project_id=project_id, job_id=job_id, operation="kaggle_part_poll"):
        final_status, error_detail = _poll_part_render(job_id, k_slug, username, token)
    if final_status == "complete":
        out_dir = part_dir(project_id, idx) / "kaggle_output"
        with api_usage.context(user_id=user_id, project_id=project_id, job_id=job_id, operation="kaggle_part_pull"):
            video = kaggle_service.pull_output_video(k_slug, username, token, out_dir)
        if not video:
            raise RuntimeError("Render concluiu mas o MP4 nao foi encontrado no output.")
        db.update_part(project_id, idx, status="done", video_path=str(video), error="")
        return
    if final_status == "error":
        raise RuntimeError(error_detail or "kernel falhou")
    raise RuntimeError(f"timeout apos {PART_RENDER_TIMEOUT // 60} min")


def run_kaggle_parts_job(
    job_id: int,
    project_id: int,
    user_id: int,
    username: str,
    token: str,
) -> None:
    try:
        check_job_canceled(job_id)
        project = db.get_project(project_id, user_id)
        if not project:
            raise RuntimeError(MSG_PROJECT_NOT_FOUND)
        parts = [p for p in db.list_parts(project_id) if p["status"] != "done"]
        total = len(db.list_parts(project_id))
        if not parts:
            raise RuntimeError("Todas as partes ja foram renderizadas.")
        ok = 0
        failed = 0
        for part in parts:
            check_job_canceled(job_id)
            idx = part["part_idx"]
            try:
                _upload_and_render_part(job_id, project_id, user_id, project, part, total, username, token)
                ok += 1
            except JobCanceled:
                db.update_part(project_id, idx, status="error", error="render interrompido pelo usuario")
                raise
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
    except JobCanceled:
        finish_canceled_job(job_id, project_id, "kaggle_parts")
    except Exception as exc:  # noqa: BLE001
        db.fail_job(job_id, "Falha no render por partes", str(exc))


@app.post("/projects/{project_id}/render-parts", responses=ERROR_RESPONSES)
def render_parts(
    request: Request,
    project_id: int,
    background_tasks: BackgroundTasks,
    csrf_token: Annotated[str, Form()] = "",
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


def _run_ffmpeg(args: list[str], timeout: int = 1800) -> subprocess.CompletedProcess:
    return subprocess.run(["ffmpeg", "-y", *args], capture_output=True, timeout=timeout)


def _collect_part_videos(job_id: int, parts: list[dict]) -> list[Path]:
    """Coleta os MP4s renderizados das partes em ordem; raise se alguma faltar."""
    videos: list[Path] = []
    for part in sorted(parts, key=lambda p: p["part_idx"]):
        check_job_canceled(job_id)
        video = Path(part.get("video_path") or "")
        if part["status"] != "done" or not video.exists():
            raise RuntimeError(f"Parte {part['part_idx']} sem video renderizado.")
        videos.append(video)
    return videos


def _concat_part_videos(job_id: int, concat_list: Path, base_out: Path) -> None:
    """Stream copy primeiro (mesmo preset do montador); re-encode como fallback."""
    check_job_canceled(job_id)
    result = _run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", str(base_out)])
    if result.returncode != 0 or not base_out.exists() or base_out.stat().st_size == 0:
        check_job_canceled(job_id)
        db.update_job(job_id, status="running", message="Stream copy falhou; re-encodando")
        result = _run_ffmpeg(
            ["-f", "concat", "-safe", "0", "-i", str(concat_list),
             "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-an", str(base_out)],
            timeout=3600,
        )
        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg concat falhou: {result.stderr.decode(errors='replace')[:400]}")


def _mux_narration(job_id: int, project_id: int, base_out: Path, out_dir: Path) -> str:
    """Adiciona a narracao ao master. Retorna o nome do master ou '' se nao houver/falhar."""
    narration = find_input_media(project_id, "narration")
    if not narration:
        return ""
    check_job_canceled(job_id)
    db.update_job(job_id, status="running", message="Adicionando narracao ao master")
    master_out = out_dir / kaggle_service.MASTER_VIDEO_NAME
    result = _run_ffmpeg(
        ["-i", str(base_out), "-i", str(narration),
         "-c:v", "copy", "-c:a", "aac", "-map", "0:v:0", "-map", "1:a:0",
         "-shortest", str(master_out)],
    )
    if result.returncode == 0 and master_out.exists() and master_out.stat().st_size > 0:
        return master_out.name
    logger.warning("mux de narracao falhou: %s", result.stderr.decode(errors="replace")[:300])
    return ""


def run_concat_job(job_id: int, project_id: int, user_id: int) -> None:
    try:
        check_job_canceled(job_id)
        db.update_job(job_id, status="running", message="Concatenando partes")
        project = db.get_project(project_id, user_id)
        if not project:
            raise RuntimeError(MSG_PROJECT_NOT_FOUND)
        parts = db.list_parts(project_id)
        if not parts:
            raise RuntimeError("Projeto sem partes.")
        videos = _collect_part_videos(job_id, parts)
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

        _concat_part_videos(job_id, concat_list, base_out)
        master_name = _mux_narration(job_id, project_id, base_out, out_dir)

        check_job_canceled(job_id)
        concat_list.unlink(missing_ok=True)
        db.finish_job(
            job_id,
            "Video final concatenado" + (" com narracao" if master_name else ""),
            {"base": base_out.name, "master": master_name, "parts": len(videos)},
        )
    except JobCanceled:
        finish_canceled_job(job_id, project_id, "concat_parts")
    except Exception as exc:  # noqa: BLE001
        db.fail_job(job_id, "Falha na concatenacao", str(exc))


@app.post("/projects/{project_id}/concat-parts", responses=ERROR_RESPONSES)
def concat_parts(
    request: Request,
    project_id: int,
    background_tasks: BackgroundTasks,
    csrf_token: Annotated[str, Form()] = "",
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


@app.get("/projects/{project_id}/parts-status", responses=ERROR_RESPONSES)
def parts_status(request: Request, project_id: int):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    jobs = db.list_project_jobs(project_id, user["id"])
    active = next(
        (j for j in jobs if j["kind"] in {"kaggle_parts", "concat_parts"} and j["status"] in ACTIVE_JOB_STATUSES),
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


@app.get("/jobs/{job_id}", responses=ERROR_RESPONSES)
def job_status(request: Request, job_id: int):
    user = require_user(request)
    job = db.get_job(job_id, user["id"])
    if not job:
        raise HTTPException(404)
    return JSONResponse(job)


@app.post("/jobs/{job_id}/cancel", responses=ERROR_RESPONSES)
def cancel_job(request: Request, job_id: int, csrf_token: Annotated[str, Form()] = ""):
    user = require_user(request)
    verify_csrf(request, csrf_token)
    job = db.request_job_cancel(job_id, user["id"])
    if not job:
        raise HTTPException(404)
    if job["status"] not in ACTIVE_JOB_STATUSES:
        raise HTTPException(409, f"Job ja esta {job['status']}.")
    return JSONResponse(job)


@app.get("/projects/{project_id}/jobs", responses=ERROR_RESPONSES)
def project_jobs(request: Request, project_id: int):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    return JSONResponse({"project_status": project.get("status", ""), "jobs": db.list_project_jobs(project_id, user["id"])})


@app.get("/projects/{project_id}/diagnostics.json", responses=ERROR_RESPONSES)
def project_diagnostics_json(request: Request, project_id: int, refresh: str = ""):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    scenes = db.list_scenes(project_id)
    assets_by_scene = db.list_assets_for_project(project_id)
    for scene in scenes:
        scene_assets = annotate_assets_with_vision(scene, assets_by_scene.get(scene["id"], []), project_config(project))
        scene["assets"] = scene_assets
        scene["selected"] = next((a for a in scene_assets if a["state"] in CHOSEN_ASSET_STATES), None)
        scene["low_relevance_count"] = sum(1 for a in scene_assets if a.get("low_relevance"))
    curation_stats = annotate_broll_requirements(scenes, project_config(project))
    broll_required = {sid for sid, on in scene_broll_flags(scenes, project_config(project)).items() if on}
    scene_code_by_id = {scene["id"]: scene["scene_id"] for scene in scenes}
    selected = {
        row["scene_id"]
        for row in db.list_assets_by_state(project_id, CHOSEN_ASSET_STATES)
        if scene_code_by_id.get(row["scene_id"]) in broll_required
    }
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
        scene_count=len(broll_required),
        total_scene_count=len(scenes),
        expected_duration=expected_duration_from_scenes(scenes),
    )
    if validation:
        snapshot["outputs"]["validation"] = validation
    jobs = db.list_project_jobs(project_id, user["id"])
    outputs = local_output_videos(project_work)
    return JSONResponse(
        {
            "project": {"id": project_id, "name": project["name"], "status": project["status"]},
            "diagnostics": snapshot,
            "api_usage": db.api_usage_summary(user["id"], project_id=project_id),
            "problem_scenes": problem_scenes(scenes, project_config(project)),
            "operational_state": ops_status.project_state(
                project,
                scenes=scenes,
                asset_count=sum(len(items) for items in assets_by_scene.values()),
                curation_stats=curation_stats,
                jobs=jobs,
                parts=db.list_parts(project_id) if project_config(project).get("long_mode") else [],
                outputs=outputs,
                diagnostics=snapshot,
                has_asset_keys=has_visual_provider(user),
            ),
            "jobs": jobs,
        }
    )


@app.post("/projects/{project_id}/validate-output", responses=ERROR_RESPONSES)
def validate_output(request: Request, project_id: int, csrf_token: Annotated[str, Form()] = ""):
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


@app.get("/projects/{project_id}/download-render-log", responses=ERROR_RESPONSES)
def download_render_log(request: Request, project_id: int):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    path = project_output_file(project_id, "log_render.txt")
    if not path.exists():
        raise HTTPException(404, "Log de render ainda nao encontrado.")
    return FileResponse(path, filename=path.name, media_type="text/plain")


@app.get("/projects/{project_id}/download-validation", responses=ERROR_RESPONSES)
def download_validation(request: Request, project_id: int):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    path = diagnostics.validation_path(project_work_dir(project_id))
    if not path.exists():
        raise HTTPException(404, "Validacao ainda nao gerada.")
    return FileResponse(path, filename=path.name, media_type=MEDIA_TYPE_JSON)


@app.get("/projects/{project_id}/download-hyperframes-status", responses=ERROR_RESPONSES)
def download_hyperframes_status(request: Request, project_id: int):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    path = project_output_file(project_id, "hyperframes_status.json")
    if not path.exists():
        raise HTTPException(404, "Status HyperFrames ainda nao encontrado.")
    return FileResponse(path, filename=path.name, media_type=MEDIA_TYPE_JSON)


def _enrich_complete_kaggle_status(info: dict, project_id: int, k_slug: str, user: dict) -> None:
    """Anexa URLs de video, hyperframes e validacao quando o render Kaggle concluiu."""
    project_work = project_work_dir(project_id)
    outputs = local_output_videos(project_work)
    if not outputs["base"] and not outputs["master"]:
        with api_usage.context(user_id=user["id"], project_id=project_id, operation="kaggle_pull_output"):
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


@app.get("/projects/{project_id}/kaggle-status", responses=ERROR_RESPONSES)
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
        with api_usage.context(user_id=user["id"], project_id=project_id, operation="kaggle_status"):
            info = kaggle_service.get_status(k_slug, user["kaggle_username"], user["kaggle_token"])
        if info.get("status") == "complete":
            _enrich_complete_kaggle_status(info, project_id, k_slug, user)
        db.update_kaggle_status(project_id, info["status"])
        return JSONResponse(info)
    except Exception as exc:
        return JSONResponse({"status": "error", "error": str(exc)})


@app.get("/projects/{project_id}/download-kaggle-video", responses=ERROR_RESPONSES)
def download_kaggle_video(request: Request, project_id: int):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    video = latest_kaggle_video(project_work_dir(project_id))
    if not video:
        raise HTTPException(404, "Video do Kaggle ainda nao baixado.")
    return FileResponse(video, filename=video.name, media_type=MEDIA_TYPE_MP4)


@app.get("/projects/{project_id}/download-base-video", responses=ERROR_RESPONSES)
def download_base_video(request: Request, project_id: int):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    video = local_output_videos(project_work_dir(project_id))["base"]
    if not video:
        raise HTTPException(404, "Video base ainda nao baixado.")
    return FileResponse(video, filename=video.name, media_type=MEDIA_TYPE_MP4)


@app.get("/projects/{project_id}/download-master-video", responses=ERROR_RESPONSES)
def download_master_video(request: Request, project_id: int):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    video = local_output_videos(project_work_dir(project_id))["master"]
    if not video:
        raise HTTPException(404, "Video master ainda nao renderizado.")
    return FileResponse(video, filename=video.name, media_type=MEDIA_TYPE_MP4)


@app.get("/projects/{project_id}/kaggle-debug", responses=ERROR_RESPONSES)
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
