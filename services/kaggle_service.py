"""Integração com a API do Kaggle.

Fluxo:
  1. Faz upload do asset_pack.zip como dataset privado
  2. Dispara um kernel (script Python) que roda o montador.py
  3. Retorna o slug do kernel para polling de status
"""
from __future__ import annotations

import base64
import re
import unicodedata
from pathlib import Path
from typing import Optional

import requests
from requests.auth import HTTPBasicAuth

KAGGLE_API = "https://www.kaggle.com/api/v1"
ROOT = Path(__file__).resolve().parent.parent


def _auth(username: str, token: str) -> HTTPBasicAuth:
    return HTTPBasicAuth(username, token)


def _slug(text: str, max_len: int = 36) -> str:
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (text[:max_len] or "brolls").strip("-")


def dataset_slug(project_name: str) -> str:
    return ("brolls-" + _slug(project_name))[:50]


def kernel_slug(project_name: str) -> str:
    return ("brolls-render-" + _slug(project_name))[:50]


# ------------------------------------------------------------------
# Upload do ZIP
# ------------------------------------------------------------------
def _blob_upload(zip_path: Path, username: str, token: str) -> str:
    stat = zip_path.stat()
    resp = requests.post(
        f"{KAGGLE_API}/blobs/upload",
        auth=_auth(username, token),
        json={
            "fileName": zip_path.name,
            "contentLength": stat.st_size,
            "lastModifiedEpochSeconds": int(stat.st_mtime),
            "resourceType": "Dataset",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    with open(zip_path, "rb") as f:
        requests.put(
            data["createUrl"],
            data=f,
            headers={"Content-Type": "application/octet-stream"},
            timeout=600,
        ).raise_for_status()
    return data["token"]


def _dataset_exists(slug: str, username: str, token: str) -> bool:
    return requests.get(
        f"{KAGGLE_API}/datasets/{username}/{slug}",
        auth=_auth(username, token),
        timeout=15,
    ).status_code == 200


def upload_dataset(zip_path: Path, project_name: str, username: str, token: str) -> str:
    """Cria ou atualiza o dataset Kaggle com o ZIP. Retorna o slug."""
    slug = dataset_slug(project_name)
    blob_token = _blob_upload(zip_path, username, token)

    if _dataset_exists(slug, username, token):
        requests.post(
            f"{KAGGLE_API}/datasets/{username}/{slug}/versions",
            auth=_auth(username, token),
            json={"versionNotes": "Atualizado", "files": [{"token": blob_token}]},
            timeout=30,
        ).raise_for_status()
    else:
        requests.post(
            f"{KAGGLE_API}/datasets",
            auth=_auth(username, token),
            json={
                "ownerSlug": username,
                "slug": slug,
                "title": f"B-rolls - {project_name}",
                "licenseName": "CC0-1.0",
                "isPrivate": True,
                "files": [{"token": blob_token}],
            },
            timeout=30,
        ).raise_for_status()

    return slug


# ------------------------------------------------------------------
# Kernel (montador)
# ------------------------------------------------------------------
def _kernel_source(ds_slug: str) -> str:
    """Gera o código-fonte do kernel que vai rodar no Kaggle."""
    montador_path = ROOT / "montador.py"
    montador_b64 = base64.b64encode(montador_path.read_bytes()).decode()

    return f"""\
import base64, subprocess, sys
from pathlib import Path

# Escreve montador.py a partir do fonte embedido
montador_src = base64.b64decode("{montador_b64}").decode("utf-8")
Path("/kaggle/working/montador.py").write_text(montador_src, encoding="utf-8")

# Localiza o ZIP no dataset
zips = list(Path("/kaggle/input/{ds_slug}").rglob("*.zip"))
if not zips:
    raise RuntimeError("ZIP nao encontrado no dataset")
zip_path = zips[0]
print(f"ZIP: {{zip_path}} ({{zip_path.stat().st_size/1024/1024:.1f}} MB)")

# Roda o montador
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
    """Cria ou atualiza o kernel. Retorna o slug."""
    slug = kernel_slug(project_name)
    requests.post(
        f"{KAGGLE_API}/kernels/push",
        auth=_auth(username, token),
        json={
            "newTitle": f"B-rolls Render - {project_name}",
            "text": _kernel_source(ds_slug),
            "language": "python",
            "kernelType": "script",
            "isPrivate": True,
            "enableGpu": False,
            "enableInternet": False,
            "datasetDataSources": [f"{username}/{ds_slug}"],
            "kernelDataSources": [],
            "competitionDataSources": [],
        },
        timeout=30,
    ).raise_for_status()
    return slug


# ------------------------------------------------------------------
# Status
# ------------------------------------------------------------------
def get_status(k_slug: str, username: str, token: str) -> dict:
    resp = requests.get(
        f"{KAGGLE_API}/kernels/{username}/{k_slug}",
        auth=_auth(username, token),
        timeout=15,
    )
    page_url = f"https://www.kaggle.com/code/{username}/{k_slug}"
    if resp.status_code == 404:
        return {"status": "queued", "url": page_url, "video_url": "", "error": ""}
    resp.raise_for_status()
    data = resp.json()
    run = data.get("currentRunningVersion") or {}
    status = (run.get("status") or "queued").lower()

    video_url = ""
    if status == "complete":
        try:
            video_url = get_video_url(k_slug, username, token)
        except Exception:  # noqa: BLE001
            video_url = ""

    return {
        "status": status,
        "url": page_url,
        "video_url": video_url,
        "error": run.get("errorMessage") or "",
    }


def get_video_url(k_slug: str, username: str, token: str) -> str:
    """Retorna a URL assinada do video_broll_base.mp4 gerado pelo kernel."""
    resp = requests.get(
        f"{KAGGLE_API}/kernels/output",
        auth=_auth(username, token),
        params={"userName": username, "kernelSlug": k_slug},
        timeout=30,
    )
    resp.raise_for_status()
    files = resp.json().get("files", [])
    # prioriza o mp4 final; cai para qualquer .mp4
    for f in files:
        if f.get("fileName", "").endswith("video_broll_base.mp4") and f.get("url"):
            return f["url"]
    for f in files:
        if f.get("fileName", "").endswith(".mp4") and f.get("url"):
            return f["url"]
    return ""
