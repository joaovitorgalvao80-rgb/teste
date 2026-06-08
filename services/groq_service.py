"""Geracao de intencao visual + keywords por cena usando Groq.

A IA recebe cada bloco (com timestamp e narracao) e devolve, para cada cena:
  visual_goal, keywords (ingles), must_show, must_not_show, asset_type, overlay_text.

Se a chave Groq nao estiver disponivel ou a chamada falhar, usamos um
fallback heuristico (PT->EN) para nao travar o fluxo no MVP.
"""
from __future__ import annotations

import json
import re
from typing import Optional

import requests

from .script_parser import remove_accents

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_TRANSCRIBE_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
DEFAULT_MODEL = "llama-3.3-70b-versatile"
GROQ_MODELS = [
    ("llama-3.3-70b-versatile", "Llama 3.3 70B — melhor qualidade (padrão)"),
    ("llama-3.1-8b-instant",    "Llama 3.1 8B Instant — mais rápido"),
    ("gemma2-9b-it",            "Gemma 2 9B — Google"),
    ("mixtral-8x7b-32768",      "Mixtral 8x7B — contexto longo"),
]

PT_TO_EN = {
    "dengue": "dengue fever Brazil mosquito",
    "mosquito": "mosquito close up",
    "aedes": "aedes aegypti mosquito",
    "balde": "bucket water backyard",
    "agua": "stagnant water backyard",
    "agua parada": "stagnant water mosquito breeding",
    "bti": "biological mosquito control larvicide",
    "larvicida": "biological larvicide mosquito control",
    "quintal": "rural backyard Brazil",
    "roca": "rural farm Brazil",
    "sitio": "rural farm backyard",
    "hospital": "hospital emergency room",
    "abelha": "bee flower close up",
    "cachorro": "dog backyard",
    "crianca": "child playing backyard",
    "veneno": "pesticide spraying",
    "pulverizador": "garden sprayer",
    "calha": "gutter water leaves",
    "pneu": "old tire water",
    "garrafa": "plastic bottle water",
}


def _overlay_from(text: str) -> str:
    words = re.sub(r"[^A-Za-zA-Yorg0-9\s]", " ", text).split()
    words = [w for w in words if len(w) > 2][:5]
    return " ".join(words).upper()


def fallback_scene_brief(scene: dict, style: str, avatar_safe_area: str) -> dict:
    text = scene.get("narration", "")
    low = remove_accents(text.lower())
    keywords: list[str] = []
    for pt, en in PT_TO_EN.items():
        if remove_accents(pt) in low and en not in keywords:
            keywords.append(en)
    clean = re.sub(r"[^a-z0-9\s]", " ", remove_accents(text.lower()))
    tokens = [t for t in clean.split() if len(t) > 4]
    if tokens:
        keywords.append(" ".join(tokens[:4]))
    for fb in ["rural Brazil daily life", "practical close up detail", "natural outdoor scene"]:
        if fb not in keywords:
            keywords.append(fb)
    return {
        "scene_id": scene["scene_id"],
        "visual_goal": f"Concrete editorial B-roll for: {text}"[:240],
        "keywords": keywords[:3],
        "must_show": [],
        "must_not_show": ["corporate stock", "watermark", "text inside footage"],
        "asset_type": "video",
        "overlay_text": _overlay_from(text),
        "avatar_safe_area": avatar_safe_area,
    }


def _build_prompt(scenes: list[dict], style: str, avatar_safe_area: str, safe_ratio: float) -> str:
    blocks = []
    for s in scenes:
        blocks.append(
            f'{s["scene_id"]} [{s["start_time"]:.1f}s-{s["end_time"]:.1f}s]: {s.get("narration","")}'
        )
    joined = "\n".join(blocks)
    return f"""You are a senior YouTube editor planning a 100% B-roll video from a Brazilian Portuguese script.

For EACH scene below, produce concrete visual search intent for Pexels/Pixabay.

Return ONE JSON object only, shape:
{{
  "scenes": [
    {{
      "scene_id": "scene_001",
      "visual_goal": "what footage should appear here, concrete, in English",
      "keywords": ["english video search phrase", "english video search phrase", "english video search phrase"],
      "must_show": ["concrete element", "concrete element"],
      "must_not_show": ["thing to reject", "thing to reject"],
      "asset_type": "video",
      "overlay_text": "PT-BR uppercase, max 5 words, may be empty"
    }}
  ]
}}

Rules:
- Keywords MUST be in English (Pexels/Pixabay work better in English).
- Use concrete, filmable scenes. Avoid generic corporate stock.
- Visual style requested: {style}.
- Avatar safe area is on the {avatar_safe_area} (~{safe_ratio*100:.0f}% of width). Keep main action and text away from it.
- must_not_show should always include watermark / text inside footage / generic corporate stock.
- Return one scene object per input scene, same scene_id, same order.

SCENES:
{joined}
"""


