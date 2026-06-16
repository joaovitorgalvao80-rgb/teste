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
import logging
import os
import re
import shutil
import time
import unicodedata
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import requests

logger = logging.getLogger("nwrch.packager")

DOWNLOAD_WORKERS = 4
DOWNLOAD_CONNECT_TIMEOUT = float(os.getenv("ASSET_DOWNLOAD_CONNECT_TIMEOUT", "8"))
DOWNLOAD_READ_TIMEOUT = float(os.getenv("ASSET_DOWNLOAD_READ_TIMEOUT", "20"))
DOWNLOAD_TOTAL_TIMEOUT = float(os.getenv("ASSET_DOWNLOAD_TOTAL_TIMEOUT", "45"))


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


def _copy_generated(asset: dict, work_dir: Path, dest: Path, max_bytes: int) -> bool:
    """Copia imagem gerada por IA (arquivo local no work dir) em vez de baixar.

    O download_url desses assets e uma rota interna (/projects/X/generated/...),
    nao um endpoint publico; requests.get falharia sem sessao autenticada.
    """
    name = Path(urlparse(asset.get("download_url", "")).path).name
    if not name or "/" in name or "\\" in name or ".." in name:
        logger.warning("skip: nome de imagem gerada invalido: %r", name)
        return False
    src = work_dir / "generated" / name
    try:
        if not src.is_file():
            logger.warning("skip: imagem gerada nao encontrada no disco: %s", name)
            return False
        size = src.stat().st_size
        if size == 0 or size > max_bytes:
            logger.warning("skip: imagem gerada vazia ou acima do limite: %s", name)
            return False
        shutil.copyfile(src, dest)
        return dest.exists() and dest.stat().st_size > 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("copia de imagem gerada falhou: %s", exc)
        dest.unlink(missing_ok=True)
        return False


