"""Rotas de autenticação: login, registro, logout."""
from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import APIRouter, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

import database as db
from app_shared import (
    ERROR_RESPONSES,
    INVITE_CODE,
    PROJECTS_PATH,
    current_user,
    registration_state,
    render_template,
    require_user,
    safe_next_url,
    verify_csrf,
)

router = APIRouter()


@router.get("/", response_class=HTMLResponse, responses=ERROR_RESPONSES)
def home(request: Request):
    if current_user(request):
        return RedirectResponse(PROJECTS_PATH, status_code=303)
    return RedirectResponse("/login", status_code=303)


@router.get("/login", response_class=HTMLResponse, responses=ERROR_RESPONSES)
def login_page(request: Request, error: str = "", next: str = ""):
    if current_user(request):
        return RedirectResponse(PROJECTS_PATH, status_code=303)
    return render_template(request, "login.html", {"error": error, "next": next})


@router.post("/login", responses=ERROR_RESPONSES)
def login(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    verify_csrf(request, csrf_token)
    user = db.get_user_by_name(username.strip())
    if not user or not db.verify_password(password, user["password_hash"]):
        return RedirectResponse("/login?error=Credenciais+invalidas", status_code=303)
    request.session.clear()
    request.session["session_id"] = db.create_login_session(user["id"])
    return RedirectResponse(safe_next_url(next), status_code=303)


@router.post("/register", responses=ERROR_RESPONSES)
def register(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    invite_code: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
):
    verify_csrf(request, csrf_token)
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
    request.session["session_id"] = db.create_login_session(uid)
    return RedirectResponse("/settings", status_code=303)


@router.get("/logout", responses=ERROR_RESPONSES)
def logout(request: Request):
    db.revoke_login_session(request.session.get("session_id", ""))
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
