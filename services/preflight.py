"""Preflight checks before packaging/rendering project assets."""
from __future__ import annotations


def asset_quality_issues(row: dict, scene_dur: float, target_w: int) -> list[str]:
    issues: list[str] = []
    width = int(row.get("width") or 0)
    height = int(row.get("height") or 0)
    duration = float(row.get("duration") or 0)
    if row.get("asset_type") == "video":
        if 0 < width < target_w * 0.66:
            issues.append(f"resolucao baixa ({width}x{height}, minimo recomendado {int(target_w * 0.66)}p)")
        if 0 < duration < scene_dur * 0.4:
            issues.append(f"clip curto ({duration:.1f}s para cena de {scene_dur:.1f}s; sera loopado)")
    elif row.get("asset_type") == "image" and 0 < width < target_w * 0.5:
        issues.append(f"imagem pequena ({width}x{height})")
    if (row.get("vision_verdict") or "") == "descartar":
        issues.append("IA de visao marcou como inadequado")
    return issues


def selected_quality_warnings(
    selected_rows: list[dict],
    scenes_by_db_id: dict[int, dict],
    target_w: int,
) -> list[dict]:
    warnings: list[dict] = []
    for row in selected_rows:
        scene = scenes_by_db_id.get(row.get("scene_id"))
        scene_dur = float(scene.get("duration") or 4.0) if scene else 4.0
        scene_code = scene.get("scene_id", "?") if scene else "?"
        issues = asset_quality_issues(row, scene_dur, target_w)
        if issues:
            warnings.append({"scene_id": scene_code, "issues": issues})
    return warnings


def _missing_required_scenes(
    scenes: list[dict],
    selected_by_scene: dict[int, dict],
    required_scene_db_ids: set[int],
) -> list[dict]:
    missing: list[dict] = []
    for scene in scenes:
        if scene["id"] not in required_scene_db_ids or scene["id"] in selected_by_scene:
            continue
        missing.append({
            "scene_id": scene.get("scene_id", ""),
            "scene_db_id": scene.get("id"),
            "issues": ["sem take obrigatorio escolhido"],
            "severity": "blocker",
        })
    return missing


def build_package_preflight(
    *,
    scenes: list[dict],
    config: dict,
    selected_rows: list[dict],
    required_scene_db_ids: set[int],
    target_w: int,
    problem_items: list[dict] | None = None,
) -> dict:
    selected_by_scene = {row["scene_id"]: row for row in selected_rows}
    scenes_by_db_id = {scene["id"]: scene for scene in scenes}
    quality = selected_quality_warnings(selected_rows, scenes_by_db_id, target_w)
    blockers: list[dict] = []

    if not required_scene_db_ids and not selected_by_scene:
        blockers.append({
            "scene_id": "",
            "scene_db_id": None,
            "issues": ["o plano atual nao tem cenas de b-roll para empacotar"],
            "severity": "blocker",
        })
    elif config.get("missing_visual_policy") == "block_package":
        blockers.extend(_missing_required_scenes(scenes, selected_by_scene, required_scene_db_ids))

    if required_scene_db_ids and not any(scene_id in selected_by_scene for scene_id in required_scene_db_ids):
        if not blockers:
            blockers.append({
                "scene_id": "",
                "scene_db_id": None,
                "issues": ["selecione ao menos um asset antes de gerar o pacote"],
                "severity": "blocker",
            })

    problem_scenes = list(problem_items or [])
    status = "ok"
    if blockers:
        status = "blocked"
    elif quality or problem_scenes:
        status = "warn"

    return {
        "status": status,
        "blockers": blockers,
        "blocker_total": len(blockers),
        "warnings": quality,
        "total": len(quality),
        "problem_scenes": problem_scenes,
        "problem_total": len(problem_scenes),
        "selected_total": len(selected_rows),
        "required_total": len(required_scene_db_ids),
    }
