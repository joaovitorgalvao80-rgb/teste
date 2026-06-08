"""Gera o asset_pack.zip — o contrato entre Sistema 1 e Sistema 2.

Conteudo do ZIP (conforme PLANO_SISTEMA_COMPLETO.md):

  assets/
    scene_001_00-04_gancho_<slug>.mp4
    ...
  guia_visual.json      <- consumido pelo Sistema 2 (montador)
  guia_visual.csv       <- abrir em planilha
  roteiro_com_brolls.md <- revisao humana
  metadata/
    pexels_sources.json
    pixabay_sources.json
    rejected_assets.json
"""
from __future__ import annotations

import csv
import io
import json
import re
import unicodedata
import zipfile
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests


def _slug(text: str, max_len: int = 28) -> str:
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text.lower()).strip("_")
    return (text[:max_len] or "asset").strip("_")


def _ext_for(asset: dict) -> str:
    url = asset.get("download_url", "")
    ext = Path(urlparse(url).path).suffix.lower()
    if asset.get("asset_type") == "image":
        return ext if ext in {".jpg", ".jpeg", ".png", ".webp"} else ".jpg"
    return ext if ext in {".mp4", ".mov", ".webm"} else ".mp4"


def _stamp(seconds: float) -> str:
    return f"{int(seconds // 60):02d}-{int(seconds % 60):02d}"


def _download(url: str, dest: Path, max_bytes: int) -> bool:
    try:
        with requests.get(url, stream=True, timeout=90) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0) or 0)
            if total and total > max_bytes:
                print(f"  [skip] {total/1024/1024:.0f}MB > limite")
                return False
            got = 0
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 512):
                    if not chunk:
                        continue
                    got += len(chunk)
                    if got > max_bytes:
                        print("  [skip] passou do limite no meio do download")
                        f.close()
                        dest.unlink(missing_ok=True)
                        return False
                    f.write(chunk)
        return dest.exists() and dest.stat().st_size > 0
    except Exception as exc:  # noqa: BLE001
        print(f"  [download erro] {exc}")
        dest.unlink(missing_ok=True)
        return False


def build_guide(project: dict, config: dict, scenes: list[dict], selected_by_scene: dict[int, dict]) -> dict:
    """Monta o dicionario guia_visual.json a partir das cenas + asset selecionado."""
    guide_scenes = []
    for scene in scenes:
        asset = selected_by_scene.get(scene["id"])
        filename = None
        source_metadata = None
        if asset:
            slug = _slug(asset.get("keyword") or scene.get("visual_goal") or scene["scene_id"])
            zone = (scene.get("zone") or "").lower() or "cena"
            filename = f"{scene['scene_id']}_{_stamp(scene['start_time'])}_{zone}_{slug}{_ext_for(asset)}"
            source_metadata = {
                "source": asset["source"],
                "source_id": asset.get("source_id", ""),
                "page_url": asset.get("page_url", ""),
                "author": asset.get("author", ""),
                "author_url": asset.get("author_url", ""),
                "width": asset.get("width", 0),
                "height": asset.get("height", 0),
                "original_duration": asset.get("duration", 0),
                "keyword": asset.get("keyword", ""),
            }
        guide_scenes.append(
            {
                "id": scene["scene_id"],
                "zone": scene.get("zone", ""),
                "start_time": scene["start_time"],
                "end_time": scene["end_time"],
                "duration": scene["duration"],
                "narration": scene.get("narration", ""),
                "visual_goal": scene.get("visual_goal", ""),
                "keywords": scene.get("keywords", []),
                "must_show": scene.get("must_show", []),
                "must_not_show": scene.get("must_not_show", []),
                "asset_type": asset.get("asset_type", scene.get("asset_type", "video")) if asset else scene.get("asset_type", "video"),
                "selected_asset": f"assets/{filename}" if filename else None,
                "motion": "natural_video" if (asset and asset.get("asset_type") == "video") else "still_kenburns",
                "overlay_text": scene.get("overlay_text", ""),
                "overlay_position": "left" if config.get("avatar_safe_area", "right") == "right" else "right",
                "avatar_safe_area": scene.get("avatar_safe_area", config.get("avatar_safe_area", "right")),
                "source_metadata": source_metadata,
            }
        )
    return {
        "project_name": project["name"],
        "avatar_safe_area": config.get("avatar_safe_area", "right"),
        "avatar_safe_width_ratio": config.get("avatar_safe_width_ratio", 0.30),
        "resolution": config.get("resolution", "1920x1080"),
        "format": config.get("format", "16:9"),
        "total_duration": round(max((s["end_time"] for s in scenes), default=0.0), 3),
        "scenes": guide_scenes,
    }


