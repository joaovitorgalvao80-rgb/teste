"""Smoke test do modo vídeo longo (Frente C) sem APIs externas.

- cria projeto long_mode com roteiro de ~10 min
- verifica autosplit em partes
- empacota por parte com download mockado
- simula partes renderizadas e roda a concatenação real (se ffmpeg existir)

Uso: python tools/smoke_long_mode.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

tmp = tempfile.mkdtemp(prefix="nwrch_smoke_long_")
os.environ["DATA_DIR"] = tmp
os.environ["APP_ENV"] = "dev"
os.environ["ENFORCE_CSRF"] = "0"

from starlette.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
import database as db  # noqa: E402
from services import packager  # noqa: E402

client = TestClient(app_module.app, follow_redirects=False)


def must(cond, msg):
    if not cond:
        raise SystemExit(f"FALHOU: {msg}")
    print(f"ok: {msg}")


# download mockado: escreve bytes fake em vez de baixar da internet
def _fake_download(url: str, dest: Path, max_bytes: int) -> bool:
    dest.write_bytes(b"\x00" * 2048)
    return True


packager._download = _fake_download

with client:
    client.post("/register", data={"username": "smoke", "password": "smokepass123"})

    # roteiro de 10 min: 100 cenas de 6s com timestamps
    lines = []
    for i in range(100):
        start, end = i * 6, i * 6 + 6
        lines.append(f"[{start//60:02d}:{start%60:02d}.0 - {end//60:02d}:{end%60:02d}.0] Cena {i+1} do video longo sobre agricultura familiar.")
    r = client.post("/projects/new", data={
        "name": "smoke longo",
        "script": "\n".join(lines),
        "avatar_safe_area": "right",
        "visual_style": "documental",
        "resolution": "1280x720",
        "scene_duration": "4.0",
        "long_mode": "1",
    })
    must(r.status_code == 303, "criar projeto long_mode")
    pid = int(r.headers["location"].rsplit("/", 1)[-1])

    r = client.post(f"/projects/{pid}/generate-map")
    must(r.status_code == 303, "generate-map aceito")
    scenes = db.list_scenes(pid)
    must(len(scenes) == 100, f"100 cenas (tem {len(scenes)})")
    parts = db.list_parts(pid)
    # 600s / 150s alvo = 4 partes
    must(len(parts) == 4, f"4 partes criadas (tem {len(parts)})")
    must(all(p["scene_count"] == 25 for p in parts), "25 cenas por parte")
    must(scenes[0]["part"] == 1 and scenes[-1]["part"] == 4, "cenas com part atribuido")

    # render-parts antes do pacote deve falhar
    r = client.post(f"/projects/{pid}/render-parts")
    must(r.status_code == 400, "render-parts bloqueado sem pacote")

    # injeta 1 asset fake selecionado por cena
    for scene in scenes:
        db.add_assets(scene["id"], [{
            "source": "pexels", "source_id": f"s{scene['id']}", "asset_type": "video",
            "preview_url": "", "download_url": f"https://example.com/{scene['id']}.mp4",
            "page_url": "", "width": 1280, "height": 720, "duration": 8,
            "keyword": "farm", "author": "", "author_url": "",
        }])
    project = db.get_project(pid, 1)
    broll_map = app_module.scene_broll_flags(scenes, app_module.project_config(project))
    scene_by_db_id = {scene["id"]: scene for scene in scenes}
    for a in db.list_assets_by_state(pid, ["pending"]):
        scene = scene_by_db_id[a["scene_id"]]
        if broll_map.get(scene["scene_id"], True):
            db.set_asset_state(a["id"], "accepted")

    for p in parts:
        r = client.post(f"/projects/{pid}/parts/{p['part_idx']}/confirm")
        must(r.status_code == 303, f"parte {p['part_idx']} curada")

    # pacote por partes
    r = client.post(f"/projects/{pid}/package")
    must(r.status_code == 303, "package aceito")
    parts = db.list_parts(pid)
    must(all(p["status"] == "zipped" for p in parts), "todas as partes zipadas")
    for p in parts:
        zp = app_module.part_dir(pid, p["part_idx"]) / p["zip_name"]
        must(zp.exists() and zp.stat().st_size > 0, f"zip da parte {p['part_idx']} existe")
    must(db.get_project(pid, 1)["status"] == "packaged", "status packaged")

    # guia da parte 2 deve comecar em t=0 (rebase)
    import zipfile, json as _json
    p2zip = app_module.part_dir(pid, 2) / db.list_parts(pid)[1]["zip_name"]
    with zipfile.ZipFile(p2zip) as zf:
        guide = _json.loads(zf.read("guia_visual.json"))
    must(abs(guide["scenes"][0]["start_time"]) < 0.01, "parte 2 rebaseada para t=0")
    must(abs(guide["total_duration"] - 150.0) < 0.01, f"parte 2 dura 150s (tem {guide['total_duration']})")

    # concat bloqueado com partes pendentes
    r = client.post(f"/projects/{pid}/concat-parts")
    must(r.status_code == 400, "concat bloqueado com partes pendentes")

    # parts-status responde
    r = client.get(f"/projects/{pid}/parts-status")
    must(r.status_code == 200 and len(r.json()["parts"]) == 4, "parts-status ok")

    # simula partes renderizadas com clipes reais e concatena
    if shutil.which("ffmpeg"):
        for p in db.list_parts(pid):
            out_dir = app_module.part_dir(pid, p["part_idx"]) / "kaggle_output"
            out_dir.mkdir(parents=True, exist_ok=True)
            clip = out_dir / "video_broll_base.mp4"
            subprocess.run(
                ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=320x180:d=1",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)],
                capture_output=True, check=True,
            )
            db.update_part(pid, p["part_idx"], status="done", video_path=str(clip))
        r = client.post(f"/projects/{pid}/concat-parts")
        must(r.status_code == 200, "concat-parts aceito")
        final = app_module.project_work_dir(pid) / "kaggle_output" / "video_broll_base.mp4"
        must(final.exists() and final.stat().st_size > 0, "video final concatenado existe")
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(final)],
            capture_output=True, text=True,
        )
        dur = float(probe.stdout.strip() or 0)
        must(3.5 <= dur <= 4.5, f"duracao final ~4s = soma das partes (tem {dur:.2f}s)")
    else:
        print("aviso: ffmpeg ausente; pulando teste de concatenacao real")

print("\nSMOKE LONG MODE: tudo passou.")
