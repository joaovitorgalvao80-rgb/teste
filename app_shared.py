"""NWRCH Studio — módulo compartilhado.

Configuração, constantes, helpers e funções de background job.
Não contém a instância FastAPI (app) para evitar importações circulares:
  app.py  →  app_shared   (correto)
  routes/ →  app_shared   (correto)
  app_shared  →  app.py   (nunca!)
"""
from __future__ import annotations

import base64
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
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Optional
from urllib.parse import urlparse

from fastapi import Depends, File, Form, Request, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse, PlainTextResponse
from pydantic import BaseModel
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

import database as db
from services import (
    api_usage,
    asset_search,
    auto_select,
    diagnostics,
    edit_plan,
    editorial_analysis,
    groq_service,
    kaggle_service,
    ops_status,
    packager,
    scoring,
    vision,
)
from services.project_config import (
    ALLOWED_BROLL_DENSITIES,
    ALLOWED_VIDEO_STYLES,
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
# Configuração via variáveis de ambiente
# ------------------------------------------------------------------
def _load_env_file(path: Path = ROOT / ".env") -> None:
    """Carrega .env local simples sem sobrescrever variáveis já exportadas."""
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
    "1", "true", "yes", "on",
}
ALLOW_REGISTRATION = os.getenv("ALLOW_REGISTRATION", "1" if APP_ENV != "production" else "0").strip().lower() in {
    "1", "true", "yes", "on",
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
CHOSEN_ASSET_STATES = ["selected", "accepted"]
EDIT_PLAN_FILENAME = "edit_plan.json"
EDITORIAL_REPORT_FILENAME = "editorial_report.json"
PROJECTS_PATH = "/projects"
MEDIA_TYPE_JSON = "application/json"
MEDIA_TYPE_MP4 = "video/mp4"
MSG_PROJECT_NOT_FOUND = "Projeto nao encontrado."
MSG_NO_API_KEYS = "Cadastre ao menos uma chave de API em /settings."
MSG_SELECTING_TAKES = "Selecionando os melhores takes"
MSG_PART_NOT_FOUND = "Parte nao encontrada."
ERROR_RESPONSES = {
    400: {"description": "Requisicao invalida"},
    401: {"description": "Nao autenticado / sessao expirada"},
    403: {"description": "Acesso negado"},
    404: {"description": "Recurso nao encontrado"},
    409: {"description": "Conflito com o estado atual do projeto"},
    500: {"description": "Erro interno do servidor"},
}

# Áudio/vídeo: extensões aceitas para upload e limites da API Groq
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".flv", ".wmv"}
_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac", ".wma"}
_ALLOWED_UPLOAD_EXTS = _VIDEO_EXTS | _AUDIO_EXTS
_GROQ_MAX_BYTES = 24 * 1024 * 1024

NARRATION_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
AVATAR_EXTS = {".webm", ".mov", ".mp4"}
MEDIA_KINDS = {"narration": NARRATION_EXTS, "avatar": AVATAR_EXTS}
MAX_MEDIA_UPLOAD_MB = 200
MAX_TRANSCRIBE_UPLOAD_MB = 500
MAX_KEYS_FILE_BYTES = MAX_KEYS_FILE_BYTES  # re-export

# Imagens geradas por IA
GENERATED_DIR_NAME = "generated"
MAX_GENERATED_UPLOAD_MB = 15
_GENERATED_NAME_RE = re.compile(r"^gen_[0-9a-f]{32}\.(png|jpg|webp)$")
_GENERATED_MEDIA_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}

# Video longo: render por partes
PART_POLL_SECONDS = 30
PART_RENDER_TIMEOUT = 45 * 60

# Curadoria
CURATION_REPORT_NAME = "curation_report.md"

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
async def lifespan(_app):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
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


# ------------------------------------------------------------------
# Templates e filtros Jinja2
# ------------------------------------------------------------------
templates = Jinja2Templates(directory=str(ROOT / "templates"))

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


# ------------------------------------------------------------------
# Helpers de sessão / CSRF / template
# ------------------------------------------------------------------
def csrf_token_for(request: Request) -> str:
    token = request.session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["_csrf_token"] = token
    return token


def _app_setting(name: str, default):
    """Read a setting from the 'app' module if loaded, else fall back to default.

    Tests mutate module globals via `webapp.X = value`; this indirection lets
    those mutations propagate to functions defined in app_shared without a
    circular import.
    """
    import sys
    m = sys.modules.get("app")
    return getattr(m, name, default) if m is not None else default


def verify_csrf(request: Request, token: str = "") -> None:
    if not _app_setting("ENFORCE_CSRF", ENFORCE_CSRF):
        return
    expected = request.session.get("_csrf_token", "")
    supplied = token or request.headers.get("x-csrf-token", "")
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        raise HTTPException(403, "CSRF token invalido. Recarregue a pagina e tente novamente.")


