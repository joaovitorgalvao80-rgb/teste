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

from .groq_service import GROQ_URL, DEFAULT_MODEL, resolve_model

logger = logging.getLogger("nwrch.autoselect")

SCENES_PER_CALL = 8       # cenas por chamada ao Groq
CANDIDATES_PER_SCENE = 10  # top-N da heurística enviados à IA


def heuristic_score(scene: dict, asset: dict, config: dict) -> float:
    """Pontua a adequação técnica de um candidato à cena (maior = melhor)."""
    score = 0.0
    is_video = asset.get("asset_type") == "video"
    prefer_video = (config.get("asset_type_priority") or "video") == "video"

    if is_video == prefer_video:
        score += 30.0
    if not is_video and not config.get("image_fallback"):
        score -= 15.0

    # resolução: cobre a resolução alvo sem exagerar
    try:
        target_w = int(str(config.get("resolution") or "1920x1080").split("x", 1)[0])
    except ValueError:
        target_w = 1920
    width = int(asset.get("width") or 0)
    if width >= target_w:
        score += 20.0
    elif width >= target_w * 0.66:
        score += 10.0

    # vídeo precisa cobrir a duração da cena (loop degrada a percepção)
    if is_video:
        duration = float(asset.get("duration") or 0)
        scene_duration = float(scene.get("duration") or 0)
        if duration >= scene_duration:
            score += 20.0
        elif duration >= scene_duration * 0.5:
            score += 8.0
        if 3 <= duration <= 30:
            score += 5.0

    # casou com a keyword principal da cena (mais alinhada ao visual_goal)
    keywords = scene.get("keywords") or []
    if keywords and asset.get("keyword") == keywords[0]:
        score += 10.0

    # imagens geradas pelo usuário foram feitas sob medida para a cena
    if asset.get("source") == "generated":
        score += 12.0

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
and visual goal. Judge by the matched keyword, asset type, dimensions and duration.
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


def choose_best_takes(
    scenes: list[dict],
    candidates_by_scene: dict[int, list[dict]],
    config: dict,
    groq_key: str = "",
    model: str = DEFAULT_MODEL,
    progress: Optional[callable] = None,
) -> dict[int, tuple[int, float, str]]:
    """Escolhe o melhor take por cena.

    Retorna {scene_db_id: (asset_id, score, reason)}. Cenas sem candidatos
    ficam de fora. `progress(done, total)` é chamado por lote, se passado.
    """
    pending: list[tuple[dict, list[dict]]] = []
    for scene in scenes:
        candidates = candidates_by_scene.get(scene["id"]) or []
        if not candidates:
            continue
        top = rank_candidates(scene, candidates, config)[:CANDIDATES_PER_SCENE]
        pending.append((scene, top))

    results: dict[int, tuple[int, float, str]] = {}
    done = 0
    for start in range(0, len(pending), SCENES_PER_CALL):
        batch = pending[start:start + SCENES_PER_CALL]
        ai_choices = rank_with_groq(batch, groq_key, model=model) if groq_key else {}
        for scene, top in batch:
            scene_db_id = scene["id"]
            ai = ai_choices.get(scene_db_id)
            if ai:
                asset_id, reason = ai
                asset = next(a for a in top if a["id"] == asset_id)
                score = heuristic_score(scene, asset, config)
                results[scene_db_id] = (asset_id, score, reason or "escolhido pela IA")
            else:
                best = top[0]
                score = heuristic_score(scene, best, config)
                results[scene_db_id] = (
                    best["id"],
                    score,
                    "melhor opção técnica (resolução/duração/tipo)",
                )
        done += len(batch)
        if progress:
            progress(done, len(pending))
    return results
