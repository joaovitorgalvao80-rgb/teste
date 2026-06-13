"""E2E com APIs reais (Groq + Pexels + Pixabay + credencial Kaggle).

Chaves vêm de variáveis de ambiente: TEST_PEXELS_KEY, TEST_PIXABAY_KEY,
TEST_GROQ_KEY, TEST_KAGGLE_USER, TEST_KAGGLE_TOKEN.

Fluxo: registra usuário → salva chaves → cria projeto de 4 cenas →
mapa visual (Groq real) → busca + auto-seleção (Pexels/Pixabay/Groq reais) →
aceita tudo → relatório → pacote (downloads reais) → testa credencial Kaggle.

Não envia kernel ao Kaggle (render real fica para o teste manual).

Uso: python tools/e2e_real_apis.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

tmp = tempfile.mkdtemp(prefix="nwrch_e2e_")
os.environ["DATA_DIR"] = tmp
os.environ["APP_ENV"] = "dev"
os.environ["ENFORCE_CSRF"] = "0"

from starlette.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
import database as db  # noqa: E402

KEYS = {name: os.environ.get(f"TEST_{name}", "") for name in
        ["PEXELS_KEY", "PIXABAY_KEY", "GROQ_KEY", "KAGGLE_USER", "KAGGLE_TOKEN"]}
missing = [k for k, v in KEYS.items() if not v]
if missing:
    raise SystemExit(f"defina as variaveis TEST_{', TEST_'.join(missing)}")

client = TestClient(app_module.app, follow_redirects=False)


def must(cond, msg):
    if not cond:
        raise SystemExit(f"FALHOU: {msg}")
    print(f"ok: {msg}")


SCRIPT = """[00:00.0 - 00:05.0] Voce sabia que a agua parada no quintal pode virar um criadouro de mosquito da dengue?
[00:05.0 - 00:10.0] Um simples balde esquecido na chuva ja e suficiente para milhares de larvas.
[00:10.0 - 00:15.0] A boa noticia e que existe um larvicida biologico barato chamado BTI.
[00:15.0 - 00:20.0] Aplique nas calhas e vasos de planta e proteja sua familia hoje mesmo."""

with client:
    client.post("/register", data={"username": "e2e", "password": "e2epassword1"})
    r = client.post("/settings", data={
        "pexels": KEYS["PEXELS_KEY"],
        "pixabay": KEYS["PIXABAY_KEY"],
        "groq": KEYS["GROQ_KEY"],
        "groq_model": "llama-3.3-70b-versatile",
        "kaggle_username": KEYS["KAGGLE_USER"],
        "kaggle_token": KEYS["KAGGLE_TOKEN"],
    })
    must(r.status_code == 303, "chaves salvas em /settings")

    r = client.get("/settings/test-kaggle")
    data = r.json()
    must(data.get("ok"), f"credencial Kaggle valida ({data.get('detail')})")

    r = client.post("/projects/new", data={
        "name": "e2e dengue bti",
        "script": SCRIPT,
        "avatar_safe_area": "right",
        "visual_style": "realistic editorial B-roll, rural Brazil",
        "resolution": "1280x720",
        "scene_duration": "5.0",
    })
    must(r.status_code == 303, "projeto criado")
    pid = int(r.headers["location"].rsplit("/", 1)[-1])

    t0 = time.time()
    r = client.post(f"/projects/{pid}/generate-map")
    must(r.status_code == 303, f"mapa visual (Groq real, {time.time()-t0:.1f}s)")
    scenes = db.list_scenes(pid)
    must(len(scenes) == 4, f"4 cenas (tem {len(scenes)})")
    must(all(s["keywords"] for s in scenes), "todas as cenas com keywords da IA")
    must(all(s["visual_goal"] for s in scenes), "todas as cenas com visual_goal")
    print("   exemplo keywords:", scenes[0]["keywords"])

    t0 = time.time()
    r = client.post(f"/projects/{pid}/search")
    must(r.status_code == 303, f"busca de assets (APIs reais, {time.time()-t0:.1f}s)")
    project = db.get_project(pid, 1)
    must(project["status"] == "searched", f"status pos-busca manual: {project['status']}")

    r = client.post(f"/projects/{pid}/auto-select")
    must(r.status_code == 303, "auto-select iniciado")
    chosen = db.list_assets_by_state(pid, ["selected"])
    must(len(chosen) == 4, f"4 takes auto-selecionados (tem {len(chosen)})")
    for c in chosen:
        print(f"   {c['scene_code']}: {c['source']} {c['width']}x{c['height']} {c['duration']}s — {c['auto_reason'][:70]}")

    # aceita todos na revisao
    for c in chosen:
        client.post(f"/assets/{c['id']}/state", data={"state": "accepted"})
    r = client.post(f"/projects/{pid}/finish-review")
    must(r.status_code == 303, "revisao concluida")
    r = client.get(f"/projects/{pid}/curation-report")
    must(r.status_code == 200, "relatorio gerado")

    t0 = time.time()
    r = client.post(f"/projects/{pid}/package")
    must(r.status_code == 303, f"pacote com downloads reais ({time.time()-t0:.1f}s)")
    project = db.get_project(pid, 1)
    must(project["status"] == "packaged", f"status packaged (tem {project['status']})")
    zips = list((app_module.project_work_dir(pid)).glob("*.zip"))
    must(zips and zips[0].stat().st_size > 100_000, f"ZIP real: {zips[0].name} ({zips[0].stat().st_size//1024} KB)")
    import zipfile
    with zipfile.ZipFile(zips[0]) as zf:
        names = zf.namelist()
    must(any(n.startswith("assets/") for n in names), "ZIP contem assets baixados")
    must("curation_report.md" in names, "ZIP contem o relatorio de curadoria")
    must("edit_plan.json" in names, "ZIP contem edit_plan")

print("\nE2E COM APIS REAIS: tudo passou.")
