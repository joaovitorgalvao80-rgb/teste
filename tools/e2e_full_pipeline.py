"""E2E real: app -> pacote (plano LLM) -> Kaggle -> HyperFrames -> master.

Percorre o fluxo de producao inteiro pelos endpoints reais do app, com dados
isolados em workdir/e2e_data (nao toca o banco do usuario):

  registro -> settings (Kaggle + OpenRouter) -> projeto com timestamps
  -> mapa visual (fallback heuristico, sem Groq) -> assets injetados de um
  servidor HTTP local -> selecao -> upload de narracao -> package (edit plan
  via OpenRouter real) -> send-to-kaggle (render real) -> polling
  -> download do final_master.mp4 -> ffprobe (video + audio).

Chaves somente via ambiente: OPENROUTER_KEY, KAGGLE_USERNAME, KAGGLE_KEY.

Uso:
  python tools/e2e_full_pipeline.py
"""
from __future__ import annotations

import functools
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

E2E_DIR = ROOT / "workdir" / "e2e_data"
POLL_SECONDS = 45
MAX_WAIT_SECONDS = 45 * 60

SCRIPT = """[00:00.0 - 00:04.0] Todo criador perde horas montando b-roll na mao.
[00:04.0 - 00:08.0] O NWRCH Studio busca os clipes certos e monta a base sozinho.
[00:08.0 - 00:12.0] Voce so revisa, aprova e publica.
"""


def run(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=True)


def make_media(media_dir: Path) -> None:
    media_dir.mkdir(parents=True, exist_ok=True)
    sources = ["testsrc2=size=1280x720:rate=30:duration=6",
               "smptebars=size=1280x720:rate=30:duration=6",
               "mandelbrot=size=1280x720:rate=30",]
    for i, src in enumerate(sources, start=1):
        out = media_dir / f"clip{i}.mp4"
        cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i", src]
        if "duration" not in src:
            cmd += ["-t", "6"]
        cmd += ["-vf", "format=yuv420p", "-c:v", "libx264", "-preset", "fast", str(out)]
        run(cmd)
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
         "-c:a", "libmp3lame", str(media_dir / "narration.mp3")])


def serve(media_dir: Path) -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(media_dir))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return port


