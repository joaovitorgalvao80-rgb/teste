"""Rotas de curadoria: estado de assets, revisão, visão, preview, imagens geradas."""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated, Optional

from fastapi import APIRouter, BackgroundTasks, File, Form, Request, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

import database as db
from services import api_usage, groq_service
from services.project_config import project_config
from app_shared import (
    CHOSEN_ASSET_STATES,
    ERROR_RESPONSES,
    MAX_GENERATED_UPLOAD_MB,
    MEDIA_KINDS,
    MAX_MEDIA_UPLOAD_MB,
    MSG_NO_API_KEYS,
    MSG_PART_NOT_FOUND,
    _GENERATED_MEDIA_TYPES,
    _GENERATED_NAME_RE,
    _image_kind_and_size,
    _coerce_int,
    _project_status_after_take_change,
    _revert_part_on_take_change,
    annotate_assets_with_vision,
    annotate_broll_requirements,
    curation_report_path,
    ensure_no_active_job,
    ensure_project_not_busy,
    find_input_media,
    has_research_provider,
    mark_project_dirty,
    project_generated_dir,
    read_upload_limited,
    render_template,
    require_user,
    run_research_job,
    run_vision_job,
    save_input_media_bytes,
    scene_broll_flags,
    verify_csrf,
)

router = APIRouter()


@router.post("/assets/{asset_id}/state", responses=ERROR_RESPONSES)
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
    if redirect and redirect.startswith("/") and not redirect.startswith("//"):
        return RedirectResponse(redirect, status_code=303)
    return JSONResponse({"id": asset_id, "state": updated["state"], "project_status": project_status})


@router.post("/projects/{project_id}/analyze-vision", responses=ERROR_RESPONSES)
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


@router.get("/projects/{project_id}/review", response_class=HTMLResponse, responses=ERROR_RESPONSES)
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


@router.post("/projects/{project_id}/research-rejected", responses=ERROR_RESPONSES)
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
    if not has_research_provider(user):
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
        user.get("exa_key", ""),
        user.get("firecrawl_key", ""),
        part_idx,
    )
    suffix = f"?part={part_idx}" if part_idx is not None else ""
    return RedirectResponse(f"/projects/{project_id}/review{suffix}", status_code=303)


@router.post("/projects/{project_id}/finish-review", responses=ERROR_RESPONSES)
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
    from services import packager
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


@router.get("/projects/{project_id}/curation-report", responses=ERROR_RESPONSES)
def download_curation_report(request: Request, project_id: int):
    user = require_user(request)
    if not db.get_project(project_id, user["id"]):
        raise HTTPException(404)
    path = curation_report_path(project_id)
    if not path.exists():
        raise HTTPException(404, "Relatorio de curadoria ainda nao gerado. Conclua a revisao primeiro.")
    return FileResponse(path, filename=path.name, media_type="text/markdown")


@router.get("/projects/{project_id}/preview", response_class=HTMLResponse, responses=ERROR_RESPONSES)
def preview_page(request: Request, project_id: int):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    config = project_config(project)
    scenes = db.list_scenes(project_id)
    chosen_rows = db.list_assets_by_state(project_id, CHOSEN_ASSET_STATES)
    chosen_by_scene = {row["scene_id"]: row for row in chosen_rows}
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


@router.post("/scenes/{scene_db_id}/generated-image", responses=ERROR_RESPONSES)
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
        dest.unlink(missing_ok=True)
        raise
    if added != 1:
        dest.unlink(missing_ok=True)
        raise HTTPException(500, "Falha ao registrar a imagem gerada.")
    mark_project_dirty(project["id"])
    return JSONResponse({"added": added, "url": url})


@router.get("/projects/{project_id}/generated/{filename}", responses=ERROR_RESPONSES)
def serve_generated_image(request: Request, project_id: int, filename: str):
    user = require_user(request)
    if not db.get_project(project_id, user["id"]):
        raise HTTPException(404)
    if not _GENERATED_NAME_RE.fullmatch(filename):
        raise HTTPException(404)
    path = project_generated_dir(project_id) / filename
    if not path.is_file():
        raise HTTPException(404, "Imagem gerada nao encontrada.")
    return FileResponse(
        path,
        media_type=_GENERATED_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream"),
    )


@router.post("/projects/{project_id}/upload-media", responses=ERROR_RESPONSES)
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


@router.post("/projects/{project_id}/remove-media", responses=ERROR_RESPONSES)
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
