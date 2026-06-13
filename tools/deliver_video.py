"""Driver de entrega: roteiro -> curadoria -> pacote -> render Kaggle -> MP4.

Roda o pipeline COMPLETO com APIs reais, incluindo o render no Kaggle, e copia
o video final para a pasta de saida. Chaves vem de variaveis TEST_* (nunca
hardcoded). Uso interno/manual:

    TEST_PEXELS_KEY=... TEST_PIXABAY_KEY=... TEST_GROQ_KEY=... \
    TEST_KAGGLE_USER=... TEST_KAGGLE_TOKEN=... TEST_COVERR_KEY=... TEST_NVIDIA_KEY=... \
    NARRATION=path.mp3 AVATAR=path.mp4 OUTDIR=path \
    python tools/deliver_video.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# DATA_DIR estavel para os outputs persistirem apos a execucao
DATA_DIR = os.environ.get("DELIVER_DATA_DIR") or str(ROOT / "workdir" / "deliver")
os.environ["DATA_DIR"] = DATA_DIR
os.environ["APP_ENV"] = "dev"
os.environ["ENFORCE_CSRF"] = "0"
# limita a visao LLM por cena nesta entrega (bound de custo/tempo)
os.environ.setdefault("VISION_LLM_TOP_N", "3")

from starlette.testclient import TestClient  # noqa: E402
import app as app_module  # noqa: E402
import database as db  # noqa: E402

K = {n: os.environ.get(f"TEST_{n}", "") for n in
     ["PEXELS_KEY", "PIXABAY_KEY", "GROQ_KEY", "KAGGLE_USER", "KAGGLE_TOKEN",
      "COVERR_KEY", "NVIDIA_KEY"]}
NARRATION = Path(os.environ["NARRATION"])
AVATAR = Path(os.environ["AVATAR"])
OUTDIR = Path(os.environ.get("OUTDIR", str(ROOT)))
RENDER_TIMEOUT = int(os.environ.get("RENDER_TIMEOUT", "2700"))  # 45 min


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def must(cond, msg):
    if not cond:
        log(f"FALHOU: {msg}")
        raise SystemExit(1)
    log(f"ok: {msg}")


client = TestClient(app_module.app, follow_redirects=False)

with client:
    # 1) usuario + chaves
    client.post("/register", data={"username": "deliver", "password": "deliverpass1"})
    r = client.post("/settings", data={
        "pexels": K["PEXELS_KEY"], "pixabay": K["PIXABAY_KEY"], "groq": K["GROQ_KEY"],
        "groq_model": "llama-3.3-70b-versatile",
        "coverr": K.get("COVERR_KEY", ""), "nvidia": K.get("NVIDIA_KEY", ""),
        "kaggle_username": K["KAGGLE_USER"], "kaggle_token": K["KAGGLE_TOKEN"],
    })
    must(r.status_code == 303, "chaves salvas")
    must(client.get("/settings/test-kaggle").json().get("ok"), "credencial Kaggle valida")

    # 2) transcricao da narracao (Groq Whisper)
    log("transcrevendo narracao...")
    script = app_module.groq_service.transcribe_audio(
        NARRATION.read_bytes(), NARRATION.name, K["GROQ_KEY"])
    must(script.strip(), "narracao transcrita")

    # 3) projeto (1920x1080, com fallback de imagem p/ cobrir todas as cenas)
    r = client.post("/projects/new", data={
        "name": "valdir bti", "script": script, "avatar_safe_area": "right",
        "visual_style": "realistic editorial B-roll, rural Brazil, mosquito control, documentary",
        "resolution": "1920x1080", "scene_duration": "5.0", "image_fallback": "1",
    })
    must(r.status_code == 303, "projeto criado")
    pid = int(r.headers["location"].rsplit("/", 1)[-1])

    # 4) sobe narracao + avatar
    with open(NARRATION, "rb") as f:
        r = client.post(f"/projects/{pid}/upload-media", data={"kind": "narration"},
                        files={"media": (NARRATION.name, f, "audio/mpeg")})
    must(r.status_code == 303, "narracao enviada")
    with open(AVATAR, "rb") as f:
        r = client.post(f"/projects/{pid}/upload-media", data={"kind": "avatar"},
                        files={"media": (AVATAR.name, f, "video/mp4")})
    must(r.status_code == 303, "avatar enviado")

    # 5) mapa visual
    t = time.time()
    must(client.post(f"/projects/{pid}/generate-map").status_code == 303,
         f"mapa visual ({time.time()-t:.0f}s)")
    scenes = db.list_scenes(pid)
    log(f"   {len(scenes)} cenas")

    # 6) busca (real + visao) e auto-select
    t = time.time()
    must(client.post(f"/projects/{pid}/search").status_code == 303,
         f"busca + visao ({time.time()-t:.0f}s)")
    assets_by_scene = db.list_assets_for_project(pid)
    empties = [s for s in scenes if not assets_by_scene.get(s["id"])]
    if empties:
        log(f"   {len(empties)} cenas sem candidato; buscando mais (imagens+videos)...")
        for s in empties:
            client.post(f"/scenes/{s['id']}/search-more", data={"media": "all"})
        # re-analisa visao dos novos assets e reavalia
        app_module.analyze_pending_vision(pid, 1, K["GROQ_KEY"], nvidia_key=K["NVIDIA_KEY"])
        assets_by_scene = db.list_assets_for_project(pid)
        still_empty = [s["scene_id"] for s in scenes if not assets_by_scene.get(s["id"])]
        log(f"   ainda sem candidato: {still_empty or 'nenhuma'}")
    client.post(f"/projects/{pid}/auto-select")
    chosen = db.list_assets_by_state(pid, ["selected"])
    must(len(chosen) == len(scenes), f"todas as {len(scenes)} cenas com take ({len(chosen)} selecionados)")

    # 7) aceita tudo + relatorio
    for c in chosen:
        client.post(f"/assets/{c['id']}/state", data={"state": "accepted"})
    must(client.post(f"/projects/{pid}/finish-review").status_code == 303, "revisao concluida")

    # 8) pacote (inclui avatar + narracao + edit_plan)
    t = time.time()
    must(client.post(f"/projects/{pid}/package").status_code == 303, "pacote disparado")
    # package roda em background; no TestClient executa sincrono, mas confirmamos status
    proj = db.get_project(pid, 1)
    must(proj["status"] == "packaged", f"pacote pronto ({time.time()-t:.0f}s, status={proj['status']})")
    zips = list(app_module.project_work_dir(pid).glob("*.zip"))
    must(zips, f"ZIP gerado: {zips[0].name} ({zips[0].stat().st_size//1024//1024} MB)")

    # 9) envia ao Kaggle (upload + push kernel sincronos no TestClient)
    t = time.time()
    r = client.post(f"/projects/{pid}/send-to-kaggle")
    log(f"send-to-kaggle -> {r.status_code} {r.json()}")
    must(r.status_code == 200, f"envio ao Kaggle iniciado ({time.time()-t:.0f}s)")

    # 10) poll do render
    log("aguardando render no Kaggle...")
    deadline = time.time() + RENDER_TIMEOUT
    final_status = None
    while time.time() < deadline:
        info = client.get(f"/projects/{pid}/kaggle-status").json()
        st = info.get("status")
        log(f"   kaggle-status: {st}")
        if st == "complete":
            final_status = info
            break
        if st == "error":
            final_status = info
            log(f"   erro Kaggle: {info.get('error')}")
            break
        time.sleep(25)

    if not final_status or final_status.get("status") != "complete":
        log(f"RENDER NAO CONCLUIU a tempo/erro: {final_status}")
        raise SystemExit(2)

    # 11) copia o video final para OUTDIR
    outputs = app_module.local_output_videos(app_module.project_work_dir(pid))
    src = outputs.get("master") or outputs.get("base")
    must(src and src.exists(), f"video final baixado ({final_status.get('video_url')})")
    OUTDIR.mkdir(parents=True, exist_ok=True)
    dest = OUTDIR / "video_final.mp4"
    import shutil
    shutil.copy2(src, dest)
    log(f"ENTREGUE: {dest} ({dest.stat().st_size//1024//1024} MB)")
    val = final_status.get("validation") or {}
    log(f"validacao: {val}")

print("\nENTREGA COMPLETA.", flush=True)
