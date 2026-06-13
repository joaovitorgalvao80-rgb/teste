"""Configuração de projeto: defaults, coerção e normalização.

Funções puras (sem estado global da app), extraídas de app.py para reduzir o
monólito e permitir testar/raciocinar sobre a config isoladamente.
"""
from __future__ import annotations

import json
from typing import Optional

DEFAULT_CONFIG = {
    "format": "16:9",
    "resolution": "1920x1080",
    "avatar_safe_area": "right",
    "avatar_safe_width_ratio": 0.30,
    "asset_type_priority": "video",
    "image_fallback": False,
    "visual_style": "realistic editorial YouTube B-roll, concrete scenes, rural Brazil when relevant",
    "script_language": "pt-BR",
    "keyword_language": "english",
    "scene_duration": 4.0,
    "per_keyword": 8,
    "max_download_mb": 90,
    "long_mode": False,
    "part_target_seconds": 150,
}

ALLOWED_RESOLUTIONS = {"1920x1080", "1280x720"}
ALLOWED_SAFE_AREAS = {"left", "right"}
MIN_SCENE_DURATION = 2.0
MAX_SCENE_DURATION = 8.0
MIN_AVATAR_SAFE_RATIO = 0.10
MAX_AVATAR_SAFE_RATIO = 0.45


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "sim"}
    return bool(value)


def _coerce_float(value: object, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return min(max(number, minimum), maximum)


def _coerce_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return min(max(number, minimum), maximum)


def normalize_project_config(raw_config: Optional[dict] = None) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if raw_config:
        cfg.update(raw_config)
    if cfg.get("resolution") not in ALLOWED_RESOLUTIONS:
        cfg["resolution"] = DEFAULT_CONFIG["resolution"]
    if cfg.get("avatar_safe_area") not in ALLOWED_SAFE_AREAS:
        cfg["avatar_safe_area"] = DEFAULT_CONFIG["avatar_safe_area"]
    cfg["scene_duration"] = _coerce_float(
        cfg.get("scene_duration"),
        DEFAULT_CONFIG["scene_duration"],
        MIN_SCENE_DURATION,
        MAX_SCENE_DURATION,
    )
    cfg["avatar_safe_width_ratio"] = _coerce_float(
        cfg.get("avatar_safe_width_ratio"),
        DEFAULT_CONFIG["avatar_safe_width_ratio"],
        MIN_AVATAR_SAFE_RATIO,
        MAX_AVATAR_SAFE_RATIO,
    )
    cfg["per_keyword"] = _coerce_int(cfg.get("per_keyword"), DEFAULT_CONFIG["per_keyword"], 1, 20)
    cfg["max_download_mb"] = _coerce_int(
        cfg.get("max_download_mb"), DEFAULT_CONFIG["max_download_mb"], 5, 500
    )
    cfg["image_fallback"] = _coerce_bool(cfg.get("image_fallback"))
    cfg["long_mode"] = _coerce_bool(cfg.get("long_mode"))
    cfg["part_target_seconds"] = _coerce_int(
        cfg.get("part_target_seconds"), DEFAULT_CONFIG["part_target_seconds"], 30, 300
    )
    visual_style = str(cfg.get("visual_style") or "").strip()
    cfg["visual_style"] = visual_style or DEFAULT_CONFIG["visual_style"]
    return cfg


def project_config(project: dict) -> dict:
    try:
        stored = json.loads(project.get("config_json") or "{}")
    except json.JSONDecodeError:
        stored = {}
    return normalize_project_config(stored)


def resolution_width(config: dict) -> int:
    return int(str(config.get("resolution") or DEFAULT_CONFIG["resolution"]).split("x", 1)[0])
