"""Cerebro editorial via OpenRouter: decide motion, transicoes e captions.

Recebe as cenas do projeto e pede a um LLM um plano editorial por cena.
A resposta e validada campo a campo contra o que o runner HyperFrames
suporta; qualquer cena invalida ou ausente cai no plano deterministico
(services/edit_plan.py). Falha de rede/chave/JSON devolve None e o app
segue funcionando sem IA.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import requests

logger = logging.getLogger("nwrch.llm")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-4o-mini"

# O runner HyperFrames so anima esses motions; qualquer outro vira estatico.
ALLOWED_MOTIONS = {"slow_push_in", "slow_pull_out", "drift_left", "drift_right", "hold", "none"}
ALLOWED_TRANSITIONS = {"fade", "none"}
MAX_CAPTION_CHARS = 80

_PROMPT = """You are a senior video editor orchestrating the final cut of a talking-head video.
The presenter (avatar) video is the BASE layer, full screen, for the whole duration.
A b-roll reel (one clip per scene, exact durations) can cover the screen on top of it.
Your job is to decide, per scene, the edit that will be applied:

- "broll": true to cut away to the b-roll footage covering the screen during the scene,
  false to stay on the presenter talking on camera. HARD RULES you must respect:
  the presenter must NEVER stay alone on screen for more than 30 seconds in a row;
  b-roll must NEVER cover the entire video (keep the opening hook and the closing
  on camera, plus short returns to the presenter at strong personal moments).
  Aim for roughly 50-70% b-roll coverage, cutting away on descriptive/visual passages.
- "motion": camera feel applied to the b-roll while it is on screen. One of:
  "slow_push_in" (slow zoom in, adds tension/focus), "slow_pull_out" (slow zoom out,
  reveals context/breathes), "drift_left" or "drift_right" (subtle lateral movement),
  "hold" or "none" (rest moment). Vary motion with intent; do not alternate mechanically.
- "transition_out": cut into the NEXT scene. One of: "fade" (soft, for topic/mood changes)
  or "none" (hard cut, for rhythm and continuity). Last scene must be "none".
- "caption": a short on-screen text for the scene, written from the narration.
  Max 8 words, same language as the narration, punchy, no quotes, no hashtags, no emoji.
  Use "" (empty) when the scene works better without text. Captions are punctual editorial
  beats, not subtitles; aim for about one caption every 3 scenes, only on key moments.

Respond ONLY with JSON in this exact shape:
{"scenes": [{"scene_id": "...", "broll": true, "motion": "...", "transition_out": "...", "caption": "..."}]}
One object per input scene, same scene_id, same order.

PROJECT: {project_name}
SCENES:
{scenes_block}
"""


def _scenes_block(scenes: list[dict]) -> str:
    lines = []
    for s in scenes:
        duration = float(s.get("duration") or 0)
        narration = str(s.get("narration") or "").strip().replace("\n", " ")
        lines.append(
            f'- scene_id={s.get("scene_id", "")} duration={duration:.1f}s'
            f' narration="{narration[:280]}"'
        )
    return "\n".join(lines)


def _coerce_broll(value: object) -> Optional[bool]:
    """true/false do LLM em qualquer formato razoavel; None quando nao opinou."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in {"true", "yes", "1", "broll", "b-roll"}:
            return True
        if low in {"false", "no", "0", "avatar", "presenter"}:
            return False
    return None


def _validate_directive(item: dict) -> Optional[dict]:
    """Normaliza uma decisao de cena do LLM; None se nao der para aproveitar."""
    sid = str(item.get("scene_id") or "").strip()
    if not sid:
        return None
    motion = str(item.get("motion") or "").strip().lower()
    transition = str(item.get("transition_out") or "").strip().lower()
    caption = str(item.get("caption") or "").strip().strip('"')
    if motion not in ALLOWED_MOTIONS:
        motion = ""
    if transition not in ALLOWED_TRANSITIONS:
        transition = ""
    if len(caption) > MAX_CAPTION_CHARS:
        caption = caption[:MAX_CAPTION_CHARS].rsplit(" ", 1)[0]
    return {
        "scene_id": sid,
        "motion": motion,
        "transition_out": transition,
        "caption": caption,
        "broll": _coerce_broll(item.get("broll")),
    }


def generate_scene_directives(
    project: dict,
    scenes: list[dict],
    api_key: str,
    model: str = DEFAULT_MODEL,
) -> Optional[dict[str, dict]]:
    """Pede ao LLM as decisoes editoriais por cena.

    Retorna {scene_id: {"motion", "transition_out", "caption"}} apenas com os
    campos validos, ou None quando a chamada/parse falha por completo.
    """
    if not api_key or not scenes:
        return None
    prompt = _PROMPT.replace("{project_name}", str(project.get("name") or "")).replace(
        "{scenes_block}", _scenes_block(scenes)
    )
    try:
        resp = requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.4,
                "response_format": {"type": "json_object"},
            },
            timeout=120,
        )
        if resp.status_code >= 400:
            logger.warning("OpenRouter HTTP %s: %s", resp.status_code, resp.text[:300])
            return None
        content = resp.json()["choices"][0]["message"]["content"]
        data = json.loads(content)
    except Exception as exc:  # noqa: BLE001 - fallback intencional
        logger.warning("OpenRouter erro, usando plano deterministico: %s", exc)
        return None

    directives: dict[str, dict] = {}
    for item in data.get("scenes", []):
        if not isinstance(item, dict):
            continue
        directive = _validate_directive(item)
        if directive:
            directives[directive["scene_id"]] = directive
    return directives or None