def _guide_to_csv(guide: dict) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["id", "zone", "start_time", "end_time", "duration", "narration",
         "visual_goal", "keywords", "asset_type", "selected_asset", "overlay_text"]
    )
    for s in guide["scenes"]:
        writer.writerow(
            [s["id"], s["zone"], s["start_time"], s["end_time"], s["duration"],
             s["narration"], s["visual_goal"], " | ".join(s["keywords"]),
             s["asset_type"], s.get("selected_asset") or "", s["overlay_text"]]
        )
    return buf.getvalue()


def _guide_to_md(project: dict, guide: dict) -> str:
    lines = [f"# Roteiro com B-rolls — {project['name']}", ""]
    lines.append(f"- Resolucao: {guide['resolution']}")
    lines.append(f"- Area segura do avatar: {guide['avatar_safe_area']}")
    lines.append(f"- Duracao total: {guide['total_duration']:.1f}s")
    lines.append("")
    for s in guide["scenes"]:
        lines.append(f"## {s['id']} | {s['start_time']:.1f}s-{s['end_time']:.1f}s | {s['zone']}")
        lines.append("")
        lines.append(f"**Narracao:** {s['narration']}")
        lines.append("")
        lines.append(f"**Objetivo visual:** {s['visual_goal']}")
        lines.append("")
        lines.append(f"**Keywords:** {', '.join(s['keywords'])}")
        if s["must_show"]:
            lines.append(f"**Deve mostrar:** {', '.join(s['must_show'])}")
        if s["must_not_show"]:
            lines.append(f"**Nao mostrar:** {', '.join(s['must_not_show'])}")
        lines.append(f"**Asset:** `{s.get('selected_asset') or '— sem selecao —'}`")
        if s["overlay_text"]:
            lines.append(f"**Texto overlay:** {s['overlay_text']}")
        lines.append("")
    return "\n".join(lines)


def build_zip(
    project: dict,
    config: dict,
    scenes: list[dict],
    selected_by_scene: dict[int, dict],
    rejected_assets: list[dict],
    work_dir: Path,
    max_download_mb: int = 90,
) -> Path:
    """Baixa assets selecionados, renomeia e monta o ZIP final. Retorna o caminho."""
    work_dir.mkdir(parents=True, exist_ok=True)
    guide = build_guide(project, config, scenes, selected_by_scene)
    max_bytes = int(max_download_mb * 1024 * 1024)

    # baixa cada asset selecionado para uma pasta temporaria
    tmp = work_dir / "assets_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    file_by_scene: dict[str, Path] = {}
    pexels_sources, pixabay_sources = [], []

    for scene, gscene in zip(scenes, guide["scenes"]):
        asset = selected_by_scene.get(scene["id"])
        if not asset or not gscene.get("selected_asset"):
            continue
        filename = Path(gscene["selected_asset"]).name
        dest = tmp / filename
        print(f"[zip] baixando {scene['scene_id']} <- {asset['source']} {asset.get('width')}x{asset.get('height')}")
        if _download(asset["download_url"], dest, max_bytes):
            file_by_scene[scene["scene_id"]] = dest
            record = {
                "scene_id": scene["scene_id"],
                "file": f"assets/{filename}",
                **(gscene.get("source_metadata") or {}),
            }
            (pexels_sources if asset["source"] == "pexels" else pixabay_sources).append(record)
        else:
            # falhou o download: remove a selecao do guia para nao apontar para arquivo inexistente
            gscene["selected_asset"] = None
            gscene["source_metadata"] = None

    if not file_by_scene:
        raise RuntimeError(
            "nenhum asset selecionado conseguiu ser baixado; "
            "verifique URLs expiradas, limite de MB ou conexao com Pexels/Pixabay"
        )

    safe_name = _slug(project["name"]) or "asset_pack"
    zip_path = work_dir / f"asset_pack_{safe_name}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for scene_code, src in file_by_scene.items():
            zf.write(src, f"assets/{src.name}")
        zf.writestr("guia_visual.json", json.dumps(guide, ensure_ascii=False, indent=2))
        zf.writestr("guia_visual.csv", _guide_to_csv(guide))
        zf.writestr("roteiro_com_brolls.md", _guide_to_md(project, guide))
        zf.writestr("metadata/pexels_sources.json", json.dumps(pexels_sources, ensure_ascii=False, indent=2))
        zf.writestr("metadata/pixabay_sources.json", json.dumps(pixabay_sources, ensure_ascii=False, indent=2))
        zf.writestr("metadata/rejected_assets.json", json.dumps(rejected_assets, ensure_ascii=False, indent=2))

    return zip_path
