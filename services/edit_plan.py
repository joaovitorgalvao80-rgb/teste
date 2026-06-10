"""Gera o edit_plan.json para o refinamento HyperFrames.

build_edit_plan: deterministico, sem IA - motions alternados, fades nos
limites de cena, captions do overlay_text. E o fallback garantido.

build_edit_plan_with_llm: tenta o cerebro editorial (OpenRouter via
services/llm_service) e aplica as decisoes validas por cima do plano
deterministico. start/duration vem sempre das cenas reais do projeto;
o LLM nunca altera timing.
"""
from __future__ import annotations

import math

from . import llm_service

EDIT_PLAN_VERSION = 1
NARRATION_BASENAME = "narration"
AVATAR_BASENAME = "avatar"

MAX_CAPTION_DENSITY = 0.38
KEY_TERMS = {
    "agora",
    "alerta",
    "atenção",
    "atencao",
    "barata",
    "cuidado",
    "dica",
    "erro",
    "importante",
    "mas",
    "nunca",
    "perigo",
    "problema",
    "segredo",
    "solução",
    "solucao",
}


def _clean_caption(text: str, max_words: int = 6) -> str:
    words = str(text or "").strip().replace("\n", " ").split()
    if not words:
        return ""
    return " ".join(words[:max_words])[:80].strip(" ,.;:-")


def _scene_score(scene: dict, idx: int, total: int) -> int:
    narration = str(scene.get("narration") or "").lower()
    overlay = str(scene.get("overlay_text") or "").strip()
    score = 0
    if overlay:
        score += 1
    if idx == 0:
        score += 3
    if idx == total - 1:
        score += 2
    if "?" in narration:
        score += 2
    if any(term in narration for term in KEY_TERMS):
        score += 2
    if any(ch.isdigit() for ch in narration):
        score += 1
    return score


def _caption_indexes(scenes: list[dict]) -> set[int]:
    if not scenes:
        return set()
    max_captions = max(1, math.ceil(len(scenes) * MAX_CAPTION_DENSITY))
    ranked = sorted(
        ((idx, _scene_score(scene, idx, len(scenes))) for idx, scene in enumerate(scenes)),
        key=lambda item: (-item[1], item[0]),
    )
    selected: list[int] = []
    for idx, score in ranked:
        if score <= 0:
            continue
        if len(selected) >= max_captions:
            break
        # Evita texto em cenas coladas quando ha outras opcoes boas.
        if any(abs(idx - current) <= 1 for current in selected) and len(selected) < max_captions - 1:
            continue
        selected.append(idx)
    return set(selected)


def _caption_timing(start: float, duration: float) -> tuple[float, float]:
    offset = min(0.55, max(duration * 0.16, 0.25))
    caption_duration = min(2.4, max(duration * 0.48, 0.85))
    if offset + caption_duration > duration:
        caption_duration = max(0.4, duration - offset - 0.05)
    return round(start + offset, 3), round(caption_duration, 3)


def _motion_for(idx: int, total: int, captioned: bool) -> str:
    if captioned or idx == 0:
        return "slow_push_in"
    if idx == total - 1:
        return "slow_pull_out"
    cycle = ["hold", "drift_left", "drift_right", "slow_pull_out"]
    return cycle[idx % len(cycle)]


def _transition_for(idx: int, total: int, current: dict, next_scene: dict | None) -> str:
    if idx >= total - 1:
        return "none"
    if next_scene and (current.get("zone") or "") != (next_scene.get("zone") or ""):
        return "fade"
    # Fade e recurso pontual; o ritmo padrao deve ser corte seco.
    return "fade" if (idx + 1) % 4 == 0 else "none"


def enforce_caption_policy(
    plan_scenes: list[dict],
    source_scenes: list[dict],
    fallback_to_source: bool = True,
) -> None:
    """Mantem captions pontuais mesmo quando o LLM tenta legendar tudo."""
    allowed = _caption_indexes(source_scenes)
    for idx, scene in enumerate(plan_scenes):
        if idx not in allowed:
            scene["caption"] = ""
            scene["caption_start"] = None
            scene["caption_duration"] = 0
            continue
        raw_caption = scene.get("caption")
        if fallback_to_source and not raw_caption:
            raw_caption = source_scenes[idx].get("overlay_text")
        caption = _clean_caption(raw_caption or "")
        scene["caption"] = caption
        start = float(scene.get("start") or 0)
        duration = float(scene.get("duration") or 0)
        cap_start, cap_duration = _caption_timing(start, duration)
        scene["caption_start"] = cap_start if caption else None
        scene["caption_duration"] = cap_duration if caption else 0


