"""Integração com Kaggle usando a biblioteca oficial (kaggle>=1.6.17).

Fluxo:
  1. Sobe o asset_pack.zip como dataset privado
  2. Dispara um kernel (script Python) que roda o montador.py
  3. Retorna slug do kernel para polling de status
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _slug(text: str, max_len: int = 36) -> str:
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (text[:max_len] or "brolls").strip("-")


def dataset_slug(project_name: str) -> str:
    return ("brolls-" + _slug(project_name))[:50]


def kernel_slug(project_name: str) -> str:
    return ("brolls-render-" + _slug(project_name))[:50]


def _make_api(username: str, token: str):
    """Cria instância autenticada da API Kaggle via env vars."""
    os.environ["KAGGLE_USERNAME"] = username
    os.environ["KAGGLE_KEY"] = token
    from kaggle.api.kaggle_api_extended import KaggleApiExtended  # noqa: PLC0415
    api = KaggleApiExtended()
    api.authenticate()
    return api


# ------------------------------------------------------------------
# Upload do ZIP como dataset
# ------------------------------------------------------------------
def upload_dataset(zip_path: Path, project_name: str, username: str, token: str) -> str:
    api = _make_api(username, token)
    slug = dataset_slug(project_name)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        shutil.copy2(zip_path, tmp / zip_path.name)

        metadata = {
            "title": f"B-rolls {project_name}"[:50],
            "id": f"{username}/{slug}",
            "licenses": [{"name": "CC0-1.0"}],
        }
        (tmp / "dataset-metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False), encoding="utf-8"
        )

        # Tenta atualizar se já existir, senão cria
        try:
            api.dataset_metadata(username, slug)
            api.dataset_create_version(str(tmp), "update", quiet=True)
        except Exception:
            api.dataset_create_new(str(tmp), public=False, quiet=True)

    return slug


# ------------------------------------------------------------------
# Kernel (montador)
# ------------------------------------------------------------------
def _kernel_source(ds_slug: str) -> str:
    import base64
    montador_path = ROOT / "montador.py"
    montador_b64 = base64.b64encode(montador_path.read_bytes()).decode()

    return f"""\
import base64, subprocess, sys
from pathlib import Path

montador_src = base64.b64decode("{montador_b64}").decode("utf-8")
Path("/kaggle/working/montador.py").write_text(montador_src, encoding="utf-8")

zips = list(Path("/kaggle/input/{ds_slug}").rglob("*.zip"))
if not zips:
    raise RuntimeError("ZIP nao encontrado no dataset")
zip_path = zips[0]
print(f"ZIP: {{zip_path}} ({{zip_path.stat().st_size/1024/1024:.1f}} MB)")

out = Path("/kaggle/working/video_broll_base.mp4")
result = subprocess.run(
    [sys.executable, "/kaggle/working/montador.py", str(zip_path),
     "--out", str(out), "--preset", "fast"],
    capture_output=True, text=True
)
print(result.stdout)
if result.returncode != 0:
    print("STDERR:", result.stderr[-2000:])
    raise RuntimeError("Montador falhou")
print(f"Video: {{out}} ({{out.stat().st_size/1024/1024:.1f}} MB)")
"""


def push_kernel(ds_slug: str, project_name: str, username: str, token: str) -> str:
    api = _make_api(username, token)
    slug = kernel_slug(project_name)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        (tmp / "runner.py").write_text(_kernel_source(ds_slug), encoding="utf-8")

        metadata = {
            "id": f"{username}/{slug}",
            "title": f"B-rolls Render - {project_name}"[:50],
            "code_file": "runner.py",
            "language": "python",
            "kernel_type": "script",
            "is_private": True,
            "enable_gpu": False,
            "enable_internet": False,
            "dataset_sources": [f"{username}/{ds_slug}"],
            "competition_sources": [],
            "kernel_sources": [],
        }
        (tmp / "kernel-metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False), encoding="utf-8"
        )
        api.kernels_push(str(tmp))

    return slug


# ------------------------------------------------------------------
# Status
# ------------------------------------------------------------------
def get_status(k_slug: str, username: str, token: str) -> dict:
    api = _make_api(username, token)
    page_url = f"https://www.kaggle.com/code/{username}/{k_slug}"
    try:
        status_obj = api.kernels_status(username, k_slug)
        status = (status_obj.get("status") or "queued").lower()
        error_msg = status_obj.get("failureMessage") or ""
    except Exception:
        return {"status": "queued", "url": page_url, "video_url": "", "error": ""}

    video_url = ""
    if status == "complete":
        try:
            video_url = get_video_url(k_slug, username, token)
        except Exception:
            video_url = ""

    return {"status": status, "url": page_url, "video_url": video_url, "error": error_msg}


def get_video_url(k_slug: str, username: str, token: str) -> str:
    import requests as req
    from requests.auth import HTTPBasicAuth
    resp = req.get(
        "https://www.kaggle.com/api/v1/kernels/output",
        auth=HTTPBasicAuth(username, token),
        params={"userName": username, "kernelSlug": k_slug},
        timeout=30,
    )
    resp.raise_for_status()
    for f in resp.json().get("files", []):
        if f.get("fileName", "").endswith("video_broll_base.mp4") and f.get("url"):
            return f["url"]
    for f in resp.json().get("files", []):
        if f.get("fileName", "").endswith(".mp4") and f.get("url"):
            return f["url"]
    return ""
