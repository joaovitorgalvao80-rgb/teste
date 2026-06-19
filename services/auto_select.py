"""Seleção automática do melhor take por cena.

Duas camadas:
  1. heuristic_score: ranking instantâneo por adequação técnica (tipo, resolução,
     duração compatível com a cena, keyword principal).
  2. rank_with_groq: a IA compara os melhores candidatos da heurística com o
     objetivo visual e a narração da cena e escolhe o mais adequado, com
     justificativa. Qualquer falha (sem chave, rede, JSON ruim) cai na heurística.

O resultado nunca é vazio se a cena tiver candidatos: sempre há um escolhido.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import requests

from . import api_usage, scoring
from .groq_service import GROQ_URL, DEFAULT_MODEL, resolve_model

logger = logging.getLogger("nwrch.autoselect")

SCENES_PER_CALL = 8       # cenas por chamada ao Groq
CANDIDATES_PER_SCENE = 10  # top-N da heurística enviados à IA

# Pesos da relevância textual e penalidades de curadoria.
RELEVANCE_WEIGHT = 35.0     # quanto a relevância textual (0-1) vale no score
GENERIC_PENALTY = 12.0      # keyword puramente genérica de banco de imagem
DIVERSITY_PENALTY = 60.0    # asset/autor já escolhido para outra cena
VISION_WEIGHT = 0.5         # peso por ponto (0-100) da análise de visão por IA
VISION_DISCARD_PENALTY = 40.0  # IA de visão marcou o asset como "descartar"
MIN_AUTO_CONTEXT = 0.33
MIN_AUTO_CONTEXT_WITH_RISK = 0.55


def _type_score(config: dict, is_video: bool) -> float:
    prefer_video = (config.get("asset_type_priority") or "video") == "video"
    score = 30.0 if is_video == prefer_video else 0.0
    if not is_video and not config.get("image_fallback"):
        score -= 15.0
    return score


def _resolution_score(asset: dict, config: dict) -> float:
    """Cobre a resolução alvo sem exagerar."""
    try:
        target_w = int(str(config.get("resolution") or "1920x1080").split("x", 1)[0])
    except ValueError:
        target_w = 1920
    width = int(asset.get("width") or 0)
    if width >= target_w:
        return 20.0
    if width >= target_w * 0.66:
        return 10.0
    return 0.0


def _video_duration_score(scene: dict, asset: dict) -> float:
    """Vídeo precisa cobrir a duração da cena (loop degrada a percepção)."""
    duration = float(asset.get("duration") or 0)
    scene_duration = float(scene.get("duration") or 0)
    score = 0.0
    if duration >= scene_duration:
        score += 20.0
    elif duration >= scene_duration * 0.5:
        score += 8.0
    if 3 <= duration <= 30:
        score += 5.0
    return score


def _vision_score(asset: dict) -> float:
    """Julgamento visual real por IA (quando já rodou e não é a heurística)."""
    provider = asset.get("vision_provider") or ""
    if not provider or provider == "heuristic":
        return 0.0
    score = VISION_WEIGHT * float(asset.get("vision_score") or 0)
    if (asset.get("vision_verdict") or "") == "descartar":
        score -= VISION_DISCARD_PENALTY
    return score


def heuristic_score(scene: dict, asset: dict, config: dict) -> float:
    """Pontua a adequação técnica + textual de um candidato à cena (maior = melhor)."""
    is_video = asset.get("asset_type") == "video"
    score = _type_score(config, is_video) + _resolution_score(asset, config)
    if is_video:
        score += _video_duration_score(scene, asset)

    # relevância textual: o quanto a keyword que trouxe o asset corresponde ao
    # conceito visual da cena (sem ela, assets da mesma busca ordenavam só por pixels).
    score += RELEVANCE_WEIGHT * scoring.context_relevance(scene, asset)

    # penaliza keyword puramente genérica de banco de imagem
    if scoring.is_generic_keyword(asset.get("keyword", "")):
        score -= GENERIC_PENALTY

    # imagens geradas pelo usuário foram feitas sob medida para a cena
    if asset.get("source") == "generated":
        score += 12.0

    score += _vision_score(asset)
    return score


def rank_candidates(scene: dict, candidates: list[dict], config: dict) -> list[dict]:
    """Ordena candidatos pela heurística (melhor primeiro)."""
    return sorted(
        candidates,
        key=lambda a: heuristic_score(scene, a, config),
        reverse=True,
    )


def _candidates_block(scene: dict, candidates: list[dict]) -> str:
    lines = [
        f'SCENE {scene["scene_id"]} (db_id={scene["id"]})',
        f'  narration (pt-BR): {scene.get("narration", "")}',
        f'  visual_goal: {scene.get("visual_goal", "")}',
        f'  must_show: {", ".join(scene.get("must_show") or []) or "-"}',
        f'  must_not_show: {", ".join(scene.get("must_not_show") or []) or "-"}',
        f'  scene_duration: {float(scene.get("duration") or 0):.1f}s',
        "  candidates:",
    ]
    for a in candidates:
        kind = a.get("asset_type", "video")
        dur = f'{float(a.get("duration") or 0):.0f}s' if kind == "video" else "still"
        lines.append(
            f'    - asset_id={a["id"]} source={a.get("source")} type={kind} '
            f'{a.get("width")}x{a.get("height")} {dur} matched_keyword="{a.get("keyword", "")}"'
        )
    return "\n".join(lines)


def _build_prompt(scenes_with_candidates: list[tuple[dict, list[dict]]]) -> str:
    blocks = "\n\n".join(_candidates_block(s, c) for s, c in scenes_with_candidates)
    return f"""You are a senior B-roll curator for a YouTube video in Brazilian Portuguese.