def generate_briefs(
    scenes: list[dict],
    groq_key: str,
    style: str,
    avatar_safe_area: str,
    safe_ratio: float = 0.30,
    model: str = DEFAULT_MODEL,
) -> list[dict]:
    """Devolve uma lista de briefs (1 por cena), sempre completa.

    Usa Groq quando ha chave; cai no fallback heuristico cena a cena para
    qualquer cena que a IA nao tenha coberto.
    """
    by_id: dict[str, dict] = {}

    if groq_key:
        try:
            resp = requests.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "user", "content": _build_prompt(scenes, style, avatar_safe_area, safe_ratio)}
                    ],
                    "temperature": 0.3,
                    "response_format": {"type": "json_object"},
                },
                timeout=180,
            )
            if resp.status_code < 400:
                content = resp.json()["choices"][0]["message"]["content"]
                data = json.loads(content)
                for item in data.get("scenes", []):
                    sid = item.get("scene_id")
                    if sid:
                        by_id[sid] = item
            else:
                print(f"[Groq] HTTP {resp.status_code}: {resp.text[:300]}")
        except Exception as exc:  # noqa: BLE001 - fallback intencional
            print(f"[Groq] erro, usando fallback: {exc}")

    briefs: list[dict] = []
    for scene in scenes:
        ai = by_id.get(scene["scene_id"])
        fb = fallback_scene_brief(scene, style, avatar_safe_area)
        if not ai:
            briefs.append(fb)
            continue
        keywords = ai.get("keywords") or fb["keywords"]
        if isinstance(keywords, str):
            keywords = [keywords]
        briefs.append(
            {
                "scene_id": scene["scene_id"],
                "visual_goal": (ai.get("visual_goal") or fb["visual_goal"]).strip(),
                "keywords": [str(k).strip() for k in keywords if str(k).strip()][:3] or fb["keywords"],
                "must_show": [str(k).strip() for k in (ai.get("must_show") or []) if str(k).strip()],
                "must_not_show": [str(k).strip() for k in (ai.get("must_not_show") or fb["must_not_show"]) if str(k).strip()],
                "asset_type": ai.get("asset_type") or "video",
                "overlay_text": str(ai.get("overlay_text") or "").upper()[:60],
                "avatar_safe_area": avatar_safe_area,
            }
        )
    return briefs


def _fmt_stamp(seconds: float) -> str:
    m = int(seconds // 60)
    s = seconds % 60
    return f"{m:02d}:{s:04.1f}"


def transcribe_audio(audio_bytes: bytes, filename: str, groq_key: str) -> str:
    """Transcreve áudio via Groq Whisper e retorna o roteiro no formato de timestamps.

    Retorna string pronta pra colar no campo de roteiro:
        [00:00.0 - 00:04.2] Você sabia que existe uma forma...
        [00:04.2 - 00:08.0] O problema é que quase ninguém...
    """
    if not groq_key:
        raise ValueError("Chave Groq necessária para transcrição de áudio.")

    resp = requests.post(
        GROQ_TRANSCRIBE_URL,
        headers={"Authorization": f"Bearer {groq_key}"},
        files={"file": (filename, audio_bytes, "application/octet-stream")},
        data={
            "model": "whisper-large-v3-turbo",
            "response_format": "verbose_json",
            "timestamp_granularities[]": "segment",
            "language": "pt",
        },
        timeout=300,
    )
    resp.raise_for_status()
    data = resp.json()
    segments = data.get("segments") or []
    if not segments:
        # fallback: retorna o texto completo sem timestamps
        return data.get("text", "").strip()

    lines = []
    for seg in segments:
        start = float(seg.get("start", 0))
        end = float(seg.get("end", start))
        text = seg.get("text", "").strip()
        if text:
            lines.append(f"[{_fmt_stamp(start)} - {_fmt_stamp(end)}] {text}")
    return "\n".join(lines)


def regenerate_keywords(narration: str, visual_goal: str, groq_key: str, style: str) -> list[str]:
    """Gera um novo conjunto de keywords para uma unica cena (botao 'gerar novas keywords')."""
    if groq_key:
        try:
            prompt = (
                "Generate 3 concrete English video search phrases for Pexels/Pixabay for this scene.\n"
                f"Visual style: {style}.\n"
                f"Narration (pt-BR): {narration}\n"
                f"Visual goal: {visual_goal}\n"
                'Return JSON only: {"keywords": ["...", "...", "..."]}'
            )
            resp = requests.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
                json={
                    "model": DEFAULT_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.6,
                    "response_format": {"type": "json_object"},
                },
                timeout=90,
            )
            if resp.status_code < 400:
                data = json.loads(resp.json()["choices"][0]["message"]["content"])
                kws = [str(k).strip() for k in data.get("keywords", []) if str(k).strip()]
                if kws:
                    return kws[:3]
        except Exception as exc:  # noqa: BLE001
            print(f"[Groq] regenerate erro: {exc}")
    return fallback_scene_brief({"scene_id": "x", "narration": narration}, style, "right")["keywords"]
