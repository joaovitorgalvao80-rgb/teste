"""Rotas de pacote, Kaggle, diagnósticos e downloads."""
from __future__ import annotations

import json
import random
import shutil
import zipfile
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Form, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

import database as db
from services import diagnostics, edit_plan, kaggle_service
from services.project_config import project_config
from app_shared import (
    ACTIVE_JOB_STATUSES,
    CHOSEN_ASSET_STATES,
    EDIT_PLAN_FILENAME,
    ERROR_RESPONSES,
    MEDIA_TYPE_JSON,
    MEDIA_TYPE_MP4,
    _asset_quality_issues,
    _enrich_complete_kaggle_status,
    annotate_assets_with_vision,
    annotate_broll_requirements,
    api_usage,
    curation_report_path,
    ensure_no_active_job,
    ensure_project_not_busy,
    expected_duration_from_scenes,
    has_visual_provider,
    latest_kaggle_video,
    latest_zip,
    local_output_videos,
    problem_scenes,
    project_output_file,
    project_work_dir,
    render_template,
    require_user,
    run_concat_job,
    run_kaggle_parts_job,
    run_kaggle_send_job,
    run_package_job,
    scene_broll_flags,
    verify_csrf,
    ops_status,
)

router = APIRouter()


@router.get("/projects/{project_id}/quality-warnings", responses=ERROR_RESPONSES)
def quality_warnings(request: Request, project_id: int):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    config = project_config(project)
    from services.project_config import resolution_width
    target_w = resolution_width(config)
    selected_rows = db.list_assets_by_state(project_id, CHOSEN_ASSET_STATES)
    scenes = {s["id"]: s for s in db.list_scenes(project_id)}
    warnings = []
    for row in selected_rows:
        scene = scenes.get(row.get("scene_id"))
        scene_dur = float(scene.get("duration") or 4.0) if scene else 4.0
        scene_code = scene.get("scene_id", "?") if scene else "?"
        issues = _asset_quality_issues(row, scene_dur, target_w)
        if issues:
            warnings.append({"scene_id": scene_code, "issues": issues})
    return JSONResponse({"warnings": warnings, "total": len(warnings)})


@router.post("/projects/{project_id}/package", responses=ERROR_RESPONSES)
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
    config = project_config(project)
    scenes = db.list_scenes(project_id)
    broll_required = {sid for sid, on in scene_broll_flags(scenes, config).items() if on}
    selected_rows = db.list_assets_by_state(project_id, CHOSEN_ASSET_STATES)
    selected_by_scene = {row["scene_id"]: row for row in selected_rows}
    required_scene_db_ids = {s["id"] for s in scenes if s["scene_id"] in broll_required}
    if not broll_required and not selected_by_scene:
        raise HTTPException(400, "O plano atual nao tem cenas de b-roll para empacotar.")
    if config.get("missing_visual_policy") == "block_package":
        missing = [s["scene_id"] for s in scenes if s["id"] in required_scene_db_ids and s["id"] not in selected_by_scene]
        if missing:
            preview = ", ".join(missing[:8])
            suffix = "..." if len(missing) > 8 else ""
            raise HTTPException(400, f"Faltam takes obrigatorios: {preview}{suffix}")
    if broll_required and not any(scene_id in selected_by_scene for scene_id in required_scene_db_ids):
        raise HTTPException(400, "Selecione ao menos um asset antes de gerar o pacote.")
    ensure_no_active_job(project_id, "package")
    job_id = db.create_job(user["id"], "package", project_id, "Preparando pacote ZIP")
    db.set_project_status(project_id, "packaging")
    background_tasks.add_task(run_package_job, job_id, project_id, user["id"])
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.get("/projects/{project_id}/download-zip", responses=ERROR_RESPONSES)
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


@router.get("/projects/{project_id}/edit-plan", responses=ERROR_RESPONSES)
def get_edit_plan(request: Request, project_id: int):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    from app_shared import local_edit_plan
    plan = local_edit_plan(project_id)
    if not plan:
        raise HTTPException(404, "Plano de edição não encontrado. Gere o pacote (etapa 03) primeiro.")
    return JSONResponse(plan)


def _edit_plan_path(project_id: int):
    return project_work_dir(project_id) / EDIT_PLAN_FILENAME