For EACH scene below, pick the ONE candidate asset that best illustrates the narration
and visual goal. B-roll is only illustration: prefer a clear simple object/action
over an over-specific full-scene match. Judge by the matched keyword, asset type,
dimensions and duration.
Prefer videos that cover the scene duration; avoid candidates that likely violate
must_not_show.

Return ONE JSON object only, shape:
{{
  "choices": [
    {{"scene_id": "scene_001", "asset_id": 123, "reason": "short pt-BR justification (max 18 words)"}}
  ]
}}

Rules:
- Exactly one choice per scene, same scene_id as given.
- asset_id MUST be one of the listed candidate ids for that scene.
- reason in Brazilian Portuguese, concise and concrete.
- Do not require the full location/context if the central visual subject is clear.

SCENES:
{blocks}
"""


def rank_with_groq(
    scenes_with_candidates: list[tuple[dict, list[dict]]],
    groq_key: str,
    model: str = DEFAULT_MODEL,
) -> dict[int, tuple[int, str]]:
    """Retorna {scene_db_id: (asset_id, reason)} para as cenas que a IA cobriu.

    Falha de rede/parse devolve {} (caller usa heurística).
    """
    if not groq_key or not scenes_with_candidates:
        return {}
    valid_by_scene = {
        s["id"]: {a["id"] for a in cands} for s, cands in scenes_with_candidates
    }
    code_to_db = {s["scene_id"]: s["id"] for s, _ in scenes_with_candidates}
    try:
        import time

        start = time.monotonic()
        resp = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
            json={
                "model": resolve_model(model),
                "messages": [{"role": "user", "content": _build_prompt(scenes_with_candidates)}],
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
            },
            timeout=120,
        )
        api_usage.record(
            "groq",
            "auto_select_rank",
            status_code=resp.status_code,
            ok=resp.status_code < 400,
            latency_ms=api_usage.elapsed_ms(start),
        )
        if resp.status_code >= 400:
            logger.warning("Groq rank HTTP %s: %s", resp.status_code, resp.text[:300])
            return {}
        data = json.loads(resp.json()["choices"][0]["message"]["content"])
    except Exception as exc:  # noqa: BLE001 - fallback heurístico intencional
        logger.warning("Groq rank erro, usando heuristica: %s", exc)
        return {}

    out: dict[int, tuple[int, str]] = {}
    for choice in data.get("choices", []):
        if not isinstance(choice, dict):
            continue
        scene_db_id = code_to_db.get(str(choice.get("scene_id") or ""))
        try:
            asset_id = int(choice.get("asset_id"))
        except (TypeError, ValueError):
            continue
        if scene_db_id is None or asset_id not in valid_by_scene.get(scene_db_id, set()):
            continue
        reason = str(choice.get("reason") or "").strip()[:200]
        out[scene_db_id] = (asset_id, reason)
    return out


def _filter_candidates(candidates: list[dict]) -> list[dict]:
    """Aplica os filtros de visão: descarta reprovados e prefere os aprovados."""
    # A IA de visao ja reprovou ('descartar') os assets fora de contexto.
    non_discard = [c for c in candidates if (c.get("vision_verdict") or "") != "descartar"]
    if non_discard:
        candidates = non_discard
    # Prefere takes que a IA de visao REALMENTE aprovou (viu a imagem e achou que combina).
    vetted = [
        c for c in candidates
        if (c.get("vision_provider") or "heuristic") != "heuristic"
        and (c.get("vision_verdict") or "") in {"ótimo", "bom"}
    ]
    return vetted or candidates


def _acceptable_for_auto_select(scene: dict, asset: dict) -> bool:
    context = scoring.context_analysis(scene, asset)
    score = float(context["context_score"])
    risks = context.get("risks") or []
    if risks:
        return score >= MIN_AUTO_CONTEXT_WITH_RISK
    return score >= MIN_AUTO_CONTEXT


def _prepare_pending(
    scenes: list[dict], candidates_by_scene: dict[int, list[dict]], config: dict
) -> list[tuple[dict, list[dict]]]:
    pending: list[tuple[dict, list[dict]]] = []
    for scene in scenes:
        candidates = candidates_by_scene.get(scene["id"]) or []
        if not candidates:
            continue
        candidates = _filter_candidates(candidates)
        candidates = [c for c in candidates if _acceptable_for_auto_select(scene, c)]
        if not candidates:
            logger.warning("auto_select: sem candidato confiavel para %s", scene.get("scene_id"))
            continue
        top = rank_candidates(scene, candidates, config)[:CANDIDATES_PER_SCENE]
        pending.append((scene, top))
    return pending


def _resolve_take(
    scene: dict,
    top: list[dict],
    ai: Optional[tuple[int, str]],
    config: dict,
    used_signatures: set[tuple[str, str]],
    used_authors: set[str],
) -> tuple[dict, tuple[int, float, str]]:
    if ai:
        asset_id, reason = ai
        asset = next(a for a in top if a["id"] == asset_id)
        score = heuristic_score(scene, asset, config)
        return asset, (asset_id, score, reason or "escolhido pela IA")
    # Fallback determinístico: melhor candidato penalizando assets/autores
    # já usados em outras cenas (diversidade visual entre cenas).
    asset = _best_with_diversity(scene, top, config, used_signatures, used_authors)
    score = heuristic_score(scene, asset, config)
    relevance = scoring.context_relevance(scene, asset)
    return asset, (asset["id"], score, _fallback_reason(relevance))


def choose_best_takes(
    scenes: list[dict],
    candidates_by_scene: dict[int, list[dict]],
    config: dict,
    groq_key: str = "",
    model: str = DEFAULT_MODEL,
    progress: Optional[callable] = None,
    seed_signatures: Optional[set] = None,
    seed_authors: Optional[set] = None,
) -> dict[int, tuple[int, float, str]]:
    """Escolhe o melhor take por cena.

    Retorna {scene_db_id: (asset_id, score, reason)}. Cenas sem candidatos
    ficam de fora. `progress(done, total)` é chamado por lote, se passado.
    seed_signatures/seed_authors: assinaturas de assets já selecionados em outras
    cenas (rodadas anteriores), para aplicar a penalidade de diversidade cross-cena.
    """
    pending = _prepare_pending(scenes, candidates_by_scene, config)

    results: dict[int, tuple[int, float, str]] = {}
    used_signatures: set[tuple[str, str]] = set(seed_signatures or ())
    used_authors: set[str] = set(seed_authors or ())
    done = 0
    for start in range(0, len(pending), SCENES_PER_CALL):
        batch = pending[start:start + SCENES_PER_CALL]
        ai_choices = rank_with_groq(batch, groq_key, model=model) if groq_key else {}
        for scene, top in batch:
            scene_db_id = scene["id"]
            asset, result = _resolve_take(
                scene, top, ai_choices.get(scene_db_id), config, used_signatures, used_authors
            )
            results[scene_db_id] = result
            used_signatures.add(scoring.asset_signature(asset))
            if asset.get("author"):
                used_authors.add(str(asset["author"]))
        done += len(batch)
        if progress:
            progress(done, len(pending))
    return results


def _best_with_diversity(
    scene: dict,
    candidates: list[dict],
    config: dict,
    used_signatures: set[tuple[str, str]],
    used_authors: set[str],
) -> dict:
    """Escolhe o melhor candidato descontando repetição de asset/autor entre cenas."""
    def adjusted(asset: dict) -> float:
        score = heuristic_score(scene, asset, config)
        if scoring.asset_signature(asset) in used_signatures:
            score -= DIVERSITY_PENALTY
        if asset.get("author") and str(asset["author"]) in used_authors:
            score -= DIVERSITY_PENALTY / 2
        return score

    return max(candidates, key=adjusted)


def _fallback_reason(relevance: float) -> str:
    label = scoring.relevance_label(relevance)
    if relevance < 0.33:
        return f"melhor opção técnica disponível (relevância {label} — revise)"
    return f"melhor opção técnica e de relevância {label} (resolução/duração/tipo)"
