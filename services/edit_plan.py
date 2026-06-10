"""Gera o edit_plan.json deterministico para o refinamento HyperFrames.

Primeira versao sem IA: usa as cenas/timestamps existentes do projeto.
Motions alternam entre push-in e pull-out (seguros, sem mostrar borda),
transicoes sao fades curtos nos limites de cena e captions vem do
overlay_text de cada cena. Quando OpenRouter/NVIDIA entrarem como cerebro
editorial, este modulo vira o fallback e o validador do plano gerado.
"""
from __future__ import annotations

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
