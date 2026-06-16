"""Rotas de configuração: chaves de API, transcrição de áudio."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, Request, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel

import database as db
from services import api_usage, groq_service, ops_status
from app_shared import (
    APP_ENV,
    DEFAULT_CONFIG,
    ERROR_RESPONSES,
    KEY_FIELD_LABELS,
    MAX_KEYS_FILE_BYTES,
    MAX_TRANSCRIBE_UPLOAD_MB,
    _extract_audio_bytes,
    detect_api_keys,
    mask_secret,
    read_upload_limited,
    render_template,
    require_user,
    secret_from_form,
    verify_csrf,
)

router = APIRouter()


@router.get("/settings", response_class=HTMLResponse, responses=ERROR_RESPONSES)
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


@router.get("/settings/integrations-status", responses=ERROR_RESPONSES)
def integrations_status(request: Request):
    user = require_user(request)
    return JSONResponse(ops_status.integration_snapshot(user, APP_ENV))


@router.get("/settings/test-kaggle", responses=ERROR_RESPONSES)
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
    """Campos do formulário de /settings."""
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


@router.post("/settings", responses=ERROR_RESPONSES)
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


@router.post("/settings/import-keys", responses=ERROR_RESPONSES)
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


@router.get("/static/empty.vtt", include_in_schema=False)
def empty_vtt() -> PlainTextResponse:
    return PlainTextResponse("WEBVTT\n\n", media_type="text/vtt")


@router.post("/transcribe-audio", responses=ERROR_RESPONSES)
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
