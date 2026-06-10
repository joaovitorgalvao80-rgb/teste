"""Smoke real do cerebro editorial: chama OpenRouter e imprime o plano.

Chave somente via variavel de ambiente OPENROUTER_KEY (nunca em arquivo).

Uso:
  $env:OPENROUTER_KEY = "..."; python tools/smoke_openrouter_plan.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services import edit_plan  # noqa: E402

SCENES = [
    {
        "scene_id": "scene_001", "start_time": 0.0, "duration": 5.0,
        "narration": "Todo criador de conteudo perde horas montando b-roll na mao.",
        "overlay_text": "",
    },
    {
        "scene_id": "scene_002", "start_time": 5.0, "duration": 6.0,
        "narration": "O NWRCH Studio mapeia o roteiro, busca os clipes certos e monta a base sozinho.",
        "overlay_text": "",
    },
    {
        "scene_id": "scene_003", "start_time": 11.0, "duration": 4.0,
        "narration": "Voce so revisa, aprova e publica o video final.",
        "overlay_text": "",
    },
]


def main() -> int:
    key = os.environ.get("OPENROUTER_KEY", "").strip()
    if not key:
        print("Defina OPENROUTER_KEY no ambiente.", file=sys.stderr)
        return 2
    project = {"name": "Smoke editorial"}
    config = {"avatar_safe_area": "right", "resolution": "1920x1080"}

    plan = edit_plan.build_edit_plan_with_llm(project, config, SCENES, openrouter_key=key)
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    if plan.get("editorial") != "llm":
        print("SMOKE_RESULT: FAILED - caiu no fallback deterministico")
        return 1
    captions = [s["caption"] for s in plan["scenes"] if s["caption"]]
    if not captions:
        print("SMOKE_RESULT: FAILED - LLM nao gerou nenhum caption")
        return 1
    print("SMOKE_RESULT: SUCCESS -", len(captions), "captions gerados pelo LLM")
    return 0


if __name__ == "__main__":
    sys.exit(main())
