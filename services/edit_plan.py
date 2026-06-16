"""Gera o edit_plan.json que o render (FFmpeg) executa.

build_edit_plan: deterministico, sem IA - decide b-roll vs avatar por cena
(decide_broll), motions alternados, fades nos limites de cena e captions
do overlay_text/narracao. start/duration vem sempre das cenas reais.
"""
from __future__ import annotations

import math
import re

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


def _broll_override(scene: dict) -> int:
    """Override manual por cena: 0=auto, 1=forcar b-roll (sem avatar),
    -1=forcar avatar (sem b-roll). Vale como 'lock' nas policies abaixo."""
    try:
        value = int(scene.get("broll_override") or 0)
    except (TypeError, ValueError):
        return 0
    return max(-1, min(1, value))


def _apply_overrides(flags: list[bool], scenes: list[dict]) -> None:
    """Aplica os overrides manuais sobre os flags (in-place)."""
    for i, scene in enumerate(scenes):
        ov = _broll_override(scene)
        if ov == 1:
            flags[i] = True
        elif ov == -1:
            flags[i] = False


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


# Threshold de score para o modo key_moments (pontos-chave):
# cenas com score >= este valor recebem b-roll.
_KEY_MOMENTS_SCORE_THRESHOLD = 2


def _apply_density(
    flags: list[bool], i: int, scene: dict, total: int, run: float, density: str
) -> float:
    """Aplica o critério de densidade e marca flags[i]. Retorna o novo run."""
    if density == "key_moments":
        if _scene_score(scene, i, total) < _KEY_MOMENTS_SCORE_THRESHOLD:
            return 0.0
    elif density != "full_coverage" and run >= MAX_BROLL_RUN_SECONDS:
        return 0.0
    flags[i] = True
    return run + _scene_duration(scene)


def _broll_flags(scenes: list[dict], density: str = "moderate", video_style: str = "avatar_broll") -> list[bool]:
    """Decide, cena a cena, se o b-roll cobre o avatar (deterministico).

    density:
      moderate      → padrão: alterna com respiro a cada MAX_BROLL_RUN_SECONDS
      key_moments   → só cenas com score alto (pontos importantes/chave)
      full_coverage → quase tudo com b-roll, sem limite de corrida
    video_style:
      avatar_broll  → avatar é a base; primeira/última cena sem b-roll
      broll_only    → b-roll em todas as cenas (sem avatar para preservar)
    """
    n = len(scenes)
    flags = [False] * n
    run = 0.0

    for i, scene in enumerate(scenes):
        if _is_presentation(scene.get("narration")):
            run = 0.0
            continue
        if video_style != "broll_only" and (i == 0 or i == n - 1):
            run = 0.0
            continue
        run = _apply_density(flags, i, scene, n, run, density)

    _apply_overrides(flags, scenes)
    return flags


def decide_broll(scenes: list[dict], config: "dict | None" = None) -> list[bool]:
    """Decisao FINAL de b-roll por cena (a mesma que o render usa): flags base +
    regra de apresentacao + guarda de avatar-solo. Determinístico, para a busca
    saber de antemao quais cenas NAO levam imagem (e nem buscar pra elas)."""
    density = (config or {}).get("broll_density", "moderate")
    video_style = (config or {}).get("video_style", "avatar_broll")
    flags = _broll_flags(scenes, density=density, video_style=video_style)
    plan = [{"duration": _scene_duration(s), "broll": flags[i]} for i, s in enumerate(scenes)]
    if video_style != "broll_only":
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


def _find_oversized_run(plan_scenes: list[dict]) -> "list[int] | None":
    """Acha o primeiro trecho de avatar-solo acima do limite (com mais de 1 cena)."""
    for run in _avatar_solo_runs(plan_scenes):
        solo = sum(float(plan_scenes[i].get("duration") or 0) for i in run)
        if solo > MAX_AVATAR_SOLO_SECONDS and len(run) > 1:
            return run
    return None


