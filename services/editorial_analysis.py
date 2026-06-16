"""Deterministic editorial analysis for render planning.

The goal is to improve edit_plan decisions using metadata the app already has,
without adding heavy video-analysis dependencies to the Railway web process.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Iterable, Optional

REPORT_VERSION = 1

ALLOWED_MOTIONS = {"hold", "drift_left", "drift_right", "slow_push_in", "slow_pull_out"}
ALLOWED_TRANSITIONS = {"none", "fade"}
HIGH_SIGNAL_TERMS = {
    "alerta",
    "atencao",
    "atenção",
    "cuidado",
    "erro",
    "importante",
    "nunca",
    "perigo",
    "problema",
    "segredo",
    "solucao",
    "solução",
}


def _duration(scene: dict) -> float:
    try:
        value = float(scene.get("duration") or 0)
    except (TypeError, ValueError):
        value = 0.0
    if value <= 0:
        try:
            value = float(scene.get("end_time") or 0) - float(scene.get("start_time") or 0)
        except (TypeError, ValueError):
            value = 0.0
    return max(value, 0.0)


def _resolution(config: dict) -> tuple[int, int]:
    raw = str(config.get("resolution") or "1920x1080").lower()
    match = re.match(r"^\s*(\d+)\s*x\s*(\d+)\s*$", raw)
    if not match:
        return 1920, 1080
    return int(match.group(1)), int(match.group(2))


def _scene_score(scene: dict, idx: int, total: int) -> int:
    narration = str(scene.get("narration") or "").lower()
    overlay = str(scene.get("overlay_text") or "").strip()
    score = 0
    if overlay:
        score += 2
    if idx == 0:
        score += 3
    if idx == total - 1:
        score += 2
    if "?" in narration:
        score += 2
    if any(term in narration for term in HIGH_SIGNAL_TERMS):
        score += 2
    if any(ch.isdigit() for ch in narration):
        score += 1
    if _duration(scene) >= 8:
        score += 1
    return score


def _caption_priority(score: int) -> str:
    if score >= 4:
        return "high"
    if score >= 2:
        return "medium"
    return "low"


def _transition_hint(scene: dict, next_scene: Optional[dict], score: int) -> str:
    if not next_scene:
        return "none"
    if (scene.get("zone") or "") != (next_scene.get("zone") or ""):
        return "fade"
    return "fade" if score >= 5 else "none"


def _motion_hint(score: int, idx: int, total: int, asset: Optional[dict]) -> str:
    if score >= 4:
        return "slow_push_in"
    if idx == total - 1:
        return "slow_pull_out"
    if asset and asset.get("asset_type") == "video":
        return "hold"
    return "drift_left" if idx % 2 else "drift_right"


def _rejected_counts(rejected_assets: Iterable[dict]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for asset in rejected_assets or []:
        scene_id = str(asset.get("scene_id") or "")
        if scene_id:
            counts[scene_id] += 1
    return dict(counts)


def _asset_risks(asset: Optional[dict], scene_duration: float, target_w: int, target_h: int) -> list[str]:
    if not asset:
        return ["missing_selected_asset"]
    risks: list[str] = []
    asset_type = asset.get("asset_type") or "video"
    try:
        asset_duration = float(asset.get("duration") or 0)
    except (TypeError, ValueError):
        asset_duration = 0.0
    if asset_type == "video" and asset_duration and asset_duration < scene_duration * 0.7:
        risks.append("short_video_loop_risk")
    width = int(asset.get("width") or 0)
    height = int(asset.get("height") or 0)
    if width and width < target_w * 0.65:
        risks.append("low_width")
    if height and height < target_h * 0.65:
        risks.append("low_height")
    if asset.get("license") == "review_required":
        risks.append("license_review_required")
    if asset.get("source") in {"firecrawl", "exa"}:
        risks.append("deep_source_verify_terms")
    return risks


def _recommendations(scene_reports: list[dict]) -> list[str]:
    risks = Counter(risk for scene in scene_reports for risk in scene.get("risks", []))
    out: list[str] = []
    if risks.get("missing_selected_asset"):
        out.append("Ha cenas sem take selecionado; elas viram avatar-only no pacote.")
    if risks.get("short_video_loop_risk"):
        out.append("Alguns videos selecionados sao curtos para a cena; prefira take maior ou imagem estatica.")
    if risks.get("deep_source_verify_terms"):
        out.append("Fontes vindas de pesquisa profunda exigem revisao manual de termos/licenca.")
    if risks.get("low_width") or risks.get("low_height"):
        out.append("Ha assets abaixo da resolucao alvo; podem ficar suaves ou pixelados.")
    return out


def build_report(
    project: dict,
    config: dict,
    scenes: list[dict],
    selected_by_scene: dict[int, dict],
    rejected_assets: Optional[list[dict]] = None,
) -> dict:
    """Build a lightweight editorial report and per-scene render hints."""
    target_w, target_h = _resolution(config)
    rejected_by_scene = _rejected_counts(rejected_assets or [])
    total = len(scenes)
    scene_reports: list[dict] = []
    for idx, scene in enumerate(scenes):
        asset = selected_by_scene.get(scene.get("id"))
        duration = _duration(scene)
        score = _scene_score(scene, idx, total)
        risks = _asset_risks(asset, duration, target_w, target_h)
        rejected_count = rejected_by_scene.get(str(scene.get("scene_id")), 0)
        if rejected_count >= 2:
            risks.append("repeated_rejections")
        next_scene = scenes[idx + 1] if idx + 1 < total else None
        scene_reports.append(
            {
                "scene_id": scene.get("scene_id", f"scene_{idx + 1:03d}"),
                "db_scene_id": scene.get("id"),
                "score": score,
                "caption_priority": _caption_priority(score),
                "motion_hint": _motion_hint(score, idx, total, asset),
                "transition_hint": _transition_hint(scene, next_scene, score),
                "selected_source": (asset or {}).get("source", ""),
                "selected_asset_type": (asset or {}).get("asset_type", ""),
                "asset_duration": (asset or {}).get("duration", 0),
                "asset_resolution": {
                    "width": (asset or {}).get("width", 0),
                    "height": (asset or {}).get("height", 0),
                },
                "rejected_count": rejected_count,
                "risks": risks,
            }
        )
    broll_scenes = sum(1 for scene in scenes if scene.get("broll"))
    risk_count = sum(len(scene.get("risks", [])) for scene in scene_reports)
    return {
        "version": REPORT_VERSION,
        "editorial_mode": "assisted_v3",
        "project_name": project.get("name", ""),
        "summary": {
            "scenes": total,
            "broll_scenes": broll_scenes,
            "selected_assets": len(selected_by_scene),
            "risk_count": risk_count,
            "resolution": config.get("resolution", "1920x1080"),
            "video_style": config.get("video_style", "avatar_broll"),
            "broll_density": config.get("broll_density", "moderate"),
        },
        "recommendations": _recommendations(scene_reports),
        "scenes": scene_reports,
    }
