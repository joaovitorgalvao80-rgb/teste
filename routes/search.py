"""Rotas de busca: assets, keywords, partes, seleção automática."""
from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, BackgroundTasks, Form, Request, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse

import database as db
from services import api_usage, asset_search, groq_service, scoring
from services.project_config import project_config, resolution_width
from app_shared import (
    ERROR_RESPONSES,
    MSG_NO_API_KEYS,
    MSG_PART_NOT_FOUND,
    auto_select_for_project,
    ensure_no_active_job,
    ensure_project_not_busy,
    has_visual_provider,
    mark_project_dirty,
    require_user,
    run_auto_select_job,
    run_part_auto_select_vision_job,
    run_part_search_job,
    run_search_job,
    scene_broll_flags,
    verify_csrf,
    _write_full_curation_report,
)

router = APIRouter()


@router.post("/projects/{project_id}/search", responses=ERROR_RESPONSES)
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
        user.get("coverr_key", ""),
    )
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/parts/{part_idx}/search", responses=ERROR_RESPONSES)
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
        raise HTTPException(404, MSG_PART_NOT_FOUND)
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
    )
    return RedirectResponse(f"/projects/{project_id}/review?part={part_idx}", status_code=303)


@router.post("/projects/{project_id}/parts/{part_idx}/auto-select-vision", responses=ERROR_RESPONSES)
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
        raise HTTPException(404, MSG_PART_NOT_FOUND)
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


@router.post("/projects/{project_id}/parts/{part_idx}/confirm", responses=ERROR_RESPONSES)
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
        raise HTTPException(404, MSG_PART_NOT_FOUND)
    scenes = [s for s in db.list_scenes(project_id) if int(s.get("part") or 1) == part_idx]
    if not scenes:
        raise HTTPException(400, "Parte sem cenas.")
    assets_by_scene = db.list_assets_for_project(project_id)
    db.update_part(project_id, part_idx, curation_status="curated")
    parts = db.list_parts(project_id)
    all_curated = all(p.get("curation_status") == "curated" for p in parts)
    if all_curated:
        _write_full_curation_report(project, project_id, assets_by_scene)
        return RedirectResponse(f"/projects/{project_id}", status_code=303)
    next_part = next(
        (p["part_idx"] for p in parts if p.get("curation_status") != "curated"), part_idx
    )
    return RedirectResponse(f"/projects/{project_id}?part={next_part}#parts-panel", status_code=303)


@router.post("/projects/{project_id}/auto-select", responses=ERROR_RESPONSES)
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


@router.post("/scenes/{scene_db_id}/search-more", responses=ERROR_RESPONSES)
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
    existing_assets = db.list_assets(scene_db_id)
    existing = {a["download_url"] for a in existing_assets}
    custom = [k.strip() for k in str(keyword or "").split(",") if k.strip()][:5]
    search_keywords = custom or groq_service.normalized_scene_queries(scene)
    same_media_count = sum(
        1 for a in existing_assets
        if media == "all" or a.get("asset_type") == media
    )
    # O botao "+ imagens/videos" precisa sair da pagina 1; senao os provedores
    # retornam as mesmas URLs e a deduplicacao faz parecer que nada aconteceu.
    next_page = max(2, min(10, same_media_count // max(1, int(config["per_keyword"])) + 1))
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
            page=next_page,
            query_role_prefix=f"manual_{media}",
            scene=scene,
        )
    added = db.add_assets(scene_db_id, results)
    if added:
        mark_project_dirty(project["id"])
    return JSONResponse({"added": added, "media": media})


@router.post("/scenes/{scene_db_id}/refresh-assets", responses=ERROR_RESPONSES)
def refresh_assets(
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
    active_assets = db.list_assets(scene_db_id)
    seen = db.list_scene_asset_urls(scene_db_id)
    custom = [k.strip() for k in str(keyword or "").split(",") if k.strip()][:5]
    search_keywords = custom or groq_service.normalized_scene_queries(scene)
    per_keyword = int(config["per_keyword"]) + 4
    same_media_count = sum(
        1 for a in active_assets
        if media == "all" or a.get("asset_type") == media
    )
    next_page = max(2, min(10, (same_media_count + len(seen)) // max(1, per_keyword) + 1))
    with api_usage.context(user_id=user["id"], project_id=project["id"], operation="refresh_assets"):
        results = asset_search.search_scene(
            search_keywords,
            user["pexels_key"],
            user["pixabay_key"],
            max_w=max_w,
            per_keyword=per_keyword,
            allow_images=True,
            seen_urls=set(seen),
            media=media,
            coverr_key=user.get("coverr_key", ""),
            extra_image_banks=True,
            page=next_page,
            query_role_prefix=f"refresh_{media}",
            scene=scene,
        )
    if not results:
        return JSONResponse({"added": 0, "removed": 0, "media": media})
    removed = db.archive_scene_assets(scene_db_id)
    added = db.add_assets(scene_db_id, results)
    if added or removed:
        mark_project_dirty(project["id"])
    return JSONResponse({"added": added, "removed": removed, "media": media})


@router.post("/scenes/{scene_db_id}/regen-keywords", responses=ERROR_RESPONSES)
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
    rejected = [a for a in db.list_assets(scene_db_id) if a.get("state") == "rejected"]
    with api_usage.context(user_id=user["id"], project_id=project["id"], operation="regenerate_keywords"):
        kws = groq_service.regenerate_keywords(
            scene.get("narration", ""),
            scene.get("visual_goal", ""),
            user["groq_key"],
            config["visual_style"],
            model=user.get("groq_model") or groq_service.DEFAULT_MODEL,
            language=config["script_language"],
            rejected_assets=rejected,
        )
    db.update_scene_keywords(scene_db_id, kws)
    mark_project_dirty(project["id"])
    return JSONResponse({"keywords": kws})


@router.post("/scenes/{scene_db_id}/set-keywords", responses=ERROR_RESPONSES)
def set_keywords_manual(
    request: Request,
    scene_db_id: int,
    keywords: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
):
    """Salva keywords editadas manualmente, sem chamar o Groq."""
    user = require_user(request)
    verify_csrf(request, csrf_token)
    scene = db.get_scene(scene_db_id)
    if not scene:
        raise HTTPException(404)
    project = db.get_project(scene["project_id"], user["id"])
    if not project:
        raise HTTPException(404)
    ensure_project_not_busy(project)
    kws = [k.strip() for k in (keywords or "").split(",") if k.strip()][:3]
    if not kws:
        raise HTTPException(400, "Informe ao menos uma keyword.")
    db.update_scene_keywords(scene_db_id, kws)
    mark_project_dirty(project["id"])
    return JSONResponse({"keywords": kws})


@router.post("/scenes/{scene_db_id}/avatar-override", responses=ERROR_RESPONSES)
def set_avatar_override(
    request: Request,
    scene_db_id: int,
    mode: Annotated[str, Form()] = "auto",
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
    value = {"no_avatar": 1, "no_broll": -1, "auto": 0}.get(mode)
    if value is None:
        raise HTTPException(400, "modo invalido (use auto, no_avatar ou no_broll)")
    db.update_scene_broll_override(scene_db_id, value)
    mark_project_dirty(project["id"])
    return JSONResponse({"mode": mode, "broll_override": value})
