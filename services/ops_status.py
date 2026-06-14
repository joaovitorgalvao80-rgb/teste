"""Derived operational status for integrations, projects, and jobs.

This module is intentionally read-only: it turns the current database/app state
into UI/API summaries without changing workflow state.
"""
from __future__ import annotations

import os
import time
from typing import Optional


ACTIVE_JOB_STATUSES = {"queued", "running", "canceling"}
FAILURE_PROJECT_STATUSES = {"map_failed", "search_failed", "package_failed"}


def _has(value: object) -> bool:
    return bool(str(value or "").strip())


def _duration_label(seconds: float) -> str:
    seconds = max(0, int(seconds or 0))
    if seconds < 60:
        return f"{seconds}s"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def _ago_label(seconds: float) -> str:
    seconds = max(0, int(seconds or 0))
    if seconds < 10:
        return "agora"
    if seconds < 60:
        return f"ha {seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"ha {minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"ha {hours}h"
    return f"ha {hours // 24}d"


def decorate_job(job: dict, now: Optional[float] = None) -> dict:
    """Adds human-facing timing fields to a job dict."""
    now = time.time() if now is None else now
    out = dict(job)
    created = float(out.get("created_at") or now)
    updated = float(out.get("updated_at") or created)
    finished = float(out.get("finished_at") or 0)
    end = finished or updated or now
    out["elapsed_seconds"] = max(0, round(end - created, 3))
    out["updated_ago_seconds"] = max(0, round(now - updated, 3))
    out["elapsed_label"] = _duration_label(out["elapsed_seconds"])
    out["updated_label"] = _ago_label(out["updated_ago_seconds"])
    return out


def decorate_jobs(jobs: list[dict], now: Optional[float] = None) -> list[dict]:
    now = time.time() if now is None else now
    return [decorate_job(job, now=now) for job in jobs]


def _item(key: str, label: str, configured: bool, required: bool, detail: str, kind: str = "secret") -> dict:
    status = "ok" if configured else ("missing" if required else "optional")
    return {
        "key": key,
        "label": label,
        "configured": configured,
        "required": required,
        "status": status,
        "detail": detail,
        "kind": kind,
    }


def integration_snapshot(user: dict, app_env: str = "dev") -> dict:
    """Returns a masked readiness snapshot. Raw secret values never leave here."""
    has_pexels = _has(user.get("pexels_key"))
    has_pixabay = _has(user.get("pixabay_key"))
    has_coverr = _has(user.get("coverr_key"))
    has_groq = _has(user.get("groq_key"))
    has_nvidia = _has(user.get("nvidia_key"))
    has_kaggle_user = _has(user.get("kaggle_username"))
    has_kaggle_token = _has(user.get("kaggle_token"))
    has_asset_provider = has_pexels or has_pixabay or has_coverr
    has_kaggle = has_kaggle_user and has_kaggle_token

    app_secret = os.getenv("APP_SECRET_KEY", "")
    secret_strong = app_env != "production" or (len(app_secret) >= 32 and "change" not in app_secret.lower())

    groups = [
        {
            "key": "assets",
            "label": "Bibliotecas visuais",
            "status": "ok" if has_asset_provider else "missing",
            "summary": "Busca de assets pronta" if has_asset_provider else "configure Pexels, Pixabay ou Coverr",
            "items": [
                _item("pexels", "Pexels", has_pexels, False, "video e imagem stock"),
                _item("pixabay", "Pixabay", has_pixabay, False, "fallback de video/imagem"),
                _item("coverr", "Coverr", has_coverr, False, "video curado, limite menor"),
                _item("openverse", "Openverse", True, False, "fallback publico sem chave", "public"),
                _item("wikimedia", "Wikimedia", True, False, "acervo publico sem chave", "public"),
            ],
        },
        {
            "key": "ai",
            "label": "IA e visao",
            "status": "ok" if has_groq else "missing",
            "summary": "Mapa visual e transcricao prontos" if has_groq else "Groq e necessario para mapa/transcricao",
            "items": [
                _item("groq", "Groq", has_groq, True, "mapa visual, keywords e transcricao"),
                _item("nvidia", "NVIDIA", has_nvidia, False, "segunda opiniao de visao"),
            ],
        },
        {
            "key": "render",
            "label": "Render",
            "status": "ok" if has_kaggle else "missing",
            "summary": "Kaggle pronto para render" if has_kaggle else "username e token Kaggle pendentes",
            "items": [
                _item("kaggle_username", "Kaggle username", has_kaggle_user, True, "identifica a conta"),
                _item("kaggle_token", "Kaggle token", has_kaggle_token, True, "autoriza dataset/kernel"),
            ],
        },
        {
            "key": "security",
            "label": "Seguranca",
            "status": "ok" if secret_strong else "warn",
            "summary": "segredos criptografados" if secret_strong else "APP_SECRET_KEY fraca/ausente em producao",
            "items": [
                _item("app_secret", "APP_SECRET_KEY", secret_strong, app_env == "production", "criptografia local de segredos", "env"),
            ],
        },
    ]
    required_missing = [
        item["label"]
        for group in groups
        for item in group["items"]
        if item["required"] and not item["configured"]
    ]
    warnings = []
    if has_asset_provider and not (has_pexels and has_pixabay):
        warnings.append("Use pelo menos dois provedores visuais para reduzir busca vazia.")
    if has_groq and not has_nvidia:
        warnings.append("NVIDIA e opcional, mas melhora a segunda opiniao de visao.")
    return {
        "ready": not required_missing,
        "required_missing": required_missing,
        "warnings": warnings,
        "groups": groups,
        "security_note": "Se uma chave foi colada em chat, log ou print, gere uma chave nova no provedor.",
    }


def project_state(
    project: dict,
    *,
    scenes: list[dict],
    asset_count: int,
    curation_stats: dict,
    jobs: list[dict],
    parts: list[dict],
    outputs: dict,
    diagnostics: dict,
    has_asset_keys: bool,
) -> dict:
    """Computes the next operational state from authoritative current state."""
    active = next((job for job in jobs if job.get("status") in ACTIVE_JOB_STATUSES), None)
    status = project.get("status") or "created"
    if active:
        return {
            "code": "processing",
            "severity": "running",
            "label": active.get("message") or "Tarefa em execucao",
            "detail": f"{active.get('kind', 'job')} esta {active.get('status')}",
            "next_action": "acompanhe ou pare o job especifico",
        }
    if status in FAILURE_PROJECT_STATUSES:
        return {
            "code": status,
            "severity": "error",
            "label": "Fluxo interrompido",
            "detail": "A ultima etapa falhou; veja Jobs recentes e tente novamente.",
            "next_action": "corrigir erro e repetir a etapa",
        }
    if not scenes:
        return {
            "code": "needs_map",
            "severity": "todo",
            "label": "Mapa visual pendente",
            "detail": "O roteiro ainda nao foi dividido em cenas.",
            "next_action": "gerar mapa visual",
        }
    if curation_stats.get("required", 0) <= 0:
        return {
            "code": "avatar_only",
            "severity": "warn",
            "label": "Sem cenas de b-roll",
            "detail": "Todas as cenas atuais ficaram como avatar-only.",
            "next_action": "revise o roteiro ou adicione cenas intermediarias",
        }
    if not has_asset_keys:
        return {
            "code": "needs_asset_keys",
            "severity": "blocked",
            "label": "Busca sem provedor configurado",
            "detail": "Cadastre Pexels, Pixabay ou Coverr para buscar assets.",
            "next_action": "configurar bibliotecas visuais",
        }
    if asset_count == 0:
        return {
            "code": "needs_search",
            "severity": "todo",
            "label": "Assets ainda nao buscados",
            "detail": "As cenas existem, mas ainda nao ha candidatos para curadoria.",
            "next_action": "buscar assets",
        }
    required = int(curation_stats.get("required") or 0)
    selected = int(curation_stats.get("selected") or 0)
    accepted = int(curation_stats.get("accepted") or 0)
    if selected < required and accepted < required:
        return {
            "code": "needs_selection",
            "severity": "todo",
            "label": "Curadoria incompleta",
            "detail": f"{selected}/{required} cenas de b-roll tem take escolhido.",
            "next_action": "selecionar automaticamente ou escolher manualmente",
        }
    if accepted < required and status != "reviewed":
        return {
            "code": "needs_review",
            "severity": "todo",
            "label": "Revisao pendente",
            "detail": f"{accepted}/{required} b-rolls aceitos.",
            "next_action": "abrir revisao e aceitar os takes",
        }
    if status in {"reviewed", "needs_package", "package_failed"}:
        return {
            "code": "ready_to_package",
            "severity": "ready",
            "label": "Pronto para pacote",
            "detail": "A curadoria obrigatoria esta completa.",
            "next_action": "gerar pacote",
        }
    if status == "packaged":
        if outputs.get("master") or outputs.get("base"):
            validation = (diagnostics.get("outputs") or {}).get("validation") or {}
            if validation.get("status") == "ok":
                return {
                    "code": "delivered",
                    "severity": "ok",
                    "label": "Video validado",
                    "detail": "Ha output local validado.",
                    "next_action": "baixar master/base",
                }
            return {
                "code": "needs_output_validation",
                "severity": "warn",
                "label": "Output local encontrado",
                "detail": "Valide duracao, streams e master antes de entregar.",
                "next_action": "validar outputs",
            }
        return {
            "code": "ready_to_render",
            "severity": "ready",
            "label": "Pacote pronto",
            "detail": "O ZIP/partes estao prontos para render.",
            "next_action": "enviar para Kaggle",
        }
    if parts:
        pending_parts = [p for p in parts if p.get("curation_status") != "curated"]
        if pending_parts:
            return {
                "code": "needs_part_curation",
                "severity": "todo",
                "label": "Curadoria por parte pendente",
                "detail": f"{len(pending_parts)} parte(s) ainda precisam de revisao.",
                "next_action": "curar a proxima parte",
            }
    return {
        "code": "in_progress",
        "severity": "todo",
        "label": "Fluxo em andamento",
        "detail": f"Status atual: {status}.",
        "next_action": "seguir o proximo passo habilitado",
    }