def _download(url: str, dest: Path, max_bytes: int) -> bool:
    start = time.monotonic()
    try:
        with requests.get(
            url,
            stream=True,
            timeout=(DOWNLOAD_CONNECT_TIMEOUT, DOWNLOAD_READ_TIMEOUT),
        ) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0) or 0)
            if total and total > max_bytes:
                logger.warning("skip: %.0fMB > limite", total / 1024 / 1024)
                return False
            got = 0
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 512):
                    if not chunk:
                        continue
                    if time.monotonic() - start > DOWNLOAD_TOTAL_TIMEOUT:
                        logger.warning("skip: download excedeu %.0fs: %s", DOWNLOAD_TOTAL_TIMEOUT, url)
                        f.close()
                        dest.unlink(missing_ok=True)
                        return False
                    got += len(chunk)
                    if got > max_bytes:
                        logger.warning("skip: passou do limite no meio do download")
                        f.close()
                        dest.unlink(missing_ok=True)
                        return False
                    f.write(chunk)
        return dest.exists() and dest.stat().st_size > 0
    except Exception as exc:  # noqa: BLE001
        logger.warning("download falhou: %s", exc)
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
                "license": asset.get("license", ""),
                "license_url": asset.get("license_url", ""),
                "attribution": asset.get("attribution", ""),
                "discovery_provider": asset.get("discovery_provider", ""),
                "scrape_url": asset.get("scrape_url", ""),
                "scrape_status": asset.get("scrape_status", ""),
                "confidence": asset.get("confidence", 0),
                "width": asset.get("width", 0),
                "height": asset.get("height", 0),
                "original_duration": asset.get("duration", 0),
                "keyword": asset.get("keyword", ""),
            }
        guide_scenes.append(
            {
                "id": scene["scene_id"],
                "zone": scene.get("zone", ""),
                "broll": bool(scene.get("broll", True)),
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


def _accepted_take_lines(asset: Optional[dict]) -> list[str]:
    if not asset:
        return ["**Take aceito:** — nenhum —"]
    kind = asset.get("asset_type", "video")
    dur = f", {asset.get('duration')}s" if asset.get("duration") else ""
    lines = [
        f"**Take aceito:** {asset.get('source')} {kind} "
        f"{asset.get('width')}x{asset.get('height')}{dur}"
    ]
    if asset.get("author"):
        lines.append(f"**Autor:** [{asset['author']}]({asset.get('author_url') or asset.get('page_url') or ''})")
    if asset.get("page_url"):
        lines.append(f"**Fonte:** {asset['page_url']}")
    if asset.get("auto_reason"):
        lines.append(f"**Motivo da seleção:** {asset['auto_reason']}")
    return lines


def _rejected_take_lines(rejected: list[dict]) -> list[str]:
    if not rejected:
        return []
    lines = ["", f"Rejeitados ({len(rejected)}):"]
    for r in rejected:
        rnd = f" (rodada {r.get('review_round')})" if r.get("review_round") else ""
        lines.append(
            f"- {r.get('source')} {r.get('asset_type', 'video')} "
            f"{r.get('width')}x{r.get('height')} — {r.get('page_url') or r.get('download_url')}{rnd}"
        )
    return lines


def _scene_report_lines(scene: dict, asset: Optional[dict], rejected: list[dict]) -> list[str]:
    lines = [
        f"## {scene['scene_id']} | {scene['start_time']:.1f}s–{scene['end_time']:.1f}s | {scene.get('zone') or 'cena'}",
        "",
        f"> {scene.get('narration', '')}",
        "",
    ]
    lines.extend(_accepted_take_lines(asset))
    lines.extend(_rejected_take_lines(rejected))
    lines.append("")
    return lines


def build_curation_report(
    project: dict,
    scenes: list[dict],
    chosen_by_scene: dict[int, dict],
    rejected_by_scene: dict[int, list[dict]],
    review_round: int = 0,
) -> str:
    """Relatorio em Markdown da curadoria revisada (cena, frase, take aceito, rejeitados)."""
    total = len(scenes)
    accepted = len(chosen_by_scene)
    rejected_total = sum(len(v) for v in rejected_by_scene.values())
    lines = [
        f"# Relatório de curadoria — {project['name']}",
        "",
        f"- Cenas: {total}",
        f"- Takes aceitos: {accepted}",
        f"- Takes rejeitados ao longo da curadoria: {rejected_total}",
        f"- Rodadas de re-busca: {review_round}",
        "",
        "---",
        "",
    ]
    for scene in scenes:
        lines.extend(
            _scene_report_lines(
                scene,
                chosen_by_scene.get(scene["id"]),
                rejected_by_scene.get(scene["id"]) or [],
            )
        )
    return "\n".join(lines)


def _licenses_md() -> str:
    return "\n".join(
        [
            "# Licencas e fontes",
            "",
            "Este pacote pode conter assets de Pexels, Pixabay, Coverr, Openverse, Wikimedia, pesquisa profunda e imagens geradas pelo usuario.",
            "A licenca final depende da origem de cada arquivo.",
            "",
            "- Consulte `metadata/source_manifest.json` para todas as fontes usadas no pacote.",
            "- Consulte `metadata/pexels_sources.json` para URLs, autores e paginas Pexels.",
            "- Consulte `metadata/pixabay_sources.json` para URLs, autores e paginas Pixabay.",
            "- Consulte `metadata/generated_sources.json` para imagens adicionadas pelo usuario.",
            "- Consulte `editorial_report.json` para riscos editoriais e revisoes recomendadas.",
            "- Consulte `metadata/rejected_assets.json` para auditoria de curadoria.",
            "",
            "Nao redistribua este pacote como CC0 sem revisar as licencas dos provedores.",
        ]
    )


def _empty_sources() -> dict[str, list]:
    return {"pexels": [], "pixabay": [], "generated": [], "other": [], "all": []}


def _apply_download_results(jobs: list, ok_flags: list, file_by_scene: dict) -> dict[str, list]:
    """Processa o resultado dos downloads: registra fontes e zera seleções que falharam."""
    sources = _empty_sources()
    for (scene, gscene, asset, filename, _dest), ok in zip(jobs, ok_flags):
        if not ok:
            # falhou o download: remove a selecao do guia para nao apontar para arquivo inexistente
            gscene["selected_asset"] = None
            gscene["source_metadata"] = None
            continue
        file_by_scene[scene["scene_id"]] = _dest
        record = {
            "scene_id": scene["scene_id"],
            "file": f"assets/{filename}",
            **(gscene.get("source_metadata") or {}),
        }
        sources["all"].append(record)
        if asset["source"] == "pexels":
            sources["pexels"].append(record)
        elif asset["source"] == "pixabay":
            sources["pixabay"].append(record)
        elif asset["source"] == "generated":
            sources["generated"].append(record)
        else:
            sources["other"].append(record)
    return sources


def _write_package_zip(
    zip_path: Path,
    project: dict,
    file_by_scene: dict,
    guide: dict,
    sources: dict[str, list],
    rejected_assets: list[dict],
    edit_plan: Optional[dict],
    editorial_report: Optional[dict],
    extra_files: Optional[list[Path]],
) -> None:
    sources = sources or _empty_sources()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for _scene_code, src in file_by_scene.items():
            zf.write(src, f"assets/{src.name}")
        zf.writestr("guia_visual.json", json.dumps(guide, ensure_ascii=False, indent=2))
        zf.writestr("guia_visual.csv", _guide_to_csv(guide))
        zf.writestr("roteiro_com_brolls.md", _guide_to_md(project, guide))
        zf.writestr("LICENSES.md", _licenses_md())
        zf.writestr("metadata/source_manifest.json", json.dumps(sources["all"], ensure_ascii=False, indent=2))
        zf.writestr("metadata/pexels_sources.json", json.dumps(sources["pexels"], ensure_ascii=False, indent=2))
        zf.writestr("metadata/pixabay_sources.json", json.dumps(sources["pixabay"], ensure_ascii=False, indent=2))
        zf.writestr("metadata/generated_sources.json", json.dumps(sources["generated"], ensure_ascii=False, indent=2))
        zf.writestr("metadata/other_sources.json", json.dumps(sources["other"], ensure_ascii=False, indent=2))
        zf.writestr("metadata/rejected_assets.json", json.dumps(rejected_assets, ensure_ascii=False, indent=2))
        if edit_plan:
            zf.writestr("edit_plan.json", json.dumps(edit_plan, ensure_ascii=False, indent=2))
        if editorial_report:
            zf.writestr("editorial_report.json", json.dumps(editorial_report, ensure_ascii=False, indent=2))
        for extra in extra_files or []:
            if extra and extra.exists():
                zf.write(extra, extra.name)


def _fetch_asset(job: tuple, work_dir: Path, max_bytes: int) -> bool:
    _scene, _gscene, asset, _filename, dest = job
    if asset.get("source") == "generated":
        return _copy_generated(asset, work_dir, dest, max_bytes)
    return _download(asset["download_url"], dest, max_bytes)


def _build_download_jobs(
    scenes: list[dict], guide: dict, selected_by_scene: dict, tmp: Path
) -> list[tuple]:
    jobs = []
    for scene, gscene in zip(scenes, guide["scenes"]):
        asset = selected_by_scene.get(scene["id"])
        if not asset or not gscene.get("selected_asset"):
            continue
        filename = Path(gscene["selected_asset"]).name
        dest = tmp / filename
        logger.info("zip: baixando %s <- %s %sx%s", scene["scene_id"], asset["source"], asset.get("width"), asset.get("height"))
        jobs.append((scene, gscene, asset, filename, dest))
    return jobs


def _run_download_jobs(
    jobs: list[tuple],
    work_dir: Path,
    max_bytes: int,
    progress: Optional[Callable[[int, int, dict, bool], None]] = None,
) -> list[bool]:
    if not jobs:
        return []
    ok_flags = [False] * len(jobs)
    workers = min(DOWNLOAD_WORKERS, len(jobs))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_fetch_asset, job, work_dir, max_bytes): idx
            for idx, job in enumerate(jobs)
        }
        done = 0
        for future in as_completed(futures):
            idx = futures[future]
            ok = False
            try:
                ok = bool(future.result())
            except Exception as exc:  # noqa: BLE001
                logger.warning("download worker falhou: %s", exc)
            ok_flags[idx] = ok
            done += 1
            if progress:
                scene = jobs[idx][0]
                progress(done, len(jobs), scene, ok)
    return ok_flags


