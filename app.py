"""Sistema 1 — Plataforma Web de Coleta e Curadoria de B-rolls."""
from __future__ import annotations

import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, Request, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

import database as db
from services import asset_search, groq_service, packager, kaggle_service
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
    yield


app = FastAPI(title="B-rolls — Plataforma de Curadoria", lifespan=lifespan)

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

# ------------------------------------------------------------------
# Error handlers (mostra pagina HTML em vez de JSON cru)
# ------------------------------------------------------------------
@app.exception_handler(StarletteHTTPException)
async def html_error_handler(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 401:
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
    user = current_user(request)
    return templates.TemplateResponse(
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
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(project.get("config_json") or "{}"))
    except json.JSONDecodeError:
        pass
    return cfg


def safe_next_url(raw_next: str) -> str:
    """Aceita apenas redirects internos, evitando open redirect no login."""
    if not raw_next:
        return "/projects"
    parsed = urlparse(raw_next)
    if parsed.scheme or parsed.netloc or not raw_next.startswith("/") or raw_next.startswith("//"):
        return "/projects"
    return raw_next


def latest_zip(project_work: Path) -> Optional[Path]:
    return max(project_work.glob("*.zip"), key=lambda p: p.stat().st_mtime, default=None)


def latest_kaggle_video(project_work: Path) -> Optional[Path]:
    output_dir = project_work / "kaggle_output"
    return max(output_dir.rglob("*.mp4"), key=lambda p: p.stat().st_mtime, default=None) if output_dir.exists() else None


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
    return templates.TemplateResponse(request, "login.html", {"error": error, "next": next})


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
    request.session["user_id"] = user["id"]
    return RedirectResponse(safe_next_url(next), status_code=303)


@app.post("/register")
def register(request: Request, username: str = Form(...), password: str = Form(...)):
    username = username.strip()
    if not username or len(password) < 4:
        return RedirectResponse("/login?error=Usuario+ou+senha+invalidos", status_code=303)
    if db.get_user_by_name(username):
        return RedirectResponse("/login?error=Usuario+ja+existe", status_code=303)
    uid = db.create_user(username, password)
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
    return templates.TemplateResponse(request, "settings.html", {
        "user": user,
        "saved": saved,
        "groq_models": groq_service.GROQ_MODELS,
    })


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
    kaggle_username: str = Form(""),
    kaggle_token: str = Form(""),
):
    user = require_user(request)
    db.update_api_keys(user["id"], pexels.strip(), pixabay.strip(), groq.strip(), groq_model.strip())
    db.update_kaggle_keys(user["id"], kaggle_username.strip(), kaggle_token.strip())
    return RedirectResponse("/settings?saved=1", status_code=303)


# ------------------------------------------------------------------
# Transcrição de áudio (Groq Whisper → timestamps)
# ------------------------------------------------------------------
@app.post("/transcribe-audio")
async def transcribe_audio(request: Request, audio: UploadFile = File(...)):
    user = require_user(request)
    if not user.get("groq_key"):
        raise HTTPException(400, "Configure a chave Groq em /settings para usar transcrição.")
    data = await audio.read()
    if len(data) > 100 * 1024 * 1024:
        raise HTTPException(400, "Arquivo muito grande (máximo 100 MB).")
    try:
        transcript = groq_service.transcribe_audio(data, audio.filename or "audio.mp3", user["groq_key"])
        return JSONResponse({"transcript": transcript})
    except Exception as exc:
        raise HTTPException(500, f"Transcrição falhou: {exc}") from exc


# ------------------------------------------------------------------
# Projects
# ------------------------------------------------------------------
@app.get("/projects", response_class=HTMLResponse)
def projects_page(request: Request):
    user = require_user(request)
    projects = db.list_projects(user["id"])
    return templates.TemplateResponse(request, "projects.html", {"user": user, "projects": projects})


@app.get("/projects/new", response_class=HTMLResponse)
def new_project_page(request: Request):
    user = require_user(request)
    return templates.TemplateResponse(request, "new_project.html", {"user": user, "config": DEFAULT_CONFIG})