def _pick_broll_target(oversized: list[int], source_scenes: list[dict], n: int) -> "int | None":
    """Escolhe a cena mais forte do trecho para virar b-roll (preserva gancho/apresentacao).

    Nunca escolhe uma cena travada em avatar (override -1): o pedido manual do
    usuario vence o guard de avatar-solo. Retorna None quando todo o trecho esta
    travado em avatar (nada a fazer)."""
    free = [i for i in oversized if _broll_override(source_scenes[i]) != -1]
    candidates = [
        i for i in free
        if i != 0 and not _is_presentation(source_scenes[i].get("narration"))
    ]
    if not candidates:
        candidates = [i for i in free if i != 0] or free
    if not candidates:
        return None
    middle = oversized[len(oversized) // 2]
    return max(
        candidates,
        key=lambda i, middle=middle: (_scene_score(source_scenes[i], i, n), -abs(i - middle)),
    )


def _apply_plan_overrides(plan_scenes: list[dict], source_scenes: list[dict]) -> None:
    for scene, src in zip(plan_scenes, source_scenes):
        if _broll_override(src) == 0 and _is_presentation(src.get("narration")):
            scene["broll"] = False
    for scene, src in zip(plan_scenes, source_scenes):
        ov = _broll_override(src)
        if ov == 1:
            scene["broll"] = True
        elif ov == -1:
            scene["broll"] = False


def enforce_broll_policy(plan_scenes: list[dict], source_scenes: list[dict]) -> dict:
    """Garante as regras de alternancia mesmo quando o LLM decide o b-roll.

    1. Nunca 100% b-roll: o avatar precisa abrir o video na tela.
    2. Nenhum trecho de avatar sozinho pode passar de MAX_AVATAR_SOLO_SECONDS.
    Retorna o resumo de cobertura para o plano.
    """
    n = len(plan_scenes)
    if n == 0:
        return {"coverage": 0.0, "max_avatar_solo_seconds": MAX_AVATAR_SOLO_SECONDS}

    _apply_plan_overrides(plan_scenes, source_scenes)

    # nunca 100% b-roll: o avatar precisa abrir o video. Cede numa cena livre
    # (nao travada em b-roll) para nao desfazer um pedido manual.
    if all(scene.get("broll") for scene in plan_scenes):
        free = next((i for i, s in enumerate(source_scenes) if _broll_override(s) != 1), 0)
        plan_scenes[free]["broll"] = False

    while True:
        oversized = _find_oversized_run(plan_scenes)
        if not oversized:
            break
        target = _pick_broll_target(oversized, source_scenes, n)
        if target is None:
            break
        plan_scenes[target]["broll"] = True

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


def _plan_scene_entry(
    i: int, scene: dict, scenes: list[dict], captioned_indexes: set, broll_flags: list[bool]
) -> dict:
    duration = _scene_duration(scene)
    start = round(float(scene.get("start_time") or 0), 3)
    caption = _caption_text(scene) if i in captioned_indexes else ""
    cap_start, cap_duration = _caption_timing(start, duration)
    next_scene = scenes[i + 1] if i + 1 < len(scenes) else None
    return {
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


def _pipeline_rules(video_style: str, density: str) -> list[str]:
    """Retorna as regras concretas do pipeline de edição para o par (video_style, density).

    Essas regras são armazenadas no edit_plan.json para auditoria e guiam o render.
    """
    rules: list[str] = []

    # Regras de estrutura do vídeo
    if video_style == "broll_only":
        rules += [
            "Sem avatar: o b-roll ocupa 100% da tela em todas as cenas.",
            "Toda cena sem take aceito fica com fundo preto (nenhum retângulo de avatar).",
            "Narração entra como áudio off-screen por cima do b-roll.",
            "Não há limite de corrida de b-roll (não existe avatar para respirar).",
        ]
    else:  # avatar_broll
        rules += [
            "Avatar é a camada base (tela cheia ou lateral): sempre visível quando não há b-roll.",
            "B-roll entra por cima do avatar nas cenas marcadas.",
            "Primeira e última cenas sempre mostram o apresentador (sem b-roll).",
            f"Avatar nunca fica sozinho por mais de {MAX_AVATAR_SOLO_SECONDS:.0f}s — a engine insere b-roll para quebrar corridas longas.",
        ]

    # Regras de densidade de b-roll
    if density == "key_moments":
        rules += [
            f"B-roll só em cenas com score de relevância ≥ {_KEY_MOMENTS_SCORE_THRESHOLD} (overlay_text, termos-chave, perguntas, números).",
            "Cenas narrativas/de transição ficam com o apresentador na tela.",
            "Cobertura estimada: 30–40% das cenas totais.",
        ]
    elif density == "full_coverage":
        rules += [
            "B-roll em quase todas as cenas — sem limite de corrida contínua.",
            "Exceções: cenas de apresentação/saudação e cenas com override manual 'sem b-roll'.",
            "Cobertura estimada: 80–90% das cenas totais.",
            "Ideal para vídeos faceless ou com muito conteúdo visual.",
        ]
    else:  # moderate
        rules += [
            f"B-roll alterna com o apresentador: respiro obrigatório a cada {MAX_BROLL_RUN_SECONDS:.0f}s de b-roll contínuo.",
            "Cenas de apresentação/saudação ficam com o apresentador.",
            "Cobertura estimada: 50–65% das cenas totais.",
        ]

    # Regras universais
    rules += [
        "Cenas com override manual 'sem b-roll' nunca recebem b-roll (trava de usuário).",
        "Cenas com override manual 'forçar b-roll' sempre recebem b-roll (trava de usuário).",
        "Cenas de apresentação/saudação ('eu sou', 'olá', 'fala galera'…) nunca levam b-roll.",
        f"Legendas (drawtext/FFmpeg) em até {int(MAX_CAPTION_DENSITY * 100)}% das cenas, priorizando as de maior score.",
        "Transição padrão: corte seco; fade a cada 4 cenas ou mudança de zona narrativa.",
    ]
    return rules


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
    density = config.get("broll_density", "moderate")
    video_style = config.get("video_style", "avatar_broll")
    captioned_indexes = _caption_indexes(scenes)
    broll_flags = _broll_flags(scenes, density=density, video_style=video_style)
    plan_scenes = [
        _plan_scene_entry(i, scene, scenes, captioned_indexes, broll_flags)
        for i, scene in enumerate(scenes)
    ]
    if video_style == "broll_only":
        # Modo só b-roll: todas as cenas têm b-roll, não há avatar para preservar
        for idx_ps, ps in enumerate(plan_scenes):
            if _broll_override(scenes[idx_ps]) != -1:
                ps["broll"] = True
        broll_policy = {
            "coverage": 1.0,
            "max_avatar_solo_seconds": 0,
            "broll_scenes": len(plan_scenes),
            "total_scenes": len(plan_scenes),
        }
    else:
        broll_policy = enforce_broll_policy(plan_scenes, scenes)

    # Descrição legível do pipeline de edição aplicado
    _DENSITY_LABEL = {
        "key_moments": "b-roll só em pontos-chave (~30-40% das cenas)",
        "moderate": "b-roll moderado com respiro (padrão)",
        "full_coverage": "b-roll em quase todo o vídeo (~80-90%)",
    }
    _STYLE_LABEL = {
        "avatar_broll": "avatar como base + b-roll sobreposto",
        "broll_only": "só b-roll (sem avatar na tela)",
    }

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
        "broll_density": density,
        "video_style": video_style,
        "editorial_pipeline": {
            "video_style": _STYLE_LABEL.get(video_style, video_style),
            "broll_density": _DENSITY_LABEL.get(density, density),
            "rules": _pipeline_rules(video_style, density),
        },
        "broll_policy": broll_policy,
        "scenes": plan_scenes,
        "audio": None,
        "avatar": None,
    }
    if narration_file:
        plan["audio"] = {"src": narration_file, "volume": 1.0}
    if avatar_file and video_style != "broll_only":
        # O avatar e a base do video: tela cheia, com os b-rolls por cima.
        plan["avatar"] = {
            "src": avatar_file,
            "mode": "base",
            "position": safe_area if safe_area in {"left", "right"} else "right",
            "scale": float(config.get("avatar_safe_width_ratio") or 0.30),
        }
    return plan