def registration_state() -> dict:
    allow_registration = _app_setting("ALLOW_REGISTRATION", ALLOW_REGISTRATION)
    allow_first_user = _app_setting("ALLOW_FIRST_USER", ALLOW_FIRST_USER)
    invite_code = _app_setting("INVITE_CODE", INVITE_CODE)
    user_count = db.count_users()
    first_user_allowed = allow_first_user and user_count == 0
    invite_required = bool(invite_code) and not first_user_allowed
    enabled = first_user_allowed or allow_registration or bool(invite_code)
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


def has_research_provider(user: dict) -> bool:
    return has_visual_provider(user)


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


def _wants_json(request: Request) -> bool:
    """Detecta chamadas fetch/XHR para responder JSON em vez de página HTML."""
    fetch_mode = request.headers.get("sec-fetch-mode", "")
    if fetch_mode and fetch_mode != "navigate":
        return True
    return MEDIA_TYPE_JSON in request.headers.get("accept", "")


def current_user(request: Request) -> Optional[dict]:
    uid = request.session.get("user_id")
    return db.get_user(uid) if uid else None


def require_user(request: Request) -> dict:
    # When tests patch webapp.require_user, delegate to the patched version.
    import sys
    m = sys.modules.get("app")
    if m is not None:
        fn = m.__dict__.get("require_user")
        if fn is not None and fn is not require_user:
            return fn(request)
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401)
    return user


def scene_broll_flags(scenes: list[dict], config: dict) -> dict:
    """Mapeia scene_id -> bool usando a MESMA decisão do render."""
    out: dict = {}
    if config.get("long_mode"):
        by_part: dict = {}
        for s in scenes:
            by_part.setdefault(int(s.get("part") or 1), []).append(s)
        for idx in sorted(by_part):
            grp = by_part[idx]
            for s, flag in zip(grp, edit_plan.decide_broll(grp, config=config)):
                out[s["scene_id"]] = flag
    else:
        for s, flag in zip(scenes, edit_plan.decide_broll(scenes, config=config)):
            out[s["scene_id"]] = flag
    return out


def missing_selected_scene_ids(
    scenes: list[dict],
    selected_by_scene: dict[int, dict],
    required_scene_ids: Optional[set] = None,
) -> list[str]:
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


def _chosen_asset_reasons(chosen: dict, scene: dict, target_w: int) -> list[str]:
    reasons: list[str] = []
    if chosen.get("low_relevance"):
        reasons.append("take escolhido com baixa relevancia")
    if chosen.get("vision_verdict") == "descartar":
        reasons.append("visao sugere descartar")
    if chosen.get("asset_type") == "video":
        if float(chosen.get("duration") or 0) < float(scene.get("duration") or 0) * 0.5:
            reasons.append("video curto para a cena")
    if int(chosen.get("width") or 0) and int(chosen.get("width") or 0) < target_w * 0.66:
        reasons.append("resolucao baixa")
    return reasons


def problem_scenes(scenes: list[dict], config: dict, limit: int = 8) -> list[dict]:
    """Cenas de b-roll que merecem revisão antes de gastar render."""
    target_w = resolution_width(config)
    out: list[dict] = []
    for scene in scenes:
        if not scene.get("broll_required"):
            continue
        assets = scene.get("assets") or []
        chosen = scene.get("selected")
        if not assets:
            reasons: list[str] = ["sem candidatos"]
        elif not chosen:
            reasons = ["sem take escolhido"]
        else:
            reasons = _chosen_asset_reasons(chosen, scene, target_w)
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
VISION_LLM_TOP_N = int(os.getenv("VISION_LLM_TOP_N", "8"))
VISION_SHEET_N = int(os.getenv("VISION_SHEET_N", "5"))
VIDEO_FRAME_VISION_MAX_FRAMES = int(os.getenv("VIDEO_FRAME_VISION_MAX_FRAMES", "9"))
_TAKE_STATE_RANK = {"accepted": 3, "selected": 2, "favorite": 1, "pending": 0, "rejected": -1}


def _take_sort_key(asset: dict) -> tuple:
    query_role = str(asset.get("query_role") or "")
    manual_rank = 1 if query_role.startswith("manual_") else 0
    return (
        _TAKE_STATE_RANK.get(asset.get("state", "pending"), 0),
        manual_rank,
        float(asset.get("vision_score") or 0),
        float(asset.get("relevance") or 0),
        int(asset.get("id") or 0),
    )