@app.post("/projects/new")
def new_project(
    request: Request,
    name: str = Form(...),
    script: str = Form(...),
    avatar_safe_area: str = Form("right"),
    visual_style: str = Form(DEFAULT_CONFIG["visual_style"]),
    resolution: str = Form("1920x1080"),
    scene_duration: float = Form(4.0),
    image_fallback: str = Form(""),
):
    user = require_user(request)
    config = dict(DEFAULT_CONFIG)
    config.update({
        "avatar_safe_area": avatar_safe_area,
        "visual_style": visual_style.strip() or DEFAULT_CONFIG["visual_style"],
        "resolution": resolution,
        "scene_duration": float(scene_duration or 4.0),
        "image_fallback": bool(image_fallback),
    })
    pid = db.create_project(user["id"], name.strip() or "projeto", script, config)
    return RedirectResponse(f"/projects/{pid}", status_code=303)


@app.post("/projects/{project_id}/delete")
def delete_project(request: Request, project_id: int):
    user = require_user(request)
    db.delete_project(project_id, user["id"])
    return RedirectResponse("/projects", status_code=303)


@app.get("/projects/{project_id}", response_class=HTMLResponse)
def project_page(request: Request, project_id: int):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    config = project_config(project)
    scenes = db.list_scenes(project_id)
    for s in scenes:
        s["assets"] = db.list_assets(s["id"])
        s["selected"] = next((a for a in s["assets"] if a["state"] == "selected"), None)
    selected_count = sum(1 for s in scenes if s.get("selected"))
    return templates.TemplateResponse(
        request,
        "project.html",
        {
            "user": user,
            "project": project,
            "config": config,
            "scenes": scenes,
            "selected_count": selected_count,
            "has_keys": bool(user["pexels_key"] or user["pixabay_key"]),
        },
    )


