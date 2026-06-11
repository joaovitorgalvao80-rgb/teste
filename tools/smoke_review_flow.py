"""Smoke test do fluxo de revisão (Frente B) sem APIs externas.

Roda com um banco temporário: cria usuário, projeto, cenas e assets fake,
auto-seleciona via heurística (sem chave Groq), aceita/rejeita na revisão,
re-busca é pulada (sem chaves) e gera o relatório.

Uso: python tools/smoke_review_flow.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

tmp = tempfile.mkdtemp(prefix="nwrch_smoke_")
os.environ["DATA_DIR"] = tmp
os.environ["APP_ENV"] = "dev"
os.environ["ENFORCE_CSRF"] = "0"

from starlette.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
import database as db  # noqa: E402

client = TestClient(app_module.app, follow_redirects=False)


def must(cond, msg):
    if not cond:
        raise SystemExit(f"FALHOU: {msg}")
    print(f"ok: {msg}")


with client:  # dispara lifespan (init_db etc.)
    # registro + login
    r = client.post("/register", data={"username": "smoke", "password": "smokepass123"})
    must(r.status_code == 303, f"registro (HTTP {r.status_code})")

    # cria projeto com roteiro com timestamps
    script = "\n".join(
        f"[00:{i*4:02d}.0 - 00:{i*4+4:02d}.0] Cena de teste numero {i + 1} sobre mosquito e agua parada."
        for i in range(3)
    )
    r = client.post("/projects/new", data={
        "name": "smoke review",
        "script": script,
        "avatar_safe_area": "right",
        "visual_style": "documental",
        "resolution": "1280x720",
        "scene_duration": "4.0",
    })
    must(r.status_code == 303, "criar projeto")
    pid = int(r.headers["location"].rsplit("/", 1)[-1])

    # mapa visual roda sincrono no TestClient (BackgroundTasks após resposta)
    r = client.post(f"/projects/{pid}/generate-map")
    must(r.status_code == 303, "generate-map aceito")
    scenes = db.list_scenes(pid)
    must(len(scenes) == 3, f"3 cenas criadas (tem {len(scenes)})")

    # injeta assets fake direto no banco (sem Pexels/Pixabay)
    for i, scene in enumerate(scenes):
        db.add_assets(scene["id"], [
            {
                "source": "pexels", "source_id": f"f{i}a", "asset_type": "video",
                "preview_url": "", "download_url": f"https://example.com/v{i}a.mp4",
                "page_url": "https://example.com", "width": 1920, "height": 1080,
                "duration": 12, "keyword": (scene["keywords"] or ["kw"])[0],
                "author": "Smoke", "author_url": "",
            },
            {
                "source": "pixabay", "source_id": f"f{i}b", "asset_type": "video",
                "preview_url": "", "download_url": f"https://example.com/v{i}b.mp4",
                "page_url": "", "width": 640, "height": 360,
                "duration": 2, "keyword": "other",
                "author": "", "author_url": "",
            },
        ])

    # auto-seleção manual (sem chave Groq -> heurística)
    r = client.post(f"/projects/{pid}/auto-select")
    must(r.status_code == 303, "auto-select aceito")
    chosen = db.list_assets_by_state(pid, ["selected"])
    must(len(chosen) == 3, f"3 takes auto-selecionados (tem {len(chosen)})")
    # heurística deve preferir o 1920x1080 com duração que cobre a cena
    must(all(c["width"] == 1920 for c in chosen), "heurística escolheu os takes de maior qualidade")
    must(all(c["auto_reason"] for c in chosen), "auto_reason preenchido")
    must(db.get_project(pid, 1)["status"] == "reviewing", "status reviewing")

    # tela de revisão
    r = client.get(f"/projects/{pid}/review")
    must(r.status_code == 200 and "Revis" in r.text, "tela de revisão renderiza")

    # aceita 2, rejeita 1
    client.post(f"/assets/{chosen[0]['id']}/state", data={"state": "accepted"})
    client.post(f"/assets/{chosen[1]['id']}/state", data={"state": "accepted"})
    client.post(f"/assets/{chosen[2]['id']}/state", data={"state": "rejected"})
    must(db.get_project(pid, 1)["status"] == "reviewing", "aceitar/rejeitar não derruba o status")

    # concluir sem 100% aceito deve falhar
    r = client.post(f"/projects/{pid}/finish-review")
    must(r.status_code == 400, "finish-review bloqueado com cena rejeitada")

    # re-busca sem chaves deve dar 400 amigável
    r = client.post(f"/projects/{pid}/research-rejected")
    must(r.status_code == 400, "research-rejected exige chaves de API")

    # seleciona o reserva manualmente (mesma cena do rejeitado) e aceita
    rejected_scene_id = chosen[2]["scene_id"]
    others = [a for a in db.list_assets_by_state(pid, ["pending"]) if a["scene_id"] == rejected_scene_id]
    must(len(others) >= 1, "cena rejeitada tem candidato reserva")
    client.post(f"/assets/{others[0]['id']}/state", data={"state": "accepted"})

    # agora conclui e gera o relatório
    r = client.post(f"/projects/{pid}/finish-review")
    must(r.status_code == 303, "finish-review concluído")
    must(r.headers["location"] == f"/projects/{pid}", "finish-review leva ao console do projeto")
    must(db.get_project(pid, 1)["status"] == "reviewed", "status reviewed")
    r = client.get(f"/projects/{pid}/review")
    must("Gerar pacote" in r.text and "/package" in r.text, "review concluido mostra gerar pacote")
    must(f"/projects/{pid}/finish-review" not in r.text, "review concluido nao repete concluir revisao")
    r = client.get(f"/projects/{pid}/curation-report")
    must(r.status_code == 200 and "Relatório de curadoria" in r.text, "relatório baixável")
    must("Rejeitados (1)" in r.text or "Rejeitados" in r.text, "relatório lista rejeitados")

    # página do projeto mostra pipeline atualizado
    r = client.get(f"/projects/{pid}")
    must(r.status_code == 200 and "Revisão (3/3)" in r.text, "pipeline mostra revisão 3/3")

    # editar depois de concluir reabre revisao e remove relatorio obsoleto
    report_path = app_module.curation_report_path(pid)
    must(report_path.exists(), "relatorio existe apos concluir")
    r = client.post(
        f"/assets/{others[0]['id']}/state",
        data={"state": "rejected", "redirect": f"/projects/{pid}/review"},
    )
    must(r.status_code == 303, "fallback nativo redireciona apos editar revisao concluida")
    must(db.get_project(pid, 1)["status"] == "reviewing", "editar take reabre a revisao")
    must(not report_path.exists(), "relatorio obsoleto removido")

print("\nSMOKE REVIEW FLOW: tudo passou.")
