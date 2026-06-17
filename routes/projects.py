"""Rotas de projetos: CRUD, mapa visual, transcriçao."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Request, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

import database as db
from services import api_usage, groq_service, ops_status
from services.project_config import DEFAULT_CONFIG, LANGUAGES, ALLOWED_BROLL_DENSITIES, ALLOWED_VIDEO_STYLES, _coerce_bool, normalize_project_config, project_config, resolution_width
from app_shared import (
    ACTIVE_JOB_STATUSES,
    CHOSEN_ASSET_STATES,
    ERROR_RESPONSES,
    MAX_TRANSCRIBE_UPLOAD_MB,
    MSG_NO_API_KEYS,
    PROJECTS_PATH,
    _take_sort_key,
    annotate_assets_with_vision,
    annotate_broll_requirements,
    ensure_no_active_job,
    ensure_project_not_busy,
    expected_duration_from_scenes,
    find_input_media,
    has_visual_provider,
    local_edit_plan,
    local_editorial_report,
    local_hyperframes_status,
    local_output_videos,
    mark_project_dirty,
    prepare_narration_media,
    problem_scenes,
    project_diagnostics_snapshot,
    project_work_dir,
    read_upload_limited,
    remove_project_workspace,
    render_template,
    require_user,
    run_generate_map_job,
    save_input_media_bytes,
    scene_broll_flags,
    verify_csrf,
)

router = APIRouter()


@router.get("/projects", response_class=HTMLResponse, responses=ERROR_RESPONSES)
def projects_page(request: Request):
    user = require_user(request)
    projects = db.list_projects(user["id"])
    return render_template(request, "projects.html", {"user": user, "projects": projects})


@dataclass
class NewProjectConfig:
    avatar_safe_area: Annotated[str, Form()] = "right"
    visual_style: Annotated[str, Form()] = DEFAULT_CONFIG["visual_style"]
    resolution: Annotated[str, Form()] = "1920x1080"
    scene_duration: Annotated[float, Form()] = 4.0
    image_fallback: Annotated[str, Form()] = ""
    long_mode: Annotated[str, Form()] = ""
    script_language: Annotated[str, Form()] = DEFAULT_CONFIG["script_language"]
    broll_density: Annotated[str, Form()] = DEFAULT_CONFIG["broll_density"]
    video_style: Annotated[str, Form()] = DEFAULT_CONFIG["video_style"]


@router.get("/projects/new", response_class=HTMLResponse, responses=ERROR_RESPONSES)
def new_project_page(request: Request):
    user = require_user(request)
    return render_template(request, "new_project.html", {"user": user, "config": DEFAULT_CONFIG, "languages": LANGUAGES})


@router.post("/projects/new", responses=ERROR_RESPONSES)
async def new_project(
    request: Request,
    name: Annotated[str, Form()],
    script: Annotated[str, Form()],
    cfg: Annotated[NewProjectConfig, Depends()],
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
    is_long = _coerce_bool(cfg.long_mode)
    scene_duration = cfg.scene_duration
    if is_long and scene_duration == DEFAULT_CONFIG["scene_duration"]:
        scene_duration = 7.0
    config = normalize_project_config({
        "avatar_safe_area": cfg.avatar_safe_area,
        "visual_style": cfg.visual_style.strip() or DEFAULT_CONFIG["visual_style"],
        "resolution": cfg.resolution,
        "scene_duration": scene_duration,
        "image_fallback": cfg.image_fallback,
        "long_mode": is_long,
        "script_language": cfg.script_language,
        "broll_density": cfg.broll_density,
        "video_style": cfg.video_style,
    })
    pid = db.create_project(user["id"], name.strip() or "projeto", script, config)
    if prepared_narration:
        save_input_media_bytes(pid, "narration", prepared_narration[0], prepared_narration[1])
    return RedirectResponse(f"/projects/{pid}", status_code=303)


@router.post("/projects/{project_id}/delete", responses=ERROR_RESPONSES)
def delete_project(request: Request, project_id: int, csrf_token: Annotated[str, Form()] = ""):
    user = require_user(request)
    verify_csrf(request, csrf_token)
    project = db.get_project(project_id, user["id"])
    if project:
        ensure_project_not_busy(project)
        db.delete_project(project_id, user["id"])
        remove_project_workspace(project_id)
    return RedirectResponse(PROJECTS_PATH, status_code=303)


@router.post("/projects/{project_id}/update-style", responses=ERROR_RESPONSES)
def update_project_style(
    request: Request,
    project_id: int,
    broll_density: Annotated[str, Form()] = "",
    video_style: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    user = require_user(request)
    verify_csrf(request, csrf_token)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    cfg = project_config(project)
    if broll_density and broll_density in ALLOWED_BROLL_DENSITIES:
        cfg["broll_density"] = broll_density
    if video_style and video_style in ALLOWED_VIDEO_STYLES:
        cfg["video_style"] = video_style
    db.set_project_config(project_id, cfg)
    mark_project_dirty(project_id)
    return RedirectResponse(f"/projects/{project_id}#refinamento", status_code=303)


@router.get("/projects/{project_id}", response_class=HTMLResponse, responses=ERROR_RESPONSES)
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
        s["asset_pool_total"] = len(annotated)
        s["good_candidate_count"] = sum(
            1 for a in annotated
            if not a.get("low_relevance") and a.get("vision_verdict") != "descartar"
        )
        s["pool_fraco"] = bool(annotated) and s["good_candidate_count"] < 6
        s["assets"] = annotated[:12]
        s["selected"] = next((a for a in annotated if a["state"] in CHOSEN_ASSET_STATES), None)
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
            "editorial_report": local_editorial_report(project_id),
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


@router.post("/projects/{project_id}/generate-map", responses=ERROR_RESPONSES)
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
