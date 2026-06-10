"""Gera o edit_plan.json para o refinamento HyperFrames.

build_edit_plan: deterministico, sem IA - motions alternados, fades nos
limites de cena, captions do overlay_text. E o fallback garantido.

build_edit_plan_with_llm: tenta o cerebro editorial (OpenRouter via
services/llm_service) e aplica as decisoes validas por cima do plano
deterministico. start/duration vem sempre das cenas reais do projeto;
o LLM nunca altera timing.
"""
from __future__ import annotations

from . import llm_service

EDIT_PLAN_VERSION = 1
NARRATION_BASENAME = "narration"
AVATAR_BASENAME = "avatar"

_MOTION_CYCLE = ["slow_push_in", "slow_pull_out"]
_TRANSITION_DEFAULT = "fade"


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
    plan_scenes = []
    for i, scene in enumerate(scenes):
        duration = float(scene.get("duration") or 0)
        if duration <= 0:
            duration = max(float(scene.get("end_time") or 0) - float(scene.get("start_time") or 0), 0.5)
        plan_scenes.append(
            {
                "scene_id": scene.get("scene_id", f"scene_{i + 1:03d}"),
                "start": round(float(scene.get("start_time") or 0), 3),
                "duration": round(duration, 3),
                "motion": _MOTION_CYCLE[i % len(_MOTION_CYCLE)],
                "transition_out": _TRANSITION_DEFAULT if i < len(scenes) - 1 else "none",
                "caption": (scene.get("overlay_text") or "").strip(),
            }
        )

    plan: dict = {
        "version": EDIT_PLAN_VERSION,
        "project_name": project.get("name", ""),
        "resolution": config.get("resolution", "1920x1080"),
        "fps": 30,
        "caption_position": "left" if safe_area == "right" else "right",
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
        if directive["caption"]:
            scene["caption"] = directive["caption"]
        # ultima cena nunca tem transicao de saida
        if i == last_idx:
            scene["transition_out"] = "none"
    plan["editorial"] = "llm"
    return plan