def build_edit_plan(
    project: dict,
    config: dict,
    scenes: list[dict],
    narration_file: str = "",
    avatar_file: str = "",
) -> dict:
    """Monta o plano de edicao a partir das cenas ja mapeadas do projeto.

    narration_file/avatar_file sao nomes relativos dentro do pacote
    (ex: "narration.mp3"); vazios quando o usuario nao enviou os arquivos.
    """
    safe_area = (config.get("avatar_safe_area") or "right").lower()
    captioned_indexes = _caption_indexes(scenes)
    plan_scenes = []
    for i, scene in enumerate(scenes):
        duration = float(scene.get("duration") or 0)
        if duration <= 0:
            duration = max(float(scene.get("end_time") or 0) - float(scene.get("start_time") or 0), 0.5)
        start = round(float(scene.get("start_time") or 0), 3)
        caption = _clean_caption(scene.get("overlay_text") or "") if i in captioned_indexes else ""
        cap_start, cap_duration = _caption_timing(start, duration)
        next_scene = scenes[i + 1] if i + 1 < len(scenes) else None
        plan_scenes.append(
            {
                "scene_id": scene.get("scene_id", f"scene_{i + 1:03d}"),
                "start": start,
                "duration": round(duration, 3),
                "motion": _motion_for(i, len(scenes), bool(caption)),
                "transition_out": _transition_for(i, len(scenes), scene, next_scene),
                "caption": caption,
                "caption_start": cap_start if caption else None,
                "caption_duration": cap_duration if caption else 0,
            }
        )

    plan: dict = {
        "version": EDIT_PLAN_VERSION,
        "project_name": project.get("name", ""),
        "resolution": config.get("resolution", "1920x1080"),
        "fps": 30,
        "caption_position": "left" if safe_area == "right" else "right",
        "caption_policy": {
            "max_density": MAX_CAPTION_DENSITY,
            "selected": sum(1 for scene in plan_scenes if scene["caption"]),
            "total_scenes": len(plan_scenes),
        },
        "editorial_mode": "deterministic_v2",
        "scenes": plan_scenes,
        "audio": None,
        "avatar": None,
    }
    if narration_file:
        plan["audio"] = {"src": narration_file, "volume": 1.0}
    if avatar_file:
        plan["avatar"] = {
            "src": avatar_file,
            "position": safe_area if safe_area in {"left", "right"} else "right",
            "scale": float(config.get("avatar_safe_width_ratio") or 0.30),
        }
    return plan


def build_edit_plan_with_llm(
    project: dict,
    config: dict,
    scenes: list[dict],
    openrouter_key: str = "",
    narration_file: str = "",
    avatar_file: str = "",
) -> dict:
    """Plano deterministico + decisoes editoriais do LLM quando disponiveis.

    O LLM so pode alterar motion, transition_out e caption, e somente com
    valores que o runner suporta (validados em llm_service). Timing e
    estrutura ficam sempre com o plano deterministico.
    """
    plan = build_edit_plan(
        project, config, scenes, narration_file=narration_file, avatar_file=avatar_file
    )
    if not openrouter_key:
        return plan

    directives = llm_service.generate_scene_directives(project, scenes, openrouter_key)
    if not directives:
        return plan

    last_idx = len(plan["scenes"]) - 1
    for i, scene in enumerate(plan["scenes"]):
        directive = directives.get(scene["scene_id"])
        if not directive:
            continue
        if directive["motion"]:
            scene["motion"] = directive["motion"]
        if directive["transition_out"]:
            scene["transition_out"] = directive["transition_out"]
        scene["caption"] = directive["caption"]
        # ultima cena nunca tem transicao de saida
        if i == last_idx:
            scene["transition_out"] = "none"
    enforce_caption_policy(plan["scenes"], scenes, fallback_to_source=False)
    plan["caption_policy"]["selected"] = sum(1 for scene in plan["scenes"] if scene["caption"])
    plan["editorial"] = "llm"
    return plan
