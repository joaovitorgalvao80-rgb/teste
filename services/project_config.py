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
    "script_language": "pt",
    "keyword_language": "english",
    "scene_duration": 4.0,
    "per_keyword": 8,
    "max_download_mb": 90,
    "long_mode": False,
    "part_target_seconds": 150,
    # broll_density: quanto do vídeo é coberto por b-roll
    #   key_moments   → só cenas com score alto (momentos-chave, ~30-40%)
    #   moderate      → padrão: alterna com respiro a cada 22s de b-roll
    #   full_coverage → quase tudo com b-roll, pouquíssimas pausas (~80-90%)
    "broll_density": "moderate",
    # video_style: estrutura geral do vídeo
    #   avatar_broll → avatar como base; b-rolls sobrepõem nas cenas marcadas
    #   broll_only   → sem avatar; b-roll ocupa 100% da tela em todas as cenas
    "video_style": "avatar_broll",
    "visual_source_mode": "stock",
    "visual_coverage": "planned",
    "missing_visual_policy": "fallback_avatar",
}

ALLOWED_BROLL_DENSITIES = {"key_moments", "moderate", "full_coverage"}
ALLOWED_VIDEO_STYLES = {"avatar_broll", "broll_only"}
ALLOWED_VISUAL_SOURCE_MODES = {"stock", "ai_preferred", "ai_required", "manual_upload"}
ALLOWED_VISUAL_COVERAGES = {"planned", "full_required"}
ALLOWED_MISSING_VISUAL_POLICIES = {"fallback_avatar", "block_package"}

# Idiomas suportados para roteiro/transcrição/overlay. Fonte única de verdade.
#   whisper: código ISO 639-1 enviado ao Whisper na transcrição.
#   name:    nome em inglês usado nos prompts da Groq (descrição do roteiro
#            e instrução de idioma do overlay_text).
#   label:   rótulo exibido no seletor da UI.
# As keywords de busca (Pexels/Pixabay) permanecem SEMPRE em inglês,
# independentemente do idioma — melhor cobertura de resultados.
LANGUAGES = {
    "pt": {"whisper": "pt", "name": "Brazilian Portuguese", "label": "Português (BR)"},
    "en": {"whisper": "en", "name": "English", "label": "English"},
    "es": {"whisper": "es", "name": "Spanish", "label": "Español"},
    "fr": {"whisper": "fr", "name": "French", "label": "Français"},
    "pl": {"whisper": "pl", "name": "Polish", "label": "Polski"},
    "de": {"whisper": "de", "name": "German", "label": "Deutsch"},
    "it": {"whisper": "it", "name": "Italian", "label": "Italiano"},
}
DEFAULT_LANGUAGE = "pt"


def normalize_language(value: object) -> str:
    """Normaliza um código de idioma para uma chave válida de LANGUAGES.

    Aceita o legado "pt-BR" e variantes com região (ex.: "en-US") reduzindo ao
    código base; cai em DEFAULT_LANGUAGE para qualquer valor desconhecido.
    """
    code = str(value or "").strip().lower().replace("_", "-")
    if code in LANGUAGES:
        return code
    base = code.split("-", 1)[0]
    return base if base in LANGUAGES else DEFAULT_LANGUAGE


def language_whisper_code(value: object) -> str:
    return LANGUAGES[normalize_language(value)]["whisper"]


def language_name(value: object) -> str:
    """Nome em inglês do idioma, para usar nos prompts da Groq."""
    return LANGUAGES[normalize_language(value)]["name"]


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
    cfg["script_language"] = normalize_language(cfg.get("script_language"))
    if cfg.get("broll_density") not in ALLOWED_BROLL_DENSITIES:
        cfg["broll_density"] = DEFAULT_CONFIG["broll_density"]
    if cfg.get("video_style") not in ALLOWED_VIDEO_STYLES:
        cfg["video_style"] = DEFAULT_CONFIG["video_style"]
    if cfg.get("visual_source_mode") not in ALLOWED_VISUAL_SOURCE_MODES:
        cfg["visual_source_mode"] = DEFAULT_CONFIG["visual_source_mode"]
    if cfg.get("visual_coverage") not in ALLOWED_VISUAL_COVERAGES:
        cfg["visual_coverage"] = (
            "full_required" if cfg["video_style"] == "broll_only" else DEFAULT_CONFIG["visual_coverage"]
        )
    if cfg.get("missing_visual_policy") not in ALLOWED_MISSING_VISUAL_POLICIES:
        cfg["missing_visual_policy"] = (
            "block_package" if cfg["video_style"] == "broll_only" else DEFAULT_CONFIG["missing_visual_policy"]
        )
    if cfg["video_style"] == "broll_only":
        cfg["visual_coverage"] = "full_required"
        cfg["missing_visual_policy"] = "block_package"
    if cfg["missing_visual_policy"] == "block_package":
        cfg["visual_coverage"] = "full_required"
    return cfg


def project_config(project: dict) -> dict:
    try:
        stored = json.loads(project.get("config_json") or "{}")
    except json.JSONDecodeError:
        stored = {}
    return normalize_project_config(stored)


def resolution_width(config: dict) -> int:
    return int(str(config.get("resolution") or DEFAULT_CONFIG["resolution"]).split("x", 1)[0])