def main() -> int:  # noqa: PLR0915 - roteiro E2E linear
    openrouter = os.environ.get("OPENROUTER_KEY", "").strip()
    kaggle_user = os.environ.get("KAGGLE_USERNAME", "").strip()
    kaggle_key = os.environ.get("KAGGLE_KEY", "").strip()
    if not (openrouter and kaggle_user and kaggle_key):
        print("Defina OPENROUTER_KEY, KAGGLE_USERNAME e KAGGLE_KEY.", file=sys.stderr)
        return 2

    if E2E_DIR.exists():
        shutil.rmtree(E2E_DIR)
    E2E_DIR.mkdir(parents=True)

    import app as webapp
    import database as db
    webapp.DATA_DIR = E2E_DIR / "data"
    webapp.WORK_DIR = webapp.DATA_DIR / "work"
    db.DATA_DIR = webapp.DATA_DIR
    db.DB_PATH = webapp.DATA_DIR / "plataforma.db"

    from fastapi.testclient import TestClient

    media_dir = E2E_DIR / "media"
    make_media(media_dir)
    port = serve(media_dir)
    print(f"[e2e] media local em http://127.0.0.1:{port}")

    with TestClient(webapp.app) as client:
        client.post("/register", data={"username": "e2e", "password": "password123"},
                    follow_redirects=False)
        client.post("/settings", data={
            "openrouter": openrouter,
            "kaggle_username": kaggle_user,
            "kaggle_token": kaggle_key,
        }, follow_redirects=False)

        r = client.post("/projects/new", data={
            "name": "E2E Pipeline",
            "script": SCRIPT,
            "avatar_safe_area": "right",
            "resolution": "1280x720",
        }, follow_redirects=False)
        project_id = int(r.headers["location"].rstrip("/").split("/")[-1])
        print(f"[e2e] projeto {project_id} criado")

        r = client.post(f"/projects/{project_id}/generate-map", follow_redirects=False)
        scenes = db.list_scenes(project_id)
        assert len(scenes) == 3, f"esperava 3 cenas, veio {len(scenes)}"
        print(f"[e2e] mapa visual: {len(scenes)} cenas")

        for i, scene in enumerate(scenes, start=1):
            db.add_assets(scene["id"], [{
                "source": "e2e-local", "source_id": f"clip{i}", "asset_type": "video",
                "download_url": f"http://127.0.0.1:{port}/clip{i}.mp4",
                "preview_url": "", "page_url": "", "width": 1280, "height": 720,
                "duration": 6, "keyword": "e2e",
            }])
            asset = db.list_assets(scene["id"])[0]
            resp = client.post(f"/assets/{asset['id']}/state", data={"state": "selected"})
            assert resp.status_code == 200, resp.text
        print("[e2e] 3 assets injetados e selecionados")

        narration = (media_dir / "narration.mp3").read_bytes()
        resp = client.post(f"/projects/{project_id}/upload-media", data={"kind": "narration"},
                           files={"media": ("narration.mp3", narration, "audio/mpeg")},
                           follow_redirects=False)
        assert resp.status_code == 303, resp.text
        print("[e2e] narracao enviada")

        resp = client.post(f"/projects/{project_id}/package", follow_redirects=False)
        assert resp.status_code == 303, resp.text
        zip_path = webapp.latest_zip(webapp.WORK_DIR / f"project_{project_id}")
        with zipfile.ZipFile(zip_path) as zf:
            plan = json.loads(zf.read("edit_plan.json"))
            names = zf.namelist()
        assert "narration.mp3" in names, names
        editorial = plan.get("editorial", "deterministico")
        captions = [s["caption"] for s in plan["scenes"] if s.get("caption")]
        print(f"[e2e] pacote ok: editorial={editorial}, captions={captions}")
        if editorial != "llm":
            print("[e2e] AVISO: OpenRouter caiu no fallback deterministico")

        resp = client.post(f"/projects/{project_id}/send-to-kaggle")
        assert resp.status_code == 200, resp.text
        send_payload = resp.json()
        print(f"[e2e] kaggle: {send_payload}")
        if send_payload.get("job_id"):
            job = client.get(f"/jobs/{send_payload['job_id']}").json()
            print(f"[e2e] kaggle job: {job['status']} - {job.get('message') or job.get('error')}")
            assert job["status"] == "complete", job

        deadline = time.time() + MAX_WAIT_SECONDS
        final_status = ""
        while time.time() < deadline:
            data = client.get(f"/projects/{project_id}/kaggle-status").json()
            status = (data.get("status") or "").lower()
            elapsed = int(MAX_WAIT_SECONDS - (deadline - time.time()))
            print(f"[{elapsed//60:02d}:{elapsed%60:02d}] status={status} "
                  f"master={bool(data.get('master_video_url'))} hf={data.get('hyperframes')}")
            if status in {"complete", "error", "cancelacknowledged"}:
                final_status = status
                break
            time.sleep(POLL_SECONDS)

        if final_status != "complete":
            print(f"E2E_RESULT: FAILED - status final {final_status or 'timeout'}")
            return 1

        resp = client.get(f"/projects/{project_id}/download-master-video")
        assert resp.status_code == 200, f"download master: HTTP {resp.status_code}"
        master = E2E_DIR / "final_master.mp4"
        master.write_bytes(resp.content)
        diag = client.get(f"/projects/{project_id}/diagnostics.json?refresh=1").json()
        validation = (diag.get("diagnostics") or {}).get("outputs", {}).get("validation") or {}
        print(f"[e2e] validacao: {validation.get('status')} issues={validation.get('issues')}")

    probe = run(["ffprobe", "-v", "error", "-show_entries",
                 "stream=codec_type:format=duration", "-of", "json", str(master)])
    info = json.loads(probe.stdout)
    kinds = sorted(s["codec_type"] for s in info["streams"])
    duration = float(info["format"]["duration"])
    print(f"[e2e] master: {master.stat().st_size/1024:.0f} KB, streams={kinds}, {duration:.1f}s")
    if "audio" not in kinds or "video" not in kinds or duration < 10:
        print("E2E_RESULT: FAILED - master sem video+audio ou curto demais")
        return 1
    print("E2E_RESULT: SUCCESS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
