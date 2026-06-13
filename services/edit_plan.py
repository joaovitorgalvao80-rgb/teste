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
import re

from . import llm_service
from .script_parser import remove_accents

# Frases de apresentacao/saudacao: nessas cenas o apresentador fica na tela
# (sem b-roll por cima). Ex.: "eu sou", "olá, eu sou", "meu nome é".
_PRESENT_STRONG = (
    "eu sou ", "meu nome ", "me chamo ", "quem fala ", "quem te fala ",
    "aqui e o ", "aqui e a ", "aqui quem fala", "sou o ", "sou a ",
)
_PRESENT_GREETING = (
    "ola", "oi ", "oi,", "oi!", "e ai", "fala galera", "fala pessoal",
    "bem vindo", "bem-vindo", "seja bem", "sejam bem",
)


def _is_presentation(narration: str) -> bool:
    """True quando a cena e apresentacao/saudacao (apresentador, sem b-roll)."""
    text = " " + remove_accents(str(narration or "")).lower().strip() + " "
    if any(p in text for p in _PRESENT_STRONG):
        return True
    head = text[:40]  # saudacao so conta no comeco da fala
    return any(g in head for g in _PRESENT_GREETING)

EDIT_PLAN_VERSION = 2
NARRATION_BASENAME = "narration"
AVATAR_BASENAME = "avatar"

# Regras de alternancia avatar/b-roll quando o avatar e a base do video:
# - o avatar nunca fica mais de 30s sozinho na tela;
# - o b-roll nunca cobre o video inteiro (o avatar precisa aparecer).
MAX_AVATAR_SOLO_SECONDS = 30.0
MAX_BROLL_RUN_SECONDS = 22.0

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


def _caption_text(scene: dict) -> str:
    """Texto da legenda da cena.

    Prefere o overlay_text do brief; quando ele vem vazio (caso comum), deriva
    uma frase curta da narracao (primeira oracao, ate 5 palavras, MAIUSCULA) —
    garantindo que o lower-third do HyperFrames sempre tenha o que mostrar.
    """
    overlay = _clean_caption(scene.get("overlay_text") or "")
    if overlay:
        return overlay
    narration = str(scene.get("narration") or "").strip()
    if not narration:
        return ""
    clause = re.split(r"[.!?;:,]", narration)[0]
    return _clean_caption(clause, max_words=5).upper()


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


def _scene_duration(scene: dict) -> float:
    duration = float(scene.get("duration") or 0)
    if duration <= 0:
        duration = max(float(scene.get("end_time") or 0) - float(scene.get("start_time") or 0), 0.5)
    return duration


def _broll_flags(scenes: list[dict]) -> list[bool]:
    """Decide, cena a cena, se o b-roll cobre o avatar (deterministico).

    O avatar e a camada base; o gancho inicial e o fechamento ficam com o
    apresentador na tela, e corridas longas de b-roll ganham um respiro.
    """
    n = len(scenes)
    flags = [False] * n
    run = 0.0
    for i, scene in enumerate(scenes):
        if i == 0 or i == n - 1:
            run = 0.0
            continue
        # apresentacao/saudacao fica com o apresentador na tela (sem b-roll)
        if _is_presentation(scene.get("narration")):
            run = 0.0
            continue
        duration = _scene_duration(scene)
        if run >= MAX_BROLL_RUN_SECONDS:
            run = 0.0
            continue
        flags[i] = True
        run += duration
    return flags


def decide_broll(scenes: list[dict]) -> list[bool]:
    """Decisao FINAL de b-roll por cena (a mesma que o render usa): flags base +
    regra de apresentacao + guarda de avatar-solo. Determinístico, para a busca
    saber de antemao quais cenas NAO levam imagem (e nem buscar pra elas)."""
    flags = _broll_flags(scenes)
    plan = [{"duration": _scene_duration(s), "broll": flags[i]} for i, s in enumerate(scenes)]
    enforce_broll_policy(plan, scenes)
    return [bool(p["broll"]) for p in plan]


def _avatar_solo_runs(plan_scenes: list[dict]) -> list[list[int]]:
    runs: list[list[int]] = []
    current: list[int] = []
    for idx, scene in enumerate(plan_scenes):
        if scene.get("broll"):
            if current:
                runs.append(current)
                current = []
        else:
            current.append(idx)
    if current:
        runs.append(current)
    return runs