def annotate_assets_with_vision(scene: dict, assets: list[dict], config: dict) -> list[dict]:
    """Anexa sinais de curadoria a cada asset para a UI."""
    annotated: list[dict] = []
    for asset in assets:
        item = dict(asset)
        context = scoring.context_analysis(scene, asset)
        relevance = float(context["context_score"])
        if asset.get("vision_analyzed"):
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
            analysis = _VISION_PROVIDER.analyze(asset, scene, config)
            item["vision_score"] = analysis.score
            item["vision_flags"] = analysis.flags
            item["vision_verdict"] = analysis.verdict
            item["vision_reason"] = "; ".join(analysis.reasons)
            item["low_relevance"] = analysis.relevance < 0.33
        item["relevance"] = round(relevance, 3)
        item["context_score"] = round(relevance, 3)
        item["matched_terms"] = context["matched"]
        item["missing_terms"] = context["missing"]
        item["context_risks"] = context["risks"]
        item["relevance_label"] = scoring.relevance_label(relevance)
        annotated.append(item)
    return annotated


def project_work_dir(project_id: int) -> Path:
    return _app_setting("WORK_DIR", WORK_DIR) / f"project_{project_id}"


def _frame_data_url(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    if not raw:
        return ""
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _verdict_for_frame_score(score: float, discarded: bool) -> str:
    if discarded or score < 35:
        return "descartar"
    if score >= 70:
        return "otimo"
    if score >= 45:
        return "bom"
    return "fraco"


def _analyze_video_frame_samples(
    project_id: int,
    user: dict,
    project_work: Path,
    payload: dict,
    manifest_path: Path,
) -> None:
    if payload.get("vision_status") == "analyzed":
        return
    groq_key = user.get("groq_key", "")
    if not groq_key:
        payload["vision_status"] = "unavailable"
        payload["vision_reason"] = "sem chave Groq para analisar frames"
        manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return
    scenes = db.list_scenes(project_id)
    config = project_config(db.get_project(project_id, user["id"]) or {})
    _inject_video_theme(scenes, config)
    scenes_by_code = {s.get("scene_id"): s for s in scenes}
    provider = vision.get_provider("groq", api_key=groq_key)
    analyzed_frames = 0

    for sample in payload.get("samples") or []:
        scene = scenes_by_code.get(sample.get("scene_id")) or {}
        frame_results = []
        for frame in sample.get("frames") or []:
            if analyzed_frames >= VIDEO_FRAME_VISION_MAX_FRAMES:
                break
            rel = str(frame.get("file") or "")
            frame_path = project_work / "kaggle_output" / rel
            data_url = _frame_data_url(frame_path)
            if not data_url:
                continue
            asset = {
                "id": analyzed_frames + 1,
                "asset_type": "image",
                "keyword": sample.get("selected_asset") or scene.get("visual_target") or scene.get("visual_goal") or "",
                "frame_data_url": data_url,
                "width": 640,
                "height": 360,
            }
            result = provider.analyze(asset, scene, config)
            frame_results.append(
                {
                    "file": rel,
                    "score": result.score,
                    "verdict": result.verdict,
                    "reasons": result.reasons[:3],
                    "flags": result.flags,
                    "provider": result.provider,
                }
            )
            analyzed_frames += 1
        if frame_results:
            avg = sum(float(r["score"]) for r in frame_results) / len(frame_results)
            discarded = any(r["verdict"] == "descartar" for r in frame_results)
            sample["frame_vision"] = frame_results
            sample["video_frame_score"] = round(avg, 1)
            sample["video_frame_verdict"] = _verdict_for_frame_score(avg, discarded)
            sample["reason"] = "frames analisados por visao" if not discarded else "ao menos um frame foi descartado pela visao"
    payload["vision_status"] = "analyzed" if analyzed_frames else "unavailable"
    payload["vision_provider"] = provider.name if analyzed_frames else ""
    payload["analyzed_frames"] = analyzed_frames
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def local_video_frame_samples(project_work: Path, project_id: int = 0, user: Optional[dict] = None) -> dict:
    """Resumo leve dos frames extraidos no Kaggle para videos finalistas."""
    candidates = [
        project_work / "kaggle_output" / "metadata" / "video_frame_samples.json",
        project_work / "kaggle_output" / "video_frame_samples.json",
    ]
    path = next((p for p in candidates if p.exists()), None)
    if not path:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if project_id and user:
        _analyze_video_frame_samples(project_id, user, project_work, payload, path)
    samples = payload.get("samples") or []
    sampled_frames = sum(int(s.get("sampled_frames") or 0) for s in samples)
    return {
        "status": payload.get("status") or "",
        "vision_status": payload.get("vision_status") or "",
        "vision_provider": payload.get("vision_provider") or "",
        "analyzed_frames": int(payload.get("analyzed_frames") or 0),
        "manifest": str(path),
        "sampled_videos": len(samples),
        "sampled_frames": sampled_frames,
        "errors": payload.get("errors") or [],
    }


def _safe_child_dir(root: Path, child: Path) -> Optional[Path]:
    root_resolved = root.resolve()
    child_resolved = child.resolve()
    try:
        child_resolved.relative_to(root_resolved)
    except ValueError:
        return None
    return child_resolved


def remove_project_artifacts(project_id: int, include_generated: bool = False) -> None:
    project_work = _safe_child_dir(_app_setting("WORK_DIR", WORK_DIR), project_work_dir(project_id))
    if not project_work or not project_work.exists():
        return
    for zip_file in project_work.glob("*.zip"):
        zip_file.unlink(missing_ok=True)
    (project_work / EDIT_PLAN_FILENAME).unlink(missing_ok=True)
    (project_work / EDITORIAL_REPORT_FILENAME).unlink(missing_ok=True)
    for folder_name in ["assets_tmp", "kaggle_output"]:
        folder = _safe_child_dir(project_work, project_work / folder_name)
        if folder and folder.exists():
            shutil.rmtree(folder, ignore_errors=True)
    if include_generated:
        generated = _safe_child_dir(project_work, project_work / GENERATED_DIR_NAME)
        if generated and generated.exists():
            shutil.rmtree(generated, ignore_errors=True)


def remove_project_workspace(project_id: int) -> None:
    project_work = _safe_child_dir(_app_setting("WORK_DIR", WORK_DIR), project_work_dir(project_id))
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


def curation_report_path(project_id: int) -> Path:
    return project_work_dir(project_id) / CURATION_REPORT_NAME


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


def project_inputs_dir(project_id: int) -> Path:
    return _app_setting("WORK_DIR", WORK_DIR) / f"project_{project_id}" / "inputs"


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


def local_editorial_report(project_id: int) -> Optional[dict]:
    report_path = project_work_dir(project_id) / EDITORIAL_REPORT_FILENAME
    if not report_path.exists():
        return None
    try:
        return json.loads(report_path.read_text(encoding="utf-8"))
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


def project_generated_dir(project_id: int) -> Path:
    return project_work_dir(project_id) / GENERATED_DIR_NAME


def project_output_file(project_id: int, filename: str) -> Path:
    return project_work_dir(project_id) / "kaggle_output" / filename


# ------------------------------------------------------------------
# Áudio: extração para upload/transcrição
# ------------------------------------------------------------------
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


def prepare_narration_media(raw: bytes, filename: str) -> tuple[bytes, str]:
    """Normaliza upload inicial de narração (áudio ou vídeo → MP3)."""
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


# ------------------------------------------------------------------
# Kaggle send job
# ------------------------------------------------------------------
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
    except Exception as exc:  # noqa: BLE001
        db.update_kaggle_status(project_id, "error")
        db.fail_job(job_id, "Falha ao enviar para o Kaggle", str(exc))


# ------------------------------------------------------------------
# Background jobs: geração do mapa visual
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
                "screen_mode": b.get("screen_mode", ""),
                "visual_need": b.get("visual_need", 0),
                "visual_strategy": b.get("visual_strategy", ""),
                "visual_target": b.get("visual_target", ""),
                "keywords": b.get("keywords", []),
                "query_ladder": b.get("query_ladder", b.get("keywords", [])),
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


# ------------------------------------------------------------------
# Background jobs: busca de assets
# ------------------------------------------------------------------
def _collect_pending_targets(
    scenes: list[dict], assets_by_scene: dict, broll_map: dict
) -> tuple[list[dict], dict]:
    target_scenes: list[dict] = []
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
    return target_scenes, candidates_by_scene


def _collect_seed_diversity(
    assets_by_scene: dict, target_scene_ids: set
) -> tuple[set, set]:
    seed_signatures: set[tuple[str, str]] = set()
    seed_authors: set[str] = set()
    for scene_id, assets in assets_by_scene.items():
        if scene_id in target_scene_ids:
            continue
        for a in assets:
            if a.get("state") in {"accepted", "selected"}:
                seed_signatures.add(scoring.asset_signature(a))
                if a.get("author"):
                    seed_authors.add(str(a["author"]))
    return seed_signatures, seed_authors


def auto_select_for_project(
    project_id: int,
    config: dict,
    groq_key: str,
    groq_model: str,
    job_id: Optional[int] = None,
    review_round: int = 0,
    part_idx: Optional[int] = None,
) -> int:
    """Escolhe o melhor take pendente para cada cena sem take aceito."""
    scenes = db.list_scenes(project_id)
    if part_idx is not None:
        scenes = [s for s in scenes if int(s.get("part") or 1) == part_idx]
    assets_by_scene = db.list_assets_for_project(project_id)
    broll_map = scene_broll_flags(scenes, config)
    target_scenes, candidates_by_scene = _collect_pending_targets(scenes, assets_by_scene, broll_map)

    if not target_scenes:
        return 0

    target_scene_ids = {s["id"] for s in target_scenes}
    seed_signatures, seed_authors = _collect_seed_diversity(assets_by_scene, target_scene_ids)

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
        seed_signatures=seed_signatures,
        seed_authors=seed_authors,
    )
    if job_id:
        check_job_canceled(job_id)
    missing_choice_scene_ids = {s["id"] for s in target_scenes} - set(choices)
    for scene_db_id in missing_choice_scene_ids:
        for asset in assets_by_scene.get(scene_db_id, []):
            if asset.get("state") == "selected" and asset.get("auto_reason"):
                db.set_asset_state(
                    asset["id"],
                    "pending",
                    auto_reason="removido: nenhum candidato confiavel para selecao automatica",
                    review_round=review_round,
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
        broll_map = scene_broll_flags(scenes, config)
        broll_scenes = [s for s in scenes if broll_map.get(s["scene_id"], True)]
        broll_count = len(broll_scenes)
        for scene_idx, scene in enumerate(broll_scenes, 1):
            check_job_canceled(job_id)
            db.update_job(job_id, status="running", message=f"[{scene_idx}/{broll_count}] {scene['scene_id']} — buscando assets")
            with api_usage.context(user_id=user_id, project_id=project_id, job_id=job_id, operation="search_assets"):
                results = asset_search.search_scene(
                    groq_service.normalized_scene_queries(scene),
                    pexels_key,
                    pixabay_key,
                    max_w=max_w,
                    per_keyword=config["per_keyword"],
                    allow_images=bool(config["image_fallback"]),
                    seen_urls=seen,
                    coverr_key=coverr_key,
                    extra_image_banks=True,
                    scene=scene,
                )
            check_job_canceled(job_id)
            added = db.add_assets(scene["id"], results)
            total_added += added
            if added == 0:
                empty_scenes.append(scene["scene_id"])
            else:
                db.update_job(job_id, status="running", message=f"[{scene_idx}/{broll_count}] {scene['scene_id']} → {added} assets")
        if broll_count > 0 and total_added <= 0:
            raise RuntimeError("Busca retornou zero assets. Verifique chaves, keywords ou disponibilidade das APIs.")
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
        broll_scenes = [s for s in scenes if broll_map.get(s["scene_id"], True)]
        broll_count = len(broll_scenes)
        for scene_idx, scene in enumerate(broll_scenes, 1):
            check_job_canceled(job_id)
            db.update_job(job_id, status="running", message=f"[{scene_idx}/{broll_count}] {scene['scene_id']} — buscando (parte {part_idx})")
            with api_usage.context(user_id=user_id, project_id=project_id, job_id=job_id, operation="search_part"):
                results = asset_search.search_scene(
                    groq_service.normalized_scene_queries(scene),
                    pexels_key,
                    pixabay_key,
                    max_w=max_w,
                    per_keyword=config["per_keyword"],
                    allow_images=bool(config["image_fallback"]),
                    seen_urls=seen,
                    coverr_key=coverr_key,
                    extra_image_banks=True,
                    scene=scene,
                )
            check_job_canceled(job_id)
            added = db.add_assets(scene["id"], results)
            total_added += added
            if added == 0:
                empty_scenes.append(scene["scene_id"])
            else:
                db.update_job(job_id, status="running", message=f"[{scene_idx}/{broll_count}] {scene['scene_id']} → {added} assets (parte {part_idx})")
        if broll_count > 0 and total_added <= 0:
            raise RuntimeError("Busca retornou zero assets. Verifique chaves, keywords ou disponibilidade das APIs.")
        check_job_canceled(job_id)
        db.update_job(job_id, status="running", message=MSG_SELECTING_TAKES)
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


# ------------------------------------------------------------------
# Background jobs: visão e seleção automática
# ------------------------------------------------------------------
def _build_vision_providers(groq_key: str, nvidia_key: str) -> list:
    """Provedores de visão disponíveis, em ordem de round-robin."""
    providers: list = []
    if groq_key:
        providers.append(vision.get_provider("groq", api_key=groq_key))
    if nvidia_key:
        providers.append(vision.get_provider("nvidia", api_key=nvidia_key))
    return providers


def _score_scene_assets(scene: dict, pend: list, provider, heuristic, config: dict, sheet_n: int):
    """Pontua as candidatas de uma cena (top-N no contact-sheet, resto na heurística)."""
    ranked = sorted(pend, key=lambda a, scene=scene: scoring.context_relevance(scene, a), reverse=True)
    top, rest = ranked[:sheet_n], ranked[sheet_n:]
    results = provider.analyze_batch(top, scene, config)
    for asset in rest:
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


def _inject_video_theme(scenes: list[dict], config: dict) -> None:
    video_theme = str(config.get("video_theme") or "").strip()
    if video_theme:
        for s in scenes:
            s["video_theme"] = video_theme


def analyze_pending_vision(
    project_id: int,
    user_id: int,
    groq_key: str = "",
    progress: Optional[callable] = None,
    nvidia_key: str = "",
    part_idx: Optional[int] = None,
) -> tuple[int, str]:
    """Analisa e persiste os assets ainda não analisados do projeto."""
    project = db.get_project(project_id, user_id)
    if not project:
        raise RuntimeError(MSG_PROJECT_NOT_FOUND)
    config = project_config(project)
    scenes = db.list_scenes(project_id)
    if part_idx is not None:
        scenes = [s for s in scenes if int(s.get("part") or 1) == part_idx]
    _inject_video_theme(scenes, config)
    assets_by_scene = db.list_assets_for_project(project_id)

    providers = _build_vision_providers(groq_key, nvidia_key)
    heuristic = vision.HeuristicVisionProvider()
    primary_name = providers[0].name if providers else heuristic.name
    sheet_n = max(2, VISION_SHEET_N)

    total_pending = sum(
        1 for scene in scenes
        for a in assets_by_scene.get(scene["id"], []) if not a.get("vision_analyzed")
    )
    if total_pending == 0:
        return 0, primary_name

    analyzed = 0
    rr = 0
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


def run_auto_select_job(
    job_id: int,
    project_id: int,
    user_id: int,
    groq_key: str,
    groq_model: str,
) -> None:
    try:
        check_job_canceled(job_id)
        db.update_job(job_id, status="running", message=MSG_SELECTING_TAKES)
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


def run_part_auto_select_vision_job(
    job_id: int,
    project_id: int,
    user_id: int,
    part_idx: int,
    groq_key: str = "",
    groq_model: str = "",
    nvidia_key: str = "",
) -> None:
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
        db.update_job(job_id, status="running", message=MSG_SELECTING_TAKES)
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


# ------------------------------------------------------------------
# Background jobs: re-busca de takes rejeitados
# ------------------------------------------------------------------
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
    with api_usage.context(user_id=user_id, project_id=scene.get("project_id"), job_id=job_id, operation="research_keywords"):
        kws = groq_service.regenerate_keywords(
            scene.get("narration", ""),
            scene.get("visual_goal", ""),
            keys["groq"],
            config["visual_style"],
            model=keys["groq_model"] or groq_service.DEFAULT_MODEL,
            language=config["script_language"],
            rejected_assets=[
                a for a in assets_by_scene.get(scene["id"], [])
                if a.get("state") == "rejected"
            ],
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
            groq_service.normalized_scene_queries(scene),
            keys["pexels"],
            keys["pixabay"],
            max_w=max_w,
            per_keyword=config["per_keyword"] + 4,
            allow_images=True,
            seen_urls=existing,
            coverr_key=keys["coverr"],
            extra_image_banks=True,
            scene=scene,
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

        keys = {
            "pexels": pexels_key,
            "pixabay": pixabay_key,
            "groq": groq_key,
            "groq_model": groq_model,
            "coverr": coverr_key,
        }
        added_total = 0
        for i, scene in enumerate(targets, 1):
            check_job_canceled(job_id)
            label = f"Nova busca {i}/{len(targets)}: {scene['scene_id']}"
            added_total += _research_one_scene(scene, label, job_id, user_id, keys, config, max_w, assets_by_scene)

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


# ------------------------------------------------------------------
# Helpers de curadoria
# ------------------------------------------------------------------
def _write_full_curation_report(project: dict, project_id: int, assets_by_scene: dict) -> None:
    """Gera o relatório completo de curadoria e marca o projeto como revisado."""
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


def _revert_part_on_take_change(project: dict, owner: dict, asset_id: int) -> None:
    """Modo longo: mexer num take de uma parte já curada exige re-confirmar a parte."""
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
        db.set_project_status(owner["project_id"], "reviewing")
        curation_report_path(owner["project_id"]).unlink(missing_ok=True)
        return "reviewing"
    if status != "reviewing":
        mark_project_dirty(owner["project_id"])
        curation_report_path(owner["project_id"]).unlink(missing_ok=True)
        fresh_project = db.get_project(owner["project_id"], user["id"])
        return (fresh_project or project).get("status", status)
    return status


# ------------------------------------------------------------------
# Background jobs: pacote ZIP
# ------------------------------------------------------------------
def parts_dir(project_id: int) -> Path:
    return project_work_dir(project_id) / "parts"


def part_dir(project_id: int, part_idx: int) -> Path:
    return parts_dir(project_id) / f"part_{part_idx:02d}"


def _rebase_scenes(scenes: list[dict]) -> list[dict]:
    """Clona as cenas da parte com a timeline movida para t=0."""
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
    """Corta o avatar no intervalo da parte (sem áudio)."""
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
    """Contexto compartilhado do job de empacotamento."""
    def __init__(self, job_id, project_id, project, config, scenes, selected_by_scene, rejected_payload):
        self.job_id = job_id
        self.project_id = project_id
        self.project = project
        self.config = config
        self.scenes = scenes
        self.selected_by_scene = selected_by_scene
        self.rejected_payload = rejected_payload


def _validate_package_selection(selected_by_scene, broll_required, required_scene_db_ids) -> None:
    if not broll_required:
        if selected_by_scene:
            return
        raise RuntimeError("Selecao vazia; escolha ao menos um asset antes de gerar pacote.")
    if not any(scene_id in selected_by_scene for scene_id in required_scene_db_ids):
        raise RuntimeError("Selecao vazia; escolha ao menos um asset antes de gerar pacote.")


def _validate_avatar_contract(plan: dict, avatar_file: Optional[Path]) -> None:
    """Falha antes do ZIP se o plano promete avatar mas o pacote nao o carrega."""
    if not bool(plan.get("avatar_required")):
        return
    if not avatar_file or not avatar_file.exists():
        raise RuntimeError("avatar obrigatorio no plano, mas o arquivo avatar.* nao foi encontrado.")
    plan_avatar = plan.get("avatar") or {}
    if not plan_avatar.get("src"):
        raise RuntimeError("avatar obrigatorio sem edit_plan.avatar.src.")
    if Path(str(plan_avatar["src"])).name != avatar_file.name:
        raise RuntimeError("edit_plan.avatar.src nao aponta para o arquivo de avatar do pacote.")


def _fallback_unselected_brolls_to_avatar(scenes: list[dict], selected_by_scene: dict[int, dict]) -> None:
    """Cenas b-roll sem take escolhido viram avatar no pacote/render."""
    for scene in scenes:
        if scene.get("broll") and scene["id"] not in selected_by_scene:
            scene["broll"] = False


def _package_download_progress(job_id: int, prefix: str):
    def _progress(done: int, total: int, scene: dict, ok: bool) -> None:
        status = "ok" if ok else "falhou"
        db.update_job(
            job_id,
            status="running",
            message=f"{prefix} {done}/{total}: {scene.get('scene_id', 'cena')} ({status})",
        )

    return _progress


def _build_part_zip(ctx: "_PackageCtx", part: dict, parts_count: int, avatar_input) -> Optional[str]:
    """Monta o ZIP de uma parte (modo longo)."""
    idx = part["part_idx"]
    check_job_canceled(ctx.job_id)
    db.update_job(ctx.job_id, status="running", message=f"Baixando assets da parte {idx}/{parts_count}")
    part_scenes = [s for s in ctx.scenes if int(s.get("part") or 1) == idx]
    if not part_scenes:
        db.update_part(ctx.project_id, idx, status="error", error="parte sem cenas")
        return None
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
    part_work = part_dir(ctx.project_id, idx)
    part_work.mkdir(parents=True, exist_ok=True)
    editorial_report = editorial_analysis.build_report(
        ctx.project,
        ctx.config,
        rebased,
        ctx.selected_by_scene,
        ctx.rejected_payload,
    )
    part_plan = edit_plan.build_edit_plan(
        ctx.project, ctx.config, rebased,
        narration_file="",
        avatar_file=avatar_name,
        editorial_report=editorial_report,
    )
    _validate_avatar_contract(part_plan, slice_out if avatar_name else None)
    (part_work / EDITORIAL_REPORT_FILENAME).write_text(
        json.dumps(editorial_report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    zip_path = packager.build_zip(
        project=ctx.project,
        config=ctx.config,
        scenes=rebased,
        selected_by_scene=ctx.selected_by_scene,
        rejected_assets=ctx.rejected_payload,
        work_dir=part_work,
        max_download_mb=ctx.config["max_download_mb"],
        edit_plan=part_plan,
        editorial_report=editorial_report,
        extra_files=extras,
        zip_basename=f"{ctx.project['name']}_pt{idx:02d}",
        progress=_package_download_progress(ctx.job_id, f"Parte {idx}: baixando asset"),
    )
    check_job_canceled(ctx.job_id)
    db.update_part(
        ctx.project_id, idx,
        zip_name=zip_path.name, status="zipped",
        error="", video_path="", dataset_slug="", kernel_slug="",
    )
    return zip_path.name


def _package_long_mode(ctx: "_PackageCtx") -> None:
    """Um ZIP por parte: cada parte é um render avatar-base próprio."""
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
    project_work.mkdir(parents=True, exist_ok=True)
    editorial_report = editorial_analysis.build_report(
        ctx.project,
        ctx.config,
        ctx.scenes,
        ctx.selected_by_scene,
        ctx.rejected_payload,
    )
    plan = edit_plan.build_edit_plan(
        ctx.project,
        ctx.config,
        ctx.scenes,
        narration_file=narration_file.name if narration_file else "",
        avatar_file=avatar_file.name if avatar_file else "",
        editorial_report=editorial_report,
    )
    _validate_avatar_contract(plan, avatar_file)
    (project_work / EDIT_PLAN_FILENAME).write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (project_work / EDITORIAL_REPORT_FILENAME).write_text(
        json.dumps(editorial_report, ensure_ascii=False, indent=2), encoding="utf-8"
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
        editorial_report=editorial_report,
        extra_files=[
            f for f in (narration_file, avatar_file, curation_report_path(ctx.project_id)) if f and f.exists()
        ],
        progress=_package_download_progress(ctx.job_id, "Baixando asset"),
    )
    check_job_canceled(ctx.job_id)
    db.set_project_status(ctx.project_id, "packaged")
    db.clear_kaggle_job(ctx.project_id)
    db.finish_job(
        ctx.job_id,
        "Pacote ZIP pronto",
        {"zip": zip_path.name, "scenes": len(ctx.scenes), "selected": len(ctx.selected_by_scene)},
    )


def run_package_job(job_id: int, project_id: int, user_id: int) -> None:
    try:
        check_job_canceled(job_id)
        db.update_job(job_id, status="running", message="Gerando edit_plan e baixando assets")
        project = db.get_project(project_id, user_id)
        if not project:
            raise RuntimeError(MSG_PROJECT_NOT_FOUND)
        config = project_config(project)
        scenes = db.list_scenes(project_id)
        broll_map = scene_broll_flags(scenes, config)
        for s in scenes:
            s["broll"] = bool(broll_map.get(s["scene_id"], True))
        broll_required = {s["scene_id"] for s in scenes if s["broll"]}
        required_scene_db_ids = {s["id"] for s in scenes if s["broll"]}
        selected_rows = db.list_assets_by_state(project_id, CHOSEN_ASSET_STATES)
        selected_by_scene = {row["scene_id"]: row for row in selected_rows}
        rejected = db.list_assets_by_state(project_id, ["rejected"])
        _validate_package_selection(selected_by_scene, broll_required, required_scene_db_ids)
        _fallback_unselected_brolls_to_avatar(scenes, selected_by_scene)
        remove_project_artifacts(project_id)
        rejected_payload = [
            {
                "scene_id": r["scene_code"],
                "source": r["source"],
                "url": r["download_url"],
                "keyword": r["keyword"],
                "reason": r.get("rejection_reason", ""),
            }
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


def _asset_quality_issues(row: dict, scene_dur: float, target_w: int) -> list[str]:
    issues: list[str] = []
    w = int(row.get("width") or 0)
    h = int(row.get("height") or 0)
    dur = float(row.get("duration") or 0)
    if row.get("asset_type") == "video":
        if 0 < w < target_w * 0.66:
            issues.append(f"resolução baixa ({w}x{h}, mínimo recomendado {int(target_w * 0.66)}p)")
        if 0 < dur < scene_dur * 0.4:
            issues.append(f"clip curto ({dur:.1f}s para cena de {scene_dur:.1f}s — será loopado)")
    elif row.get("asset_type") == "image" and 0 < w < target_w * 0.5:
        issues.append(f"imagem pequena ({w}x{h})")
    if (row.get("vision_verdict") or "") == "descartar":
        issues.append("IA de visão marcou como inadequado")
    return issues


# ------------------------------------------------------------------
# Background jobs: render por partes (Kaggle)
# ------------------------------------------------------------------
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
    """Sobe o ZIP da parte, dispara o kernel, aguarda e baixa o MP4."""
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
            except Exception as part_exc:  # noqa: BLE001
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


# ------------------------------------------------------------------
# Background jobs: concatenação final
# ------------------------------------------------------------------
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
    """Stream copy primeiro; re-encode como fallback."""
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
    """Adiciona a narração ao master. Retorna nome do master ou '' se falhar."""
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


# ------------------------------------------------------------------
# Kaggle status helpers
# ------------------------------------------------------------------
def _enrich_complete_kaggle_status(info: dict, project_id: int, k_slug: str, user: dict) -> None:
    """Anexa URLs de vídeo quando o render Kaggle concluiu."""
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
    frame_samples = local_video_frame_samples(project_work, project_id, user)
    if frame_samples:
        info["video_frame_samples"] = frame_samples
    info["validation"] = diagnostics.validate_outputs(
        project_work,
        expected_duration=expected_duration_from_scenes(db.list_scenes(project_id)),
    )