def _write_edit_plan(project_id: int, plan: dict) -> None:
    plan_path = _edit_plan_path(project_id)
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(plan, ensure_ascii=False, indent=2)
    plan_path.write_text(payload, encoding="utf-8")
    zip_path = latest_zip(plan_path.parent)
    if not zip_path:
        return
    tmp_path = zip_path.with_suffix(zip_path.suffix + ".tmp")
    with zipfile.ZipFile(zip_path, "r") as src, zipfile.ZipFile(tmp_path, "w") as dst:
        for item in src.infolist():
            if item.filename == EDIT_PLAN_FILENAME:
                continue
            dst.writestr(item, src.read(item.filename))
        dst.writestr(EDIT_PLAN_FILENAME, payload)
    shutil.move(str(tmp_path), str(zip_path))


def _load_edit_plan_for_update(project_id: int) -> dict:
    plan_path = _edit_plan_path(project_id)
    if not plan_path.exists():
        raise HTTPException(404, "Plano de edição não encontrado. Gere o pacote primeiro.")
    try:
        return json.loads(plan_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise HTTPException(400, "Plano de edição inválido.") from exc


def _find_plan_scene(plan: dict, scene_id: str) -> dict:
    for scene in plan.get("scenes") or []:
        if str(scene.get("scene_id")) == scene_id:
            return scene
    raise HTTPException(404, "Cena não encontrada no plano.")


def _random_motion(current: str = "") -> str:
    options = [m for m in edit_plan.MOTION_OPTIONS if m != current]
    return random.choice(options or list(edit_plan.MOTION_OPTIONS))


@router.post("/projects/{project_id}/edit-plan/scenes/{scene_id}", responses=ERROR_RESPONSES)
def update_edit_plan_scene(
    request: Request,
    project_id: int,
    scene_id: str,
    motion: Annotated[str, Form()] = "",
    caption: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    user = require_user(request)
    verify_csrf(request, csrf_token)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    ensure_project_not_busy(project)
    plan = _load_edit_plan_for_update(project_id)
    scene = _find_plan_scene(plan, scene_id)
    clean_motion = str(motion or "").strip()
    if clean_motion not in edit_plan.MOTION_OPTIONS:
        raise HTTPException(400, "Motion inválido.")
    scene["motion"] = clean_motion
    scene["motion_source"] = "manual"
    scene["motion_locked"] = True
    clean_caption = str(caption or "").replace("\n", " ").strip()[:80]
    scene["caption"] = clean_caption
    if clean_caption:
        start = float(scene.get("start") or 0)
        duration = float(scene.get("duration") or 0)
        cap_start, cap_duration = edit_plan._caption_timing(start, duration)
        scene["caption_start"] = cap_start
        scene["caption_duration"] = cap_duration
    else:
        scene["caption_start"] = None
        scene["caption_duration"] = 0
    _write_edit_plan(project_id, plan)
    return RedirectResponse(f"/projects/{project_id}#edit-plan-panel", status_code=303)


@router.post("/projects/{project_id}/edit-plan/scenes/{scene_id}/random-motion", responses=ERROR_RESPONSES)
def randomize_edit_plan_scene_motion(
    request: Request,
    project_id: int,
    scene_id: str,
    csrf_token: Annotated[str, Form()] = "",
):
    user = require_user(request)
    verify_csrf(request, csrf_token)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    ensure_project_not_busy(project)
    plan = _load_edit_plan_for_update(project_id)
    scene = _find_plan_scene(plan, scene_id)
    scene["motion"] = _random_motion(str(scene.get("motion") or ""))
    scene["motion_source"] = "random"
    scene["motion_locked"] = False
    _write_edit_plan(project_id, plan)
    return RedirectResponse(f"/projects/{project_id}#edit-plan-panel", status_code=303)


@router.post("/projects/{project_id}/edit-plan/random-motions", responses=ERROR_RESPONSES)
def randomize_edit_plan_motions(
    request: Request,
    project_id: int,
    csrf_token: Annotated[str, Form()] = "",
):
    user = require_user(request)
    verify_csrf(request, csrf_token)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    ensure_project_not_busy(project)
    plan = _load_edit_plan_for_update(project_id)
    changed = 0
    for scene in plan.get("scenes") or []:
        if scene.get("motion_locked"):
            continue
        scene["motion"] = _random_motion(str(scene.get("motion") or ""))
        scene["motion_source"] = "random"
        changed += 1
    plan["motion_policy"] = {
        "last_randomized": changed,
        "manual_locked": sum(1 for scene in plan.get("scenes") or [] if scene.get("motion_locked")),
    }
    _write_edit_plan(project_id, plan)
    return RedirectResponse(f"/projects/{project_id}#edit-plan-panel", status_code=303)


@router.post("/projects/{project_id}/send-to-kaggle", responses=ERROR_RESPONSES)
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


@router.post("/projects/{project_id}/render-parts", responses=ERROR_RESPONSES)
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


@router.post("/projects/{project_id}/concat-parts", responses=ERROR_RESPONSES)
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


@router.get("/projects/{project_id}/parts-status", responses=ERROR_RESPONSES)
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


@router.get("/jobs/{job_id}", responses=ERROR_RESPONSES)
def job_status(request: Request, job_id: int):
    user = require_user(request)
    job = db.get_job(job_id, user["id"])
    if not job:
        raise HTTPException(404)
    return JSONResponse(job)


@router.post("/jobs/{job_id}/cancel", responses=ERROR_RESPONSES)
def cancel_job(request: Request, job_id: int, csrf_token: Annotated[str, Form()] = ""):
    user = require_user(request)
    verify_csrf(request, csrf_token)
    job = db.request_job_cancel(job_id, user["id"])
    if not job:
        raise HTTPException(404)
    if job["status"] not in ACTIVE_JOB_STATUSES:
        raise HTTPException(409, f"Job ja esta {job['status']}.")
    return JSONResponse(job)


@router.get("/projects/{project_id}/jobs", responses=ERROR_RESPONSES)
def project_jobs(request: Request, project_id: int):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    return JSONResponse({"project_status": project.get("status", ""), "jobs": db.list_project_jobs(project_id, user["id"])})


@router.get("/projects/{project_id}/diagnostics.json", responses=ERROR_RESPONSES)
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


@router.post("/projects/{project_id}/validate-output", responses=ERROR_RESPONSES)
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


@router.get("/projects/{project_id}/kaggle-status", responses=ERROR_RESPONSES)
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


@router.get("/projects/{project_id}/download-kaggle-video", responses=ERROR_RESPONSES)
def download_kaggle_video(request: Request, project_id: int):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    video = latest_kaggle_video(project_work_dir(project_id))
    if not video:
        raise HTTPException(404, "Video do Kaggle ainda nao baixado.")
    return FileResponse(video, filename=video.name, media_type=MEDIA_TYPE_MP4)


@router.get("/projects/{project_id}/download-base-video", responses=ERROR_RESPONSES)
def download_base_video(request: Request, project_id: int):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    video = local_output_videos(project_work_dir(project_id))["base"]
    if not video:
        raise HTTPException(404, "Video base ainda nao baixado.")
    return FileResponse(video, filename=video.name, media_type=MEDIA_TYPE_MP4)


@router.get("/projects/{project_id}/download-master-video", responses=ERROR_RESPONSES)
def download_master_video(request: Request, project_id: int):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    video = local_output_videos(project_work_dir(project_id))["master"]
    if not video:
        raise HTTPException(404, "Video master ainda nao renderizado.")
    return FileResponse(video, filename=video.name, media_type=MEDIA_TYPE_MP4)


@router.get("/projects/{project_id}/download-render-log", responses=ERROR_RESPONSES)
def download_render_log(request: Request, project_id: int):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    path = project_output_file(project_id, "log_render.txt")
    if not path.exists():
        raise HTTPException(404, "Log de render ainda nao encontrado.")
    return FileResponse(path, filename=path.name, media_type="text/plain")


@router.get("/projects/{project_id}/download-validation", responses=ERROR_RESPONSES)
def download_validation(request: Request, project_id: int):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    path = diagnostics.validation_path(project_work_dir(project_id))
    if not path.exists():
        raise HTTPException(404, "Validacao ainda nao gerada.")
    return FileResponse(path, filename=path.name, media_type=MEDIA_TYPE_JSON)


@router.get("/projects/{project_id}/download-hyperframes-status", responses=ERROR_RESPONSES)
def download_hyperframes_status(request: Request, project_id: int):
    user = require_user(request)
    project = db.get_project(project_id, user["id"])
    if not project:
        raise HTTPException(404)
    path = project_output_file(project_id, "hyperframes_status.json")
    if not path.exists():
        raise HTTPException(404, "Status HyperFrames ainda nao encontrado.")
    return FileResponse(path, filename=path.name, media_type=MEDIA_TYPE_JSON)
