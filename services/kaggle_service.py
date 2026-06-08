"""Integração com Kaggle via CLI (kaggle>=1.5).

Usa subprocess + kaggle CLI em vez da API Python para evitar
incompatibilidades entre versões do pacote.

O montador.py é enviado junto no dataset — sem embutir base64 no kernel.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
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
    return ("b-rolls-render-" + _slug(project_name))[:50]


def _run(args: list[str], username: str, token: str, **kwargs) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["KAGGLE_USERNAME"] = username
    env["KAGGLE_KEY"] = token
    cmd = [sys.executable, "-m", "kaggle"] + args
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        out = (result.stderr or "") + (result.stdout or "")
        raise RuntimeError((out or "erro desconhecido")[-800:])
    return result


# ------------------------------------------------------------------
# Upload do ZIP como dataset (inclui montador.py junto)
# ------------------------------------------------------------------
def upload_dataset(zip_path: Path, project_name: str, username: str, token: str) -> str:
    slug = dataset_slug(project_name)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        shutil.copy2(zip_path, tmp / zip_path.name)

        montador_src = ROOT / "montador.py"
        if montador_src.exists():
            shutil.copy2(montador_src, tmp / "montador.py")

        metadata = {
            "title": f"B-rolls {project_name}"[:50],
            "id": f"{username}/{slug}",
            "licenses": [{"name": "CC0-1.0"}],
        }
        (tmp / "dataset-metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False), encoding="utf-8"
        )

        # tenta nova versão primeiro; se não existir, cria
        try:
            _run(
                ["datasets", "version", "-p", str(tmp), "-m", "update"],
                username, token, timeout=300,
            )
        except RuntimeError:
            _run(
                ["datasets", "create", "-p", str(tmp)],
                username, token, timeout=300,
            )

    return slug


# ------------------------------------------------------------------
# Kernel — runner.py leve, montador vem do dataset
# ------------------------------------------------------------------
_RUNNER = """\
import subprocess, sys
from pathlib import Path

ds = next(Path("/kaggle/input").iterdir())
montador = ds / "montador.py"
zips = list(ds.rglob("*.zip"))
if not zips:
    raise RuntimeError("ZIP nao encontrado em " + str(ds))

zip_path = zips[0]
out = Path("/kaggle/working/video_broll_base.mp4")
print(f"ZIP: {zip_path} ({zip_path.stat().st_size/1024/1024:.1f} MB)")

r = subprocess.run(
    [sys.executable, str(montador), str(zip_path), "--out", str(out), "--preset", "fast"],
    capture_output=True, text=True,
)
print(r.stdout[-3000:])
if r.returncode != 0:
    print("STDERR:", r.stderr[-2000:])
    raise RuntimeError("Montador falhou (exit " + str(r.returncode) + ")")
print(f"Video: {out} ({out.stat().st_size/1024/1024:.1f} MB)")
"""


def push_kernel(ds_slug: str, project_name: str, username: str, token: str) -> tuple[str, str]:
    """Retorna (k_slug, push_output) para debug."""
    slug = kernel_slug(project_name)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        (tmp / "runner.py").write_text(_RUNNER, encoding="utf-8")

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

        r = _run(["kernels", "push", "-p", str(tmp)], username, token, timeout=60)
        push_out = (r.stdout or "") + (r.stderr or "")

    return slug, push_out.strip()


# ------------------------------------------------------------------
# Status
# ------------------------------------------------------------------
def get_status(k_slug: str, username: str, token: str) -> dict:
    page_url = f"https://www.kaggle.com/code/{username}/{k_slug}"
    try:
        result = _run(
            ["kernels", "status", f"{username}/{k_slug}"],
            username, token, timeout=30,
        )
        out = result.stdout.lower()
        if "complete" in out:
            status = "complete"
        elif "running" in out:
            status = "running"
        elif "error" in out or "fail" in out:
            status = "error"
        else:
            status = "queued"
    except Exception as exc:
        err = str(exc)
        return {"status": "error", "url": page_url, "video_url": "", "error": err[:400]}

    video_url = ""
    if status == "complete":
        try:
            video_url = get_video_url(k_slug, username, token)
        except Exception:
            video_url = ""

    return {"status": status, "url": page_url, "video_url": video_url, "error": ""}


def get_video_url(k_slug: str, username: str, token: str) -> str:
    import requests as req
    from requests.auth import HTTPBasicAuth
    resp = req.get(
        "https://www.kaggle.com/api/v1/kernels/output",
        auth=HTTPBasicAuth(username, token),
        params={"userName": username, "kernelSlug": k_slug},
        timeout=30,
    )
    if not resp.ok:
        return ""
    for f in resp.json().get("files", []):
        if f.get("fileName", "").endswith("video_broll_base.mp4") and f.get("url"):
            return f["url"]
    for f in resp.json().get("files", []):
        if f.get("fileName", "").endswith(".mp4") and f.get("url"):
            return f["url"]
    return ""