def enforce_broll_policy(plan_scenes: list[dict], source_scenes: list[dict]) -> dict:
    """Garante as regras de alternancia mesmo quando o LLM decide o b-roll.

    1. Nunca 100% b-roll: o avatar precisa abrir o video na tela.
    2. Nenhum trecho de avatar sozinho pode passar de MAX_AVATAR_SOLO_SECONDS.
    Retorna o resumo de cobertura para o plano.
    """
    n = len(plan_scenes)
    if n == 0:
        return {"coverage": 0.0, "max_avatar_solo_seconds": MAX_AVATAR_SOLO_SECONDS}

    # apresentacao/saudacao nunca leva b-roll (vale p/ deterministico e LLM)
    for scene, src in zip(plan_scenes, source_scenes):
        if _is_presentation(src.get("narration")):
            scene["broll"] = False

    if all(scene.get("broll") for scene in plan_scenes):
        plan_scenes[0]["broll"] = False

    while True:
        oversized = None
        for run in _avatar_solo_runs(plan_scenes):
            solo = sum(float(plan_scenes[i].get("duration") or 0) for i in run)
            if solo > MAX_AVATAR_SOLO_SECONDS and len(run) > 1:
                oversized = run
                break
        if not oversized:
            break
        # vira b-roll a cena mais forte do trecho (sem mexer no gancho inicial
        # nem em cenas de apresentacao, que devem ficar com o apresentador)
        candidates = [
            i for i in oversized
            if i != 0 and not _is_presentation(source_scenes[i].get("narration"))
        ]
        if not candidates:
            candidates = [i for i in oversized if i != 0] or oversized
        middle = oversized[len(oversized) // 2]
        best = max(
            candidates,
            key=lambda i: (_scene_score(source_scenes[i], i, n), -abs(i - middle)),
        )
        plan_scenes[best]["broll"] = True

    total = sum(float(scene.get("duration") or 0) for scene in plan_scenes)
    covered = sum(
        float(scene.get("duration") or 0) for scene in plan_scenes if scene.get("broll")
    )
    return {
        "coverage": round(covered / total, 3) if total else 0.0,
        "max_avatar_solo_seconds": MAX_AVATAR_SOLO_SECONDS,
        "broll_scenes": sum(1 for scene in plan_scenes if scene.get("broll")),
        "total_scenes": n,
    }


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
            raw_caption = _caption_text(source_scenes[idx])
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
    broll_flags = _broll_flags(scenes)
    plan_scenes = []
    for i, scene in enumerate(scenes):
        duration = _scene_duration(scene)
        start = round(float(scene.get("start_time") or 0), 3)
        caption = _caption_text(scene) if i in captioned_indexes else ""
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
                # usa a decisao ja tomada na cena (busca/mapa) quando presente,
                # garantindo que render e busca concordem; senao recalcula.
                "broll": bool(scene["broll"]) if scene.get("broll") is not None else broll_flags[i],
            }
        )
    broll_policy = enforce_broll_policy(plan_scenes, scenes)

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
        "broll_policy": broll_policy,
        "scenes": plan_scenes,
        "audio": None,
        "avatar": None,
    }
    if narration_file:
        plan["audio"] = {"src": narration_file, "volume": 1.0}
    if avatar_file:
        # O avatar e a base do video: tela cheia, com os b-rolls por cima.
        plan["avatar"] = {
            "src": avatar_file,
            "mode": "base",
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
        # o LLM so pode REMOVER b-roll (virar avatar); nunca CRIAR b-roll, pois
        # a cena pode nao ter asset buscado (decisao avatar-only tomada na busca).
        if directive.get("broll") is False:
            scene["broll"] = False
        # ultima cena nunca tem transicao de saida
        if i == last_idx:
            scene["transition_out"] = "none"
    enforce_caption_policy(plan["scenes"], scenes, fallback_to_source=False)
    # mesmo com o LLM decidindo, as regras de avatar/b-roll sao inegociaveis
    plan["broll_policy"] = enforce_broll_policy(plan["scenes"], scenes)
    plan["caption_policy"]["selected"] = sum(1 for scene in plan["scenes"] if scene["caption"])
    plan["editorial"] = "llm"
    return plan
