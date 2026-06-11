"""Reproduz o fluxo da tela de revisão em modo PRODUÇÃO (CSRF ativo,
cookie Secure, registro fechado) — igual ao Railway.

Simula o que o browser faz: GET /review, extrai o csrf do <meta>,
POSTa /assets/{id}/state com o header x-csrf-token (como postForm faz)
e confere se o estado persiste.

Uso: python tools/repro_review_prod.py
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

tmp = tempfile.mkdtemp(prefix="nwrch_prod_repro_")
os.environ["DATA_DIR"] = tmp
os.environ["APP_ENV"] = "production"
os.environ["APP_SECRET_KEY"] = "x" * 48  # producao exige 32+ chars
# ENFORCE_CSRF default = 1 em producao; nao sobrescrever

from starlette.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
import database as db  # noqa: E402

# https para o cookie Secure (https_only=True em producao) ser enviado
client = TestClient(app_module.app, base_url="https://testserver", follow_redirects=False)


def must(cond, msg):
    if not cond:
        raise SystemExit(f"FALHOU: {msg}")
    print(f"ok: {msg}")


def meta_csrf(html: str) -> str:
    m = re.search(r'<meta name="csrf-token" content="([^"]*)"', html)
    return m.group(1) if m else ""


SCRIPT = """[00:00.0 - 00:05.0] Primeira frase do roteiro de teste.
[00:05.0 - 00:10.0] Segunda frase do roteiro de teste.
[00:10.0 - 00:15.0] Terceira frase do roteiro de teste."""

with client:
    # registro do primeiro usuario (ALLOW_FIRST_USER) exige csrf da pagina
    r = client.get("/login")
    token = meta_csrf(r.text)
    must(token, f"csrf token presente na pagina de login ({token[:8]}...)")

    r = client.post("/register", data={
        "username": "produser", "password": "prodpassword1", "csrf_token": token,
    })
    must(r.status_code == 303, f"registro do primeiro usuario (HTTP {r.status_code})")

    r = client.get("/projects")
    must(r.status_code == 200, "logado apos registro")
    token = meta_csrf(r.text)

    r = client.post("/projects/new", data={
        "name": "repro prod", "script": SCRIPT, "avatar_safe_area": "right",
        "visual_style": "documental", "resolution": "1280x720",
        "scene_duration": "5.0", "csrf_token": token,
    })
    must(r.status_code == 303, f"projeto criado (HTTP {r.status_code})")
    pid = int(r.headers["location"].rsplit("/", 1)[-1])

    # injeta cenas + assets fake direto no banco (sem APIs externas)
    import services.script_parser as sp
    scenes = sp.parse_script(SCRIPT, scene_duration=5.0)
    for s in scenes:
        s["keywords"] = ["test"]
        s["visual_goal"] = "test"
    db.replace_scenes(pid, scenes)
    db.set_project_status(pid, "reviewing")
    for scene in db.list_scenes(pid):
        db.add_assets(scene["id"], [{
            "source": "pixabay", "source_id": f"a{scene['id']}", "asset_type": "image",
            "preview_url": "https://example.com/p.jpg", "download_url": "https://example.com/d.jpg",
            "page_url": "", "width": 1920, "height": 1080, "duration": 0,
            "keyword": "test", "author": "tester", "author_url": "",
        }])
    for a in db.list_assets_by_state(pid, ["pending"]):
        db.set_asset_state(a["id"], "selected", auto_score=5.0, auto_reason="repro")

    # --- abre a tela de revisao como o browser ---
    r = client.get(f"/projects/{pid}/review")
    must(r.status_code == 200, f"GET /review (HTTP {r.status_code})")
    must(r.headers.get("cache-control") == "no-store", "HTML de revisao enviado com Cache-Control: no-store")
    html = r.text
    token = meta_csrf(html)
    must(token, "csrf token no <meta> da pagina de revisao")
    must(
        'class="review-decision"' in html and 'name="redirect"' in html and 'type="submit"' in html,
        "decisoes renderizam forms reais com fallback sem JS",
    )

    chosen = db.list_assets_by_state(pid, ["selected"])
    must(len(chosen) == 3, f"3 takes selecionados (tem {len(chosen)})")

    # --- clique em Aceitar: POST igual ao postForm (body + header) ---
    aid = chosen[0]["id"]
    r = client.post(
        f"/assets/{aid}/state",
        data={"state": "accepted", "csrf_token": token},
        headers={"x-csrf-token": token},
    )
    print(f"   resposta: HTTP {r.status_code} {r.text[:120]}")
    must(r.status_code == 200, f"aceitar take responde 200 (HTTP {r.status_code})")
    must(r.json()["state"] == "accepted", "estado retornado = accepted")

    # postForm NAO inclui csrf no body quando ja vai no header? inclui ambos;
    # mas teste tambem somente com header (caminho do verify_csrf via header)
    aid2 = chosen[1]["id"]
    r = client.post(
        f"/assets/{aid2}/state",
        data={"state": "accepted"},
        headers={"x-csrf-token": token},
    )
    must(r.status_code == 200, f"aceitar so com header x-csrf-token (HTTP {r.status_code})")

    # --- persistencia: recarrega a pagina como o usuario faria (F5) ---
    fresh = db.get_asset(aid) if hasattr(db, "get_asset") else None
    accepted_now = db.list_assets_by_state(pid, ["accepted"])
    must(len(accepted_now) == 2, f"2 takes persistidos como accepted (tem {len(accepted_now)})")
    r = client.get(f"/projects/{pid}/review")
    must(r.text.count("✓ Aceito") >= 2, "F5 na pagina mostra os takes aceitos")

    # --- rejeitar o terceiro ---
    aid3 = chosen[2]["id"]
    r = client.post(
        f"/assets/{aid3}/state",
        data={"state": "rejected", "csrf_token": token},
        headers={"x-csrf-token": token},
    )
    must(r.status_code == 200, f"rejeitar take responde 200 (HTTP {r.status_code})")

    # --- fallback sem JS: POST de form nativo redireciona de volta para /review ---
    r = client.post(
        f"/assets/{aid3}/state",
        data={
            "state": "accepted",
            "csrf_token": token,
            "redirect": f"/projects/{pid}/review",
        },
    )
    must(r.status_code == 303, f"form nativo redireciona apos aceitar (HTTP {r.status_code})")
    must(r.headers["location"] == f"/projects/{pid}/review", "redirect seguro volta para a revisao")
    must(db.get_asset(aid3)["state"] == "accepted", "fallback nativo persiste o estado")

    # --- POST sem csrf nenhum deve dar 403 (CSRF realmente ativo) ---
    r = client.post(f"/assets/{aid3}/state", data={"state": "rejected"})
    must(r.status_code == 403, f"sem csrf -> 403 (HTTP {r.status_code}) — CSRF esta ativo")

    # --- concluir cria relatorio; mexer depois reabre revisao e remove relatorio antigo ---
    r = client.get(f"/projects/{pid}/review")
    token = meta_csrf(r.text)
    r = client.post(f"/projects/{pid}/finish-review", data={"csrf_token": token})
    must(r.status_code == 303, f"concluir revisao com 3 aceitos (HTTP {r.status_code})")
    report_path = app_module.curation_report_path(pid)
    must(db.get_project(pid, 1)["status"] == "reviewed", "status reviewed apos concluir")
    must(report_path.exists(), "relatorio criado apos concluir")

    r = client.get(f"/projects/{pid}/review")
    token = meta_csrf(r.text)
    must("curation-report" in r.text, "relatorio aparece somente quando revisao esta concluida")
    r = client.post(
        f"/assets/{aid3}/state",
        data={
            "state": "rejected",
            "csrf_token": token,
            "redirect": f"/projects/{pid}/review",
        },
    )
    must(r.status_code == 303, f"alterar revisao concluida redireciona (HTTP {r.status_code})")
    must(db.get_project(pid, 1)["status"] == "reviewing", "alterar take reabre a revisao")
    must(not report_path.exists(), "relatorio antigo removido ao reabrir revisao")
    r = client.get(f"/projects/{pid}/review")
    must("curation-report" not in r.text, "relatorio antigo nao aparece na revisao reaberta")

    # dump do HTML renderizado para inspecao manual
    r = client.get(f"/projects/{pid}/review")
    out = Path(tmp) / "review_rendered.html"
    out.write_text(r.text, encoding="utf-8")
    print(f"\nHTML salvo em: {out}")

print("\nREPRO PROD: backend respondeu corretamente em todos os casos.")