def build_zip(
    project: dict,
    config: dict,
    scenes: list[dict],
    selected_by_scene: dict[int, dict],
    rejected_assets: list[dict],
    work_dir: Path,
    max_download_mb: int = 90,
    edit_plan: Optional[dict] = None,
    editorial_report: Optional[dict] = None,
    extra_files: Optional[list[Path]] = None,
    zip_basename: str = "",
    progress: Optional[Callable[[int, int, dict, bool], None]] = None,
) -> Path:
    """Baixa assets selecionados, renomeia e monta o ZIP final. Retorna o caminho."""
    work_dir.mkdir(parents=True, exist_ok=True)
    guide = build_guide(project, config, scenes, selected_by_scene)
    for scene, gscene in zip(scenes, guide["scenes"]):
        if scene["id"] not in selected_by_scene and gscene.get("broll", True):
            gscene["broll"] = False
    max_bytes = int(max_download_mb * 1024 * 1024)

    tmp = work_dir / "assets_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    file_by_scene: dict[str, Path] = {}

    jobs = _build_download_jobs(scenes, guide, selected_by_scene, tmp)

    ok_flags = _run_download_jobs(jobs, work_dir, max_bytes, progress=progress)

    sources = _apply_download_results(jobs, ok_flags, file_by_scene)

    if not file_by_scene:
        raise RuntimeError(
            "nenhum asset selecionado conseguiu ser baixado; "
            "verifique URLs expiradas, limite de MB ou conexao com os provedores"
        )
    # cenas avatar-only (broll=False) nao tem asset de proposito: nao sao "faltando".
    # Ja um take escolhido que falha no download continua sendo erro, porque o pacote
    # ficaria com menos imagens do que o usuario decidiu usar.
    missing = [
        gscene["id"] for scene, gscene in zip(scenes, guide["scenes"])
        if scene["id"] in selected_by_scene and not gscene.get("selected_asset")
    ]
    if missing:
        preview = ", ".join(missing[:8])
        suffix = "..." if len(missing) > 8 else ""
        raise RuntimeError(
            "pacote incompleto; faltam assets validos para todas as cenas. "
            f"Faltando: {preview}{suffix}"
        )

    safe_name = _slug(zip_basename or project["name"]) or "asset_pack"
    zip_path = work_dir / f"asset_pack_{safe_name}.zip"
    _write_package_zip(
        zip_path, project, file_by_scene, guide, sources, rejected_assets, edit_plan, editorial_report, extra_files
    )
    return zip_path