# ------------------------------------------------------------------
# Gerar mapa visual
# ------------------------------------------------------------------
@app.post("/projects/{project_id}/generate-map")
def generate_map(request: Request, project_id: int):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    config = project_config(project)

    base_scenes = parse_script(project["script"], config["scene_duration"])
    if not base_scenes:
        raise HTTPException(400, "Roteiro vazio ou invalido.")

    briefs = groq_service.generate_briefs(
        base_scenes,
        groq_key=user["groq_key"],
        style=config["visual_style"],
        avatar_safe_area=config["avatar_safe_area"],
        safe_ratio=config["avatar_safe_width_ratio"],
        model=user.get("groq_model") or groq_service.DEFAULT_MODEL,
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
    db.set_project_status(project_id, "mapped")
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


# ------------------------------------------------------------------
# Buscar assets
# ------------------------------------------------------------------
@app.post("/projects/{project_id}/search")
def search_all(request: Request, project_id: int):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    config = project_config(project)
    if not user["pexels_key"] and not user["pixabay_key"]:
        raise HTTPException(400, "Cadastre ao menos uma chave de API em /settings.")

    max_w = int(config["resolution"].split("x")[0])
    scenes = db.list_scenes(project_id)
    seen: set = set()
    for scene in scenes:
        results = asset_search.search_scene(
            scene["keywords"],
            user["pexels_key"],
            user["pixabay_key"],
            max_w=max_w,
            per_keyword=config["per_keyword"],
            allow_images=bool(config["image_fallback"]),
            seen_urls=seen,
        )
        db.add_assets(scene["id"], results)
    db.set_project_status(project_id, "searched")
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.post("/scenes/{scene_db_id}/search-more")
def search_more(request: Request, scene_db_id: int, media: str = Form("all")):
    user = require_user(request)
    scene = db.get_scene(scene_db_id)
    if not scene:
        raise HTTPException(404)
    project = db.get_project(scene["project_id"], user["id"])
    if not project:
        raise HTTPException(404)
    config = project_config(project)
    if media not in {"all", "video", "image"}:
        media = "all"
    max_w = int(config["resolution"].split("x")[0])
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
    return JSONResponse({"added": added, "media": media})


@app.post("/scenes/{scene_db_id}/regen-keywords")
def regen_keywords(request: Request, scene_db_id: int):
    user = require_user(request)
    scene = db.get_scene(scene_db_id)
    if not scene:
        raise HTTPException(404)
    project = db.get_project(scene["project_id"], user["id"])
    if not project:
        raise HTTPException(404)
    config = project_config(project)
    kws = groq_service.regenerate_keywords(
        scene.get("narration", ""), scene.get("visual_goal", ""), user["groq_key"], config["visual_style"]
    )
    db.update_scene_keywords(scene_db_id, kws)
    return JSONResponse({"keywords": kws})


# ------------------------------------------------------------------
# Curadoria
# ------------------------------------------------------------------
@app.post("/assets/{asset_id}/state")
def asset_state(request: Request, asset_id: int, state: str = Form(...)):
    user = require_user(request)
    if state not in {"pending", "selected", "rejected", "favorite"}:
        raise HTTPException(400, "Estado invalido.")
    if not db.asset_belongs_to_user(asset_id, user["id"]):
        raise HTTPException(404)
    updated = db.set_asset_state(asset_id, state)
    if not updated:
        raise HTTPException(404)
    return JSONResponse({"id": asset_id, "state": updated["state"]})


# ------------------------------------------------------------------
# Gerar pacote (ZIP)
# ------------------------------------------------------------------
@app.post("/projects/{project_id}/package")
def package(request: Request, project_id: int):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    config = project_config(project)
    scenes = db.list_scenes(project_id)

    selected_rows = db.list_assets_by_state(project_id, ["selected"])
    selected_by_scene = {row["scene_id"]: row for row in selected_rows}
    rejected = db.list_assets_by_state(project_id, ["rejected"])

    if not selected_by_scene:
        raise HTTPException(400, "Selecione ao menos um asset antes de gerar o pacote.")

    project_work = WORK_DIR / f"project_{project_id}"
    try:
        packager.build_zip(
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
        )
    except RuntimeError as exc:
        db.set_project_status(project_id, "package_failed")
        raise HTTPException(502, f"Falha ao gerar pacote: {exc}") from exc
    db.set_project_status(project_id, "packaged")
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@app.get("/projects/{project_id}/download-zip")
def download_zip(request: Request, project_id: int):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    project_work = WORK_DIR / f"project_{project_id}"
    zip_path = latest_zip(project_work)
    if not zip_path:
        raise HTTPException(404, "ZIP não encontrado. Gere o pacote primeiro.")
    return FileResponse(zip_path, filename=zip_path.name, media_type="application/zip")


# ------------------------------------------------------------------
# Kaggle — enviar para render
# ------------------------------------------------------------------
@app.post("/projects/{project_id}/send-to-kaggle")
def send_to_kaggle(request: Request, project_id: int):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        return JSONResponse({"error": "Projeto não encontrado."}, status_code=404)
    if not user.get("kaggle_username") or not user.get("kaggle_token"):
        return JSONResponse({"error": "Configure Kaggle username e token em /configurações."}, status_code=400)

    project_work = WORK_DIR / f"project_{project_id}"
    zip_path = latest_zip(project_work)
    if not zip_path:
        return JSONResponse({"error": "ZIP não encontrado. Clique em '3 · Preparar pacote' novamente."}, status_code=400)

    try:
        ds_slug = kaggle_service.upload_dataset(
            zip_path, project["name"], user["kaggle_username"], user["kaggle_token"]
        )
        k_slug, push_out = kaggle_service.push_kernel(
            ds_slug, project["name"], user["kaggle_username"], user["kaggle_token"]
        )
        db.update_kaggle_job(project_id, ds_slug, k_slug, "queued")
        kernel_url = f"https://www.kaggle.com/code/{user['kaggle_username']}/{k_slug}"
        return JSONResponse({"status": "queued", "kernel_url": kernel_url, "kernel_slug": k_slug, "push_out": push_out})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/projects/{project_id}/kaggle-status")
def kaggle_status(request: Request, project_id: int):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    k_slug = project.get("kaggle_kernel_slug", "")
    if not k_slug:
        return JSONResponse({"status": "none"})
    try:
        info = kaggle_service.get_status(k_slug, user["kaggle_username"], user["kaggle_token"])
        if info.get("status") == "complete" and not info.get("video_url"):
            project_work = WORK_DIR / f"project_{project_id}"
            local_video = latest_kaggle_video(project_work)
            if not local_video:
                local_video = kaggle_service.pull_output_video(
                    k_slug,
                    user["kaggle_username"],
                    user["kaggle_token"],
                    project_work / "kaggle_output",
                )
            if local_video:
                info["video_url"] = f"/projects/{project_id}/download-kaggle-video"
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
    video = latest_kaggle_video(WORK_DIR / f"project_{project_id}")
    if not video:
        raise HTTPException(404, "Video do Kaggle ainda nao baixado.")
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
    import subprocess, sys
    env = {**__import__("os").environ, "KAGGLE_USERNAME": u, "KAGGLE_KEY": t}
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
